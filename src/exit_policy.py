"""
TRIO — Shared exit policy (ladder state machine)
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

One decision function for the live guard AND the backtester so the two
can never drift:

    T1  at t1_at_r (default 0.8R) -> close t1_fraction (50%), move the
        rest of the stop to breakeven.
    T2  at t2_at_r (default 1.5R) -> close t2_fraction (30% of the
        ORIGINAL size) and arm an ATR trail. Shadow-only unless
        ladder.enabled_phase2 is true.
    Trail ratchets the stop toward price and never loosens it.
    Stop  is checked before target on the same bar (conservative).
    EOD   (hard_flat) flattens whatever is left.

The function is pure: it returns actions and never touches a broker.
Callers execute the actions and feed the resulting state back next bar.

One-unit rule: a partial that would close zero quantity is a no-op and
does NOT set the ladder flag, so the rung can be retried next bar.

DISCLAIMER: Educational purposes only. Not financial advice.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class LadderState:
    """Mutable ladder memory for ONE open position."""
    side: str = "LONG"            # LONG or SHORT
    entry: float = 0.0
    initial_stop: float = 0.0
    stop: float = 0.0             # live stop (moves after T1 / trail)
    target: float = 0.0
    quantity: int = 0             # current remaining size
    original_quantity: int = 0
    t1_hit: bool = False
    t2_hit: bool = False
    trailing_armed: bool = False
    booked_pnl: float = 0.0


@dataclass
class ExitAction:
    """One thing the caller should do this bar."""
    kind: str                     # partial | close | shadow | trail
    reason: str = ""
    fraction_of_original: float = 0.0
    quantity: int = 0             # shares/units to close (partial/close)
    new_stop: Optional[float] = None
    shadow: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)


def ladder_config(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Resolve ladder knobs with the same defaults the guard used inline."""
    rm = (cfg or {}).get("risk_management", {}) if isinstance(cfg, dict) else {}
    ladder = rm.get("ladder", {}) if isinstance(rm.get("ladder"), dict) else {}
    trail = ladder.get("trailing_after_t2") or {}
    return {
        "t1_at_r": float(ladder.get("t1_at_r", rm.get("partial_at_r", 0.8))),
        "t2_at_r": float(ladder.get("t2_at_r", 1.5)),
        "t3_at_r": float(ladder.get("t3_at_r", 2.5)),
        "t1_fraction": float(ladder.get("t1_fraction",
                                         rm.get("partial_fraction", 0.5))),
        "t2_fraction": float(ladder.get("t2_fraction", 0.30)),
        "phase2": bool(ladder.get("enabled_phase2", False)),
        "trail_mult": float(trail.get("atr_multiplier") or 3.0),
    }


def r_multiple(state: LadderState, price: float) -> float:
    """Signed R of `price` vs the FROZEN initial stop (never the live stop)."""
    risk = abs(state.entry - state.initial_stop)
    if risk <= 0:
        return 0.0
    if state.side == "LONG":
        return (price - state.entry) / risk
    return (state.entry - price) / risk


