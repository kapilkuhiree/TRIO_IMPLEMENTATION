"""
TRIO — Execution lifecycle (single shared path for every entry and exit)
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

Every trade — `python main.py paper`, premarket plan, session runner, live
rescan, local paper or MegaBull — MUST go through:

    needs   = reconcile_broker(trader)            # positions + PENDING scan
    ok, why = preflight(trader, candidate, proposed)  # halt/max/sector/margin/dup
    rec     = submit_candidate(trader, candidate, reason="premarket-plan"|"scan")
    record_confirmed_close(trader, symbol, record)    # pnl/consec/position/halt/log/alert once

No caller is allowed to `place_order()` + `_log_event()` + `add_position()`
by hand anymore — that is what produced the divergent scan_once/execute_plan
behaviour. The lifecycle owns refresh, risk refresh, halt, max, sector,
margin, dedup, submit, confirm, register, audit and alert in one place.

Shadow option spreads are tagged venue="local_shadow" and are EXCLUDED from
real account risk/equity by construction.

DISCLAIMER: Educational purposes only. Not financial advice.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from src.utils import get_logger, load_config

logger = get_logger("execution_lifecycle")

TERMINAL_SUBMIT = ("FILLED", "REJECTED")
_PENDING = ("PENDING", "PLACED", "ACCEPTED")


# ---------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------

def reconcile_broker(trader) -> Dict[str, Any]:
    """Refresh the mirror, scan for PENDING/UNKNOWN rows, rebuild risk.

    Returns {"pending": [...], "uncertain": bool}. On uncertain books sets
    the broker_uncertain halt; on a clean refresh clears ONLY that halt.
    """
    out: Dict[str, Any] = {"pending": [], "uncertain": False}
    broker = getattr(trader, "broker", None)
    if broker is None:
        return out
    try:
        broker.refresh_positions()
    except AttributeError:
        pass
    except Exception as exc:
        logger.warning("reconcile: refresh failed: %s", exc)
        try:
            from src.risk_manager import set_broker_uncertain
            set_broker_uncertain(f"refresh failed: {exc}")
            out["uncertain"] = True
        except Exception:
            pass
        return out
    try:
        pending = [s for s, p in (getattr(broker, "positions", {}) or {}).items()
                   if str(getattr(p, "status", "") or "").upper() in _PENDING
                   or str(getattr(p, "status", "") or "").upper().startswith("UNKNOWN")]
    except Exception:
        pending = []
    out["pending"] = pending
    try:
        from src.risk_manager import (set_broker_uncertain,
                                      clear_broker_uncertain, rebuild_from_ledger,
                                      get_risk_state)
        if pending:
            set_broker_uncertain(f"{len(pending)} pending/unknown position(s): "
                                 + ",".join(pending[:5]))
            out["uncertain"] = True
        else:
            clear_broker_uncertain()
        # Rebuild from the now-refreshed mirror + ledger.
        try:
            from datetime import datetime, timezone, timedelta as _td
            IST = timezone(_td(hours=5, minutes=30))
            today = datetime.now(IST).strftime("%Y-%m-%d")
        except Exception:
            today = None
        rebuild_from_ledger(getattr(broker, "positions", {}) or {},
                            getattr(broker, "closed_trades", []) or [],
                            today=today)
        rs = get_risk_state()
        try:
            trader._proposed = dict(getattr(rs, "positions", {}) or {})
        except Exception:
            pass
    except Exception as exc:
        logger.warning("reconcile: risk rebuild failed: %s", exc)
    return out


def _proposed_view(trader) -> Dict[str, Any]:
    """Current + already-accepted-this-pass portfolio for sector checks."""
    try:
        prop = getattr(trader, "_proposed", None)
        if isinstance(prop, dict) and prop:
            return dict(prop)
    except Exception:
        pass
    # Build from the live broker book (fresh start / first candidate).
    out: Dict[str, Any] = {}
    try:
        for s, p in (getattr(getattr(trader, "broker", None), "positions", {}) or {}).items():
            out[s] = {"size": int(getattr(p, "quantity", 0) or 0),
                      "entry": float(getattr(p, "avg_price", 0.0) or 0.0)}
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# preflight (returns ok, reason)
# ---------------------------------------------------------------------------

def preflight(trader, candidate: Dict[str, Any],
              proposed: Optional[Dict[str, Any]] = None,
              cfg: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """Halt, max, duplicate, sector, margin checks for ONE candidate.

    `cfg` should be the caller's already-loaded config (tests patch the
    caller's load_config binding). Falls back to load_config() when None.
    """
    from src.risk_manager import get_risk_state, would_breach_sector
    cfg = cfg if cfg is not None else load_config()
    allow_shorts = bool(cfg.get("trading", {}).get("allow_shorts", False))
    max_open = int(cfg.get("risk_management", {}).get("max_open_positions", 5) or 5)

    rs = get_risk_state()
    if getattr(rs, "halt_active", False):
        return False, f"halted: {getattr(rs, 'halt_reason', '')}"

    sym = str(candidate.get("symbol", ""))
    action = str(candidate.get("action", "HOLD"))
    try:
        qty = int(candidate.get("position_size") or 0)
    except Exception:
        qty = 0
    try:
        entry = float(candidate.get("entry_price") or 0.0)
    except Exception:
        entry = 0.0
    if action not in ("BUY", "SELL") or qty <= 0:
        return False, "invalid action/qty"

    broker = getattr(trader, "broker", None)
    npos = len(getattr(broker, "positions", {}) or {})
    if npos >= max_open:
        return False, f"max_open_positions {max_open}"
    if getattr(rs, "open_positions", 0) >= max_open:
        return False, f"max_open_positions(risk) {max_open}"

    held = (getattr(broker, "positions", {}) or {}).get(sym)
    same_side = (held is not None and int(getattr(held, "quantity", 0) or 0) > 0 and (
        (action == "SELL" and getattr(held, "side", "") == "SHORT")
        or (action == "BUY" and getattr(held, "side", "") == "LONG")))
    if same_side:
        return False, "already-holding"
    if action == "SELL" and not allow_shorts:
        held_qty = int(getattr(held, "quantity", 0) or 0) \
            if held is not None and getattr(held, "side", "") == "LONG" else 0
        if held_qty <= 0:
            return False, "no-holdings"

    base = dict(proposed if proposed is not None else _proposed_view(trader))
    try:
        if would_breach_sector(sym, qty, entry, existing=base):
            return False, "sector-exposure"
    except Exception:
        pass

    try:
        bal = broker.get_balance() if broker is not None else {}
        if float(bal.get("available", 1)) <= 0:
            return False, "margin-exhausted"
    except Exception:
        pass
    return True, ""


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------

def submit_candidate(trader, candidate: Dict[str, Any],
                     reason: str = "order",
                     proposed: Optional[Dict[str, Any]] = None,
                     cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Single entry path. Returns {"status","record"|"reason"}.

    Updates the transaction-like `proposed` map on confirmed fills so the
    next candidate in the same pass sees the new exposure (atomic sector).
    """
    ok, why = preflight(trader, candidate, proposed=proposed, cfg=cfg)
    if not ok:
        try:
            trader._log_event("skip", {"symbol": candidate.get("symbol"),
                                       "action": candidate.get("action"),
                                       "reason": why})
        except Exception:
            pass
        return {"status": "SKIP", "reason": why}

    sym = str(candidate.get("symbol", ""))
    action = str(candidate.get("action", ""))
    broker = getattr(trader, "broker", None)
    held = (getattr(broker, "positions", {}) or {}).get(sym)
    qty = int(candidate.get("position_size") or 0)
    # SELL without shorts and without holdings is never submitted — the
    # CNC settlement rule. (preflight already returned no-holdings for it;
    # this is the belt-and-braces submit-side guard.)
    _cfg = cfg if isinstance(cfg, dict) else load_config()
    if action == "SELL" and not bool(_cfg.get("trading", {}).get("allow_shorts", False)):
        held_qty = int(getattr(held, "quantity", 0) or 0) \
            if held is not None and getattr(held, "side", "") == "LONG" else 0
        if held_qty <= 0:
            return {"status": "SKIP", "reason": "no-holdings"}
        if held_qty > 0:
            qty = min(qty, held_qty)

    try:
        order = broker.place_order(
            symbol=sym, side=action, quantity=qty,
            price=candidate.get("entry_price"),
            stop_loss=candidate.get("stop_loss"), target=candidate.get("target"),
            reason=reason)
    except Exception as exc:
        logger.error("submit %s: broker raised: %s", sym, exc)
        try:
            trader._log_event("skip", {"symbol": sym, "action": action,
                                       "reason": f"broker-error: {exc}"})
        except Exception:
            pass
        return {"status": "ERROR", "reason": str(exc)}

    status = str(getattr(order, "status", "") or "").upper()
    if status in _PENDING or "UNKNOWN" in status:
        try:
            from src.risk_manager import set_broker_uncertain
            set_broker_uncertain(f"{sym} submitted but unconfirmed ({status})")
        except Exception:
            pass
        try:
            trader._log_event("skip", {"symbol": sym, "action": action,
                                       "reason": f"pending: {status}"})
        except Exception:
            pass
        return {"status": "PENDING", "reason": status}
    if status != "FILLED":
        try:
            trader._log_event("skip", {"symbol": sym, "action": action,
                                       "reason": f"rejected: {getattr(order, 'error', '')}"})
        except Exception:
            pass
        return {"status": "REJECTED", "reason": str(getattr(order, "error", ""))}

    record = record_confirmed_fill(trader, candidate, order, qty)
    # Transaction-like exposure update for the NEXT candidate in this pass.
    try:
        if proposed is not None and isinstance(proposed, dict):
            proposed[sym] = {"size": qty, "entry": float(candidate.get("entry_price") or 0.0)}
        cur = getattr(trader, "_proposed", None)
        if isinstance(cur, dict):
            cur[sym] = {"size": qty, "entry": float(candidate.get("entry_price") or 0.0)}
    except Exception:
        pass
    return {"status": "FILLED", "record": record}


