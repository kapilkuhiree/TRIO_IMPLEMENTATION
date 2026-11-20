"""NIFTY-only backtest of the staged profit-lock ladder (T1..T4).

Compares four exit policies on the exact live entries (ORB, ORB-Retest,
Mean Reversion), on NIFTY alone:

* ``flat``          - the current evidence model: stop -20%, target +30%, 15:10
* ``t1_be``         - stop to breakeven once the bid reaches +10%
* ``t1_t4``         - the runtime ``profit_lock_rungs`` ratchet: [10, 15, 20, 25]
                      (full position, stop only, no partial exits)
* ``t1_t4_partial`` - 25% sold at each of +10/+15/+20/+25, stop trails to the
                      previous rung, remainder runs to +30%

Everything else matches ``scripts/backtest_orb_retest_v2.py``: ATM strike,
Black-Scholes 5 DTE at 18% IV, real NIFTY lot size, Rs 7,000 risk/trade,
Rs 18,000 premium cap, NSE costs + one tick slippage per side, entry on the
next bar's open, hard flat 15:10, 14:30 entry cutoff.

Two fill assumptions are reported:

* ``close``     - stops/targets/rungs only trigger on the 5m close premium.
                  This is what every earlier result used.
* ``intrabar``  - stops fill on the bar's low premium, rungs and the target on
                  its high premium, stop-first when a bar spans both.  More
                  honest for a trailing stop, slightly kind to limit targets.

    python scripts/backtest_nifty_ladder.py
    python scripts/backtest_nifty_ladder.py --json output/nifty_ladder.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest_active_wide import nfo_universe, to_candles  # noqa: E402
from scripts.backtest_orb_retest_v2 import (  # noqa: E402
    HARD_FLAT,
    IST,
    STOP_FRACTION,
    TARGET_FRACTION,
    collect_mr_signals,
    collect_orb_signals,
    collect_retest_signals,
    lots_for,
    minute_of_day,
    option_costs,
    premium,
    print_table,
    select_portfolio,
    stats,
)

DATA = ROOT / "output" / "research_5m_wide" / "NIFTY.pkl"
ENTRY_CUTOFF_MIN = 315  # 14:30, same as the rest of the evidence

STRATEGY_PARAMS = {
    "orb": {"earliest_min": 45, "cooldown": 300, "cooldown_rule": "loss"},
    "orb_retest": {"earliest_min": 30, "cooldown": 0, "cooldown_rule": "any"},
    "mean_reversion": {"earliest_min": 15, "cooldown": 300,
                       "cooldown_rule": "loss"},
}
STRATEGIES = ("orb", "orb_retest", "mean_reversion")
PRIORITY = {"orb_retest": 0, "mean_reversion": 1, "orb": 2}

MODELS: Dict[str, Dict[str, Any]] = {
    "flat": {"kind": "flat", "rungs": [], "label": "flat -20%/+30% (baseline)"},
    "t1_be": {"kind": "ratchet", "rungs": [(0.10, 0.00)],
              "label": "T1 only: BE stop at +10%"},
    "t1_t4": {"kind": "ratchet",
              "rungs": [(0.10, 0.10), (0.15, 0.15),
                        (0.20, 0.20), (0.25, 0.25)],
              "label": "T1-T4 ratchet (runtime)"},
    "t1_t4_partial": {"kind": "partial", "frac": 0.25,
                      "rungs": [0.10, 0.15, 0.20, 0.25],
                      "label": "T1-T4 25% each (scale-out)"},
}


# ---------------------------------------------------------------------------
# Exit simulation
# ---------------------------------------------------------------------------

def _partial_costs(entry_prem: float, fills: List[Tuple[float, float]],
                   lot: int, lots: int) -> float:
    """Costs when the position is sold in several slices."""
    qty_total = lot * lots
    brokerage = 20.0                     # entry
    stt = exchange = sebi = slip = 0.0
    stamp = 0.00003 * entry_prem * qty_total
    slip += 0.05 * qty_total             # entry-side tick
    for frac, px in fills:
        qty = qty_total * frac
        buy_value = entry_prem * qty
        sell_value = px * qty
        turnover = buy_value + sell_value
        brokerage += 20.0
        stt += 0.001 * sell_value
        exchange += 0.0003503 * turnover
        sebi += 0.000001 * turnover
        slip += 0.05 * qty               # exit-side tick
    gst = 0.18 * (brokerage + exchange + sebi)
    return brokerage + stt + exchange + sebi + gst + stamp + slip


def build_trade(candles: List[Dict[str, Any]], index: int, kind: str,
                meta: Dict[str, Any], model: Dict[str, Any],
                fill: str) -> Optional[Dict[str, Any]]:
    """One option trade with a configurable exit policy."""
    if index + 1 >= len(candles):
        return None
    entry_bar = candles[index + 1]
    day = entry_bar["timestamp"][:10]
    spot = float(entry_bar["open"])
    if spot <= 0:
        return None
    lot, step, vol = meta["lot"], meta["step"], meta["vol"]
    strike = round(spot / step) * step
    entry_prem = premium(spot, strike, kind, vol)
    lots = lots_for(entry_prem, lot)
    if entry_prem <= 0 or lots <= 0:
        return None
    qty = lot * lots
    target = entry_prem * (1.0 + TARGET_FRACTION)
    stop = entry_prem * (1.0 - STOP_FRACTION)
    rungs = model.get("rungs") or []
    stage = 0
    is_partial = model["kind"] == "partial"
    remaining = 1.0
    fills: List[Tuple[float, float]] = []
    reason = "eod"
    exit_idx, exit_ts = index + 1, entry_bar["timestamp"]
    last_px = entry_prem

    for j in range(index + 2, len(candles)):
        bar = candles[j]
        if bar["timestamp"][:10] != day:
            break
        if pd.Timestamp(bar["timestamp"]).time() > HARD_FLAT:
            break
        close_px = premium(float(bar["close"]), strike, kind, vol)
        if fill == "intrabar":
            low_px = premium(float(bar["low"]), strike, kind, vol)
            high_px = premium(float(bar["high"]), strike, kind, vol)
        else:
            low_px = high_px = close_px
        exit_idx, exit_ts = j, bar["timestamp"]

        # 1. stop first (conservative when a bar spans both sides)
        if low_px <= stop:
            px = low_px
            if is_partial:
                fills.append((remaining, px))
                remaining = 0.0
            else:
                last_px = px
            reason = "trailing-stop" if stop > entry_prem + 1e-9 else "stop"
            break

        # 2. final target
        if high_px >= target:
            px = target if fill == "intrabar" else close_px
            if is_partial:
                fills.append((remaining, px))
                remaining = 0.0
            else:
                last_px = px
            reason = "target"
            break

        # 3. advance the ladder
        while stage < len(rungs):
            rung = rungs[stage]
            trigger = rung[0] if isinstance(rung, tuple) else rung
            if high_px < entry_prem * (1.0 + trigger):
                break
            if is_partial:
                frac = min(model["frac"], remaining)
                fills.append((frac, entry_prem * (1.0 + trigger)))
                remaining -= frac
                # stop trails to the previous rung, breakeven after T1
                stop = max(stop, entry_prem * (1.0 + (rungs[stage - 1] if stage else 0.0)))
            else:
                stop = max(stop, entry_prem * (1.0 + rung[1]))
            stage += 1
            if is_partial and remaining <= 1e-9:
                break
        last_px = close_px
        if is_partial and remaining <= 1e-9:
            break

    if is_partial:
        if remaining > 1e-9:
            fills.append((remaining, last_px))
        gross = sum(frac * (px - entry_prem) for frac, px in fills) * qty
        costs = _partial_costs(entry_prem, fills, lot, lots)
        exit_prem = sum(frac * px for frac, px in fills)
    else:
        gross = (last_px - entry_prem) * qty
        costs = option_costs(entry_prem, last_px, lot, lots)
        exit_prem = last_px

    return {
        "day": day,
        "symbol": meta["name"],
        "kind": kind,
        "entry_ts": entry_bar["timestamp"],
        "exit_ts": exit_ts,
        "entry_idx": index + 1,
        "exit_idx": exit_idx,
        "entry_premium": round(entry_prem, 2),
        "exit_premium": round(exit_prem, 2),
        "strike": strike,
        "lots": lots,
        "qty": qty,
        "reason": reason,
        "gross_pnl": round(gross, 2),
        "costs": round(costs, 2),
        "pnl": round(gross - costs, 2),
    }


# ---------------------------------------------------------------------------
# Signal stream -> trades (same rules as run_variant)
# ---------------------------------------------------------------------------

def run_strategy(candles, by_date, signals, meta, *, strategy: str,
                 model: Dict[str, Any], fill: str,
                 max_trades_per_day: int = 10) -> List[Dict[str, Any]]:
    params = STRATEGY_PARAMS[strategy]
    earliest_min = params["earliest_min"]
    cooldown = params["cooldown"]
    cooldown_rule = params["cooldown_rule"]
    trades: List[Dict[str, Any]] = []

    for day, indices in sorted(by_date.items()):
        open_trade: Optional[Dict[str, Any]] = None
        last_trade: Optional[Dict[str, Any]] = None
        taken_today = 0

        for i in indices:
            if open_trade is not None and open_trade["exit_idx"] <= i:
                last_trade, open_trade = open_trade, None
            if open_trade is not None:
                continue
            if taken_today >= max_trades_per_day:
                continue
            kind = signals.get(i)
            if kind not in ("CE", "PE"):
                continue
            if i + 1 >= len(candles) or candles[i + 1]["timestamp"][:10] != day:
                continue
            entry_ts = pd.Timestamp(candles[i + 1]["timestamp"])
            entry_minute = minute_of_day(entry_ts)
            if entry_minute < earliest_min or entry_minute > ENTRY_CUTOFF_MIN:
                continue
            if last_trade is not None and cooldown > 0:
                elapsed = (entry_ts - pd.Timestamp(last_trade["exit_ts"])).total_seconds()
                if elapsed < cooldown and not (
                        cooldown_rule == "loss" and last_trade["pnl"] >= 0):
                    continue
            trade = build_trade(candles, i, kind, meta, model, fill)
            if trade is None:
                continue
            trade["strategy"] = strategy
            trades.append(trade)
            open_trade, last_trade = trade, trade
            taken_today += 1

    return trades


def select_multi(candidates: List[Dict[str, Any]], *, max_open: int = 2,
                 max_entries: int = 10,
                 cooldowns: Optional[Dict[str, int]] = None,
                 allow_same_symbol: bool = True) -> List[Dict[str, Any]]:
    """Live-style portfolio on one underlying.

    ``select_portfolio`` blocks two open positions on the same symbol, which is
    right for a 200-name book but wrong for a NIFTY-only book: live allows two
    slots on NIFTY when they come from *different* strategies
    (``max_correlated_positions: 2``).  This keeps that rule and still forbids
    a strategy pyramiding into itself.
    """
    cooldowns = cooldowns or {}
    ordered = sorted(candidates, key=lambda t: (
        pd.Timestamp(t["entry_ts"]), PRIORITY.get(t["strategy"], 9)))
    opened: List[Dict[str, Any]] = []
    entries_by_day: Dict[str, int] = defaultdict(int)
    last_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    accepted: List[Dict[str, Any]] = []

    for trade in ordered:
        entry_ts = pd.Timestamp(trade["entry_ts"])
        opened = [p for p in opened if p["exit_ts"] > entry_ts]
        day = trade["day"]
        if entries_by_day[day] >= max_entries:
            continue
        if len(opened) >= max_open:
            continue
        if not allow_same_symbol and any(p["symbol"] == trade["symbol"]
                                         for p in opened):
            continue
        if any(p["symbol"] == trade["symbol"]
               and p["strategy"] == trade["strategy"] for p in opened):
            continue
        key = (trade["strategy"], trade["symbol"])
        cooldown = cooldowns.get(trade["strategy"], 0)
        last = last_by_key.get(key)
        if last is not None and cooldown > 0 and last["pnl"] < 0:
            if (entry_ts - pd.Timestamp(last["exit_ts"])).total_seconds() < cooldown:
                continue
        accepted.append(trade)
        opened.append({"symbol": trade["symbol"], "strategy": trade["strategy"],
                       "exit_ts": pd.Timestamp(trade["exit_ts"])})
        entries_by_day[day] += 1
        last_by_key[key] = trade
    return accepted


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def month_rows(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_month: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        by_month[trade["day"][:7]].append(trade)
    return [stats(group, month) for month, group in sorted(by_month.items())]


def reason_mix(trades: List[Dict[str, Any]]) -> str:
    counts = Counter(t["reason"] for t in trades)
    total = max(1, len(trades))
    return "  ".join(f"{k}={v} ({v / total:.0%})" for k, v in
                     sorted(counts.items(), key=lambda kv: -kv[1]))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path,
                        default=ROOT / "output" / "nifty_ladder.json")
    parser.add_argument("--strategy", default="all",
                        choices=["all", *STRATEGIES])
    args = parser.parse_args(argv)

    universe = nfo_universe()
    if "NIFTY" not in universe:
        raise SystemExit("NIFTY missing from the cached NFO dump")
    meta = universe["NIFTY"]
    meta["name"] = "NIFTY"
    frame = pd.read_pickle(DATA)
    candles = to_candles(frame, "NIFTY", meta["kind"])
    by_date: Dict[str, List[int]] = defaultdict(list)
    for i, candle in enumerate(candles):
        by_date[candle["timestamp"][:10]].append(i)
    print(f"NIFTY bars: {len(candles)}  sessions: {len(by_date)}  "
          f"lot: {meta['lot']}  step: {meta['step']}  window: "
          f"{min(by_date)} .. {max(by_date)}")

    wanted = STRATEGIES if args.strategy == "all" else (args.strategy,)
    print("collecting signals...")
    signals = {}
    if "orb" in wanted:
        signals["orb"] = collect_orb_signals(
            candles, "NIFTY", meta["lot"], meta["step"])
    if "orb_retest" in wanted:
        signals["orb_retest"] = collect_retest_signals(
            candles, "NIFTY", meta["lot"], meta["step"],
            or_min_width_atr=3.0, max_breakout_body=0.90,
            resumption_buffer_atr=0.20, atr_pct_max=0.30)
    if "mean_reversion" in wanted:
        signals["mean_reversion"] = collect_mr_signals(candles)
    print("signals: " + ", ".join(f"{k}={len(v)}" for k, v in signals.items()))

    payload: Dict[str, Any] = {
        "generated_at": pd.Timestamp.now(tz=IST).isoformat(),
        "symbol": "NIFTY",
        "bars": len(candles),
        "sessions": len(by_date),
        "lot": meta["lot"],
        "strike_step": meta["step"],
        "signals": {k: len(v) for k, v in signals.items()},
        "results": {},
    }

    for fill in ("close", "intrabar"):
        print(f"\n{'=' * 118}\nFILL MODEL: {fill}\n{'=' * 118}")
        books: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        for name, model in MODELS.items():
            raw = {s: run_strategy(candles, by_date, signals[s], meta,
                                   strategy=s, model=model, fill=fill)
                   for s in wanted}
            capped = {
                s: select_portfolio(
                    raw[s], max_open=2, max_entries=10,
                    cooldowns={s: STRATEGY_PARAMS[s]["cooldown"]})
                for s in wanted
            }
            combined = select_multi(
                [t for s in wanted for t in raw[s]],
                max_open=2, max_entries=10,
                cooldowns={s: STRATEGY_PARAMS[s]["cooldown"] for s in wanted})
            books[name] = {**capped, "combined": combined}

            rows = [stats(capped[s], f"{name} | {s}") for s in wanted]
            rows.append(stats(combined, f"{name} | COMBINED (2 slots)"))
            print_table(rows, f"EXIT POLICY: {model['label']}  [{fill}]")
            print(f"  exit mix (combined): {reason_mix(combined)}")

        print(f"\nMONTHLY NET P&L (Rs) - combined, {fill}")
        print("-" * 100)
        header = f"{'Policy':<22}" + "".join(f"{m:>14}" for m in
                                             sorted({t['day'][:7] for b in books.values()
                                                     for t in b['combined']}))
        print(header)
        print("-" * 100)
        for name in MODELS:
            by_month = {r["label"]: r for r in month_rows(books[name]["combined"])}
            months = sorted(by_month)
            cells = "".join(f"{by_month[m]['net']:>14,.0f}" for m in months)
            print(f"{name:<22}{cells}")
        print("-" * 100)

        payload["results"][fill] = {}
        for name, book in books.items():
            payload["results"][fill][name] = {
                "label": MODELS[name]["label"],
                "stats": {k: stats(book[k], k) for k in book},
                "by_month_combined": month_rows(book["combined"]),
                "by_symbol_combined": [stats(book["combined"], "NIFTY")],
                "exit_mix_combined": dict(Counter(
                    t["reason"] for t in book["combined"])),
                "trades_combined": book["combined"],
                "trades_by_strategy": {k: book[k] for k in wanted},
            }

        # Same entry, different exit: the ladder changes both the exit and the
        # trade sequence (a profitable trailing exit does not arm the loss-only
        # cooldown, so the signal can re-enter).  This block holds the baseline
        # entries fixed so the exit policy is measured on its own.
        def resimulate(trades: List[Dict[str, Any]],
                       model: Dict[str, Any]) -> List[Dict[str, Any]]:
            out = []
            for t in trades:
                nt = build_trade(candles, t["entry_idx"] - 1, t["kind"],
                                 meta, model, fill)
                if nt is not None:
                    nt["strategy"] = t["strategy"]
                    out.append(nt)
            return out

        base = books["flat"]
        rows = []
        counter_monthly: Dict[str, List[Dict[str, Any]]] = {}
        for name, model in MODELS.items():
            for key in (*wanted, "combined"):
                res = resimulate(base[key], model)
                rows.append(stats(res, f"{name} | {key}"))
            counter_monthly[name] = month_rows(resimulate(base["combined"], model))
        print_table(rows, f"SAME ENTRY, ONLY THE EXIT CHANGES [{fill}]")

        print(f"\nMONTHLY NET P&L (Rs) - same entries, exit policy only, {fill}")
        print("-" * 100)
        months_cf = [r["label"] for r in counter_monthly["flat"]]
        print(f"{'Policy':<22}" + "".join(f"{m:>14}" for m in months_cf))
        print("-" * 100)
        for name in MODELS:
            index = {r["label"]: r for r in counter_monthly[name]}
            print(f"{name:<22}" + "".join(
                f"{index[m]['net']:>14,.0f}" for m in months_cf))
        print("-" * 100)

        payload["results"][fill]["counterfactual"] = {
            name: {
                "by_month_combined": counter_monthly[name],
                "stats": {key: stats(resimulate(base[key], model), key)
                          for key in (*wanted, "combined")},
            }
            for name, model in MODELS.items()
        }

    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(payload, indent=2, default=str),
                         encoding="utf-8")
    print(f"\nSaved: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