def decide_exit(
    state: LadderState,
    *,
    price: float,
    high: Optional[float] = None,
    low: Optional[float] = None,
    atr: Optional[float] = None,
    hard_flat: bool = False,
    cfg: Optional[Dict[str, Any]] = None,
) -> List[ExitAction]:
    """Decide this bar's ladder actions. Does not mutate `state`.

    Intrabar extremes (`high`/`low`) drive stop/target touches so a bar
    that gaps through the stop is a stop. `price` (the close, live) is
    what partials and the trail are measured against. Stop is always
    reported before target when both are touched.
    """
    knobs = ladder_config(cfg)
    actions: List[ExitAction] = []
    if state.quantity <= 0:
        return actions

    # EOD hard-flat wins over everything: flatten the remainder.
    if hard_flat:
        actions.append(ExitAction(kind="close", reason="eod-squareoff",
                                   quantity=int(state.quantity)))
        return actions

    risk = abs(state.entry - state.initial_stop)
    if risk <= 0:
        return actions

    rr = r_multiple(state, price)
    stop = state.stop
    t1_hit = state.t1_hit
    t2_hit = state.t2_hit
    trailing = state.trailing_armed

    # --- T1: partial + breakeven. One-unit rule: zero qty -> no flag. ---
    if rr >= knobs["t1_at_r"] and not t1_hit:
        qty = int(state.original_quantity * knobs["t1_fraction"])
        qty = min(qty, state.quantity)
        if qty > 0:
            actions.append(ExitAction(
                kind="partial", reason="partial@T1-0.8R",
                fraction_of_original=knobs["t1_fraction"], quantity=qty,
                new_stop=state.entry,
                meta={"rr": round(rr, 2)}))
            stop = state.entry
            t1_hit = True

    # --- T2: partial + trail, or a shadow log when phase 2 is off. ---
    if t1_hit and rr >= knobs["t2_at_r"] and not t2_hit:
        if knobs["phase2"]:
            qty = int(state.original_quantity * knobs["t2_fraction"])
            qty = min(qty, max(state.quantity - (
                actions[-1].quantity if actions and actions[-1].kind == "partial"
                else 0), 0))
            if qty > 0:
                new_stop = stop
                if atr and atr > 0:
                    trail = (price - atr * knobs["trail_mult"]
                             if state.side == "LONG"
                             else price + atr * knobs["trail_mult"])
                    new_stop = _ratchet(state.side, stop, trail)
                actions.append(ExitAction(
                    kind="partial", reason="partial@T2-1.5R",
                    fraction_of_original=knobs["t2_fraction"], quantity=qty,
                    new_stop=new_stop, meta={"rr": round(rr, 2)}))
                stop = new_stop
                t2_hit = True
                trailing = True
            # else: no quantity -> leave t2_hit unset (one-unit rule)
        else:
            actions.append(ExitAction(
                kind="shadow", reason="ladder-T2", shadow=True,
                meta={"rr": round(rr, 2), "would_be": "partial@T2-1.5R"}))

    # --- Trail ratchet (only once T2 actually armed it). ---
    if trailing and atr and atr > 0:
        trail = (price - atr * knobs["trail_mult"] if state.side == "LONG"
                 else price + atr * knobs["trail_mult"])
        ratcheted = _ratchet(state.side, stop, trail)
        if ratcheted != stop:
            actions.append(ExitAction(kind="trail", reason="trail",
                                      new_stop=ratcheted))
            stop = ratcheted

    # --- Hard stop before target. Use the bar's extremes when given. ---
    lo = price if low is None else low
    hi = price if high is None else high
    if state.side == "LONG":
        stop_hit = lo <= stop
        tgt_hit = state.target > 0 and hi >= state.target
    else:
        stop_hit = hi >= stop
        tgt_hit = state.target > 0 and lo <= state.target

    remaining = state.quantity - sum(
        a.quantity for a in actions if a.kind == "partial")
    if stop_hit and remaining > 0:
        actions.append(ExitAction(kind="close", reason="stop-hit",
                                   quantity=remaining))
    elif tgt_hit and remaining > 0:
        actions.append(ExitAction(kind="close", reason="target-hit",
                                   quantity=remaining))
    return actions


def apply_actions(state: LadderState, actions: List[ExitAction],
                  price: float) -> float:
    """Mutate `state` to reflect executed actions. Returns realized pnl.

    Callers that fill at a different price (gap through the stop) should
    book their own pnl; this helper assumes fills at `price`, which is
    what the live guard does.
    """
    realized = 0.0
    for a in actions:
        if a.kind == "partial":
            qty = min(int(a.quantity), state.quantity)
            if qty <= 0:
                continue
            realized += _pnl(state, price, qty)
            state.quantity -= qty
            if a.reason.startswith("partial@T1"):
                state.t1_hit = True
            elif a.reason.startswith("partial@T2"):
                state.t2_hit = True
                state.trailing_armed = True
            if a.new_stop is not None:
                state.stop = a.new_stop
        elif a.kind == "trail" and a.new_stop is not None:
            state.stop = _ratchet(state.side, state.stop, a.new_stop)
        elif a.kind == "close":
            qty = min(int(a.quantity), state.quantity)
            realized += _pnl(state, price, qty)
            state.quantity -= qty
    # booked_pnl was double-touched above; recompute from the realized total
    state.booked_pnl += realized
    return realized


def _pnl(state: LadderState, price: float, qty: int) -> float:
    if qty <= 0:
        return 0.0
    if state.side == "LONG":
        return (price - state.entry) * qty
    return (state.entry - price) * qty


def _ratchet(side: str, current: float, proposed: float) -> float:
    """A trail only ever tightens."""
    if side == "LONG":
        return max(current, proposed)
    return min(current, proposed)