def record_confirmed_fill(trader, candidate: Dict[str, Any],
                          order: Any, qty: int) -> Dict[str, Any]:
    """Register a CONFIRMED fill: risk + audit + alert. Alert fires once."""
    from src.risk_manager import add_position as _add
    sym = str(candidate.get("symbol", ""))
    try:
        _add(sym, qty, float(candidate.get("entry_price") or 0.0),
             float(candidate.get("risk_amount") or 0.0))
    except Exception:
        pass
    record = {
        "symbol": sym, "action": str(candidate.get("action", "")),
        "entry": candidate.get("entry_price"), "stop": candidate.get("stop_loss"),
        "target": candidate.get("target"), "qty": qty,
        "rank": candidate.get("rank"), "setup": candidate.get("setup_name"),
        "confidence": int(candidate.get("confidence") or 0),
        "order_id": getattr(order, "order_id", ""),
    }
    try:
        trader._log_event("order", record)
    except Exception:
        pass
    try:
        from src.alerts import send_signal_alert
        send_signal_alert({**candidate, "order_id": getattr(order, "order_id", "")})
    except Exception as exc:
        logger.warning("Entry alert failed for %s: %s", sym, exc)
    return record


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------

def record_confirmed_close(trader, symbol: str,
                           record: Dict[str, Any]) -> Dict[str, Any]:
    """Single close path: realized pnl + consec + position + halts + log + alert-once."""
    if not record:
        return {}
    try:
        from src.risk_manager import close_position as _rm_close, update_pnl as _rm_pnl
        _rm_pnl(float(record.get("pnl") or 0.0))
        # partial exits keep the runner visible — only full closes pop the key
        try:
            broker = getattr(trader, "broker", None)
            still_open = (getattr(broker, "positions", {}) or {}).get(symbol)
            if still_open is None or int(getattr(still_open, "quantity", 0) or 0) <= 0:
                _rm_close(symbol)
            else:
                # refresh counts from the mirror for partials
                from src.risk_manager import get_risk_state as _grs
                _grs().open_positions = len(getattr(broker, "positions", {}) or {})
        except Exception:
            _rm_close(symbol)
    except Exception:
        pass
    try:
        trader._log_event("close", record)
    except Exception:
        pass
    # The broker already fired the exit alert via _record_close; do NOT
    # re-fire here — exactly-once alerting lives in the broker.
    return record
