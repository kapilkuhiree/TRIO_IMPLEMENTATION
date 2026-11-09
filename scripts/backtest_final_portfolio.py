"""
Final portfolio backtest: the strategy actually shipped in
config/options_intraday.yaml vs the old shipped stack.

NEW (live policy)
  strategies   : orb + pullback (VWAP fixed) + mean_reversion
  filter       : CE only above session VWAP, PE only below (scan-level)
  entries      : one per symbol per day (no duplicate/correlated stacking)
  exits        : stop -20%, target +25%, completed bars, 15:10 flat

OLD (previous)
  strategies   : orb + momentum (pullback dormant due to volume bug)
  entries      : every signal, up to one per strategy per symbol per day
  exits        : stop -20%, target +30%

Usage:
    python scripts/backtest_final_portfolio.py [--days 60]
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest_strategy_compare import (
    IST, SYMBOLS, ENTRY_CUTOFF,
    FixedPullback, _vwap_with_fallback, fetch_5m, to_candles,
    run_trade, stats, print_table,
    MomentumContinuationStrategy, MeanReversionStrategy,
    OrbStrategy, scan,
)

STOP = 0.20


def _allowed_by_vwap(candles, i, kind) -> bool:
    vwap = _vwap_with_fallback(candles[:i + 1])
    if vwap is None:
        return False
    close = float(candles[i]["close"])
    if kind == "CE":
        return close >= vwap
    return close <= vwap


def _call(strategy, name, candles, i, lot, step, symbol):
    if name == "mean_reversion":
        return strategy.evaluate_signal(candles, i)
    return strategy.evaluate_signal(
        candles, i, {"lot_size": lot, "strike_step": step}, symbol)


def run_new_policy(df, meta, candles, by_date, today, target,
                   order=("orb", "pullback", "mean_reversion")) -> List[Dict[str, Any]]:
    available = {
        "orb": lambda: OrbStrategy(lot_size=meta["lot"]),
        "pullback": lambda: FixedPullback(lot_size=meta["lot"]),
        "mean_reversion": lambda: MeanReversionStrategy(),
    }
    strategies = {name: available[name]() for name in order if name in available}
    trades = []
    for day, indices in by_date.items():
        if pd.Timestamp(day).date() >= today:
            continue
        entered = False
        for i in indices:
            if entered or i >= len(candles):
                break
            ts = pd.Timestamp(candles[i]["timestamp"])
            if ts.time() < pd.Timestamp("09:30").time() or ts.time() > ENTRY_CUTOFF:
                continue
            for name in order:
                try:
                    sig = _call(strategies[name], name, candles, i,
                                meta["lot"], meta["step"], meta["name"])
                except Exception:
                    continue
                if not getattr(sig, "allowed", False):
                    continue
                kind = str(getattr(sig, "option_type", "") or "").upper()
                if kind not in {"CE", "PE"}:
                    continue
                if not _allowed_by_vwap(candles, i, kind):
                    continue
                trade = run_trade(candles, i, kind=kind, lot=meta["lot"],
                                  step=meta["step"], vol=meta["vol"],
                                  target_fraction=target)
                if trade is None:
                    continue
                trade.update(day=day, symbol=meta["name"], strategy=name)
                trades.append(trade)
                entered = True
                break
    return trades


def run_old_stack(df, meta, candles, by_date, today, target) -> List[Dict[str, Any]]:
    # Old ORB had no effective VWAP filter (volume=0 -> filter skipped).
    saved = scan._session_vwap
    scan._session_vwap = lambda candles: None
    try:
        orb = OrbStrategy(lot_size=meta["lot"])
        mom = MomentumContinuationStrategy(lot_size=meta["lot"])
        trades = []
        for day, indices in by_date.items():
            if pd.Timestamp(day).date() >= today:
                continue
            for name, strategy in (("orb", orb), ("momentum", mom)):
                done = False
                for i in indices:
                    if done:
                        break
                    ts = pd.Timestamp(candles[i]["timestamp"])
                    if ts.time() < pd.Timestamp("09:30").time() or ts.time() > ENTRY_CUTOFF:
                        continue
                    try:
                        sig = strategy.evaluate_signal(
                            candles, i,
                            {"lot_size": meta["lot"], "strike_step": meta["step"]},
                            meta["name"])
                    except Exception:
                        continue
                    if not getattr(sig, "allowed", False):
                        continue
                    kind = str(getattr(sig, "option_type", "") or "").upper()
                    if kind not in {"CE", "PE"}:
                        continue
                    trade = run_trade(candles, i, kind=kind, lot=meta["lot"],
                                      step=meta["step"], vol=meta["vol"],
                                      target_fraction=target)
                    if trade is None:
                        continue
                    trade.update(day=day, symbol=meta["name"], strategy=name)
                    trades.append(trade)
                    done = True
        return trades
    finally:
        scan._session_vwap = saved


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--priority", default="orb,pullback,mean_reversion",
                    help="comma-separated same-bar priority")
    ap.add_argument("--strategies", default="orb,pullback,mean_reversion",
                    help="comma-separated enabled strategies")
    args = ap.parse_args()
    order = tuple(x.strip() for x in args.priority.split(",") if x.strip())
    enabled = tuple(x.strip() for x in args.strategies.split(",") if x.strip())
    order = tuple(x for x in order if x in enabled)

    data = {}
    for key in SYMBOLS:
        df = fetch_5m(key, args.days)
        if df is not None and len(df) > 200:
            data[key] = df
    if not data:
        print("No data.")
        return 1

    today = pd.Timestamp.now(tz=IST).date()
    all_days = sorted({d for df in data.values() for d in df.index.date if d < today})
    last_week = set(all_days[-5:])
    print(f"Data: {all_days[0]} .. {all_days[-1]} ({len(all_days)} completed sessions)")
    print(f"Last week: {sorted(str(d) for d in last_week)}")

    new_trades: List[Dict[str, Any]] = []
    old_trades: List[Dict[str, Any]] = []

    for key, df in data.items():
        meta = SYMBOLS[key]
        candles = to_candles(df, meta["name"])
        by_date = defaultdict(list)
        for i, c in enumerate(candles):
            by_date[c["timestamp"][:10]].append(i)
        new_trades += run_new_policy(df, meta, candles, by_date, today, 0.25, order)
        old_trades += run_old_stack(df, meta, candles, by_date, today, 0.30)

    for label, trades in (("LAST WEEK", None), ("FULL WINDOW", None)):
        if label == "LAST WEEK":
            new_sub = [t for t in new_trades if pd.Timestamp(t["day"]).date() in last_week]
            old_sub = [t for t in old_trades if pd.Timestamp(t["day"]).date() in last_week]
        else:
            new_sub, old_sub = new_trades, old_trades
        rows = [stats(new_sub, "NEW (live policy)"), stats(old_sub, "OLD stack")]
        print_table(rows, f"{label} — NEW vs OLD")

    # Per-strategy contribution of the new policy (full window)
    rows = []
    for s in ("orb", "pullback", "mean_reversion"):
        rows.append(stats([t for t in new_trades if t["strategy"] == s], s))
    print_table(rows, "NEW POLICY — per-strategy contribution (full window)")

    # Target sensitivity for the new policy
    for tgt in (0.20, 0.25, 0.30):
        if tgt == 0.25:
            continue
        alt = []
        for key, df in data.items():
            meta = SYMBOLS[key]
            candles = to_candles(df, meta["name"])
            by_date = defaultdict(list)
            for i, c in enumerate(candles):
                by_date[c["timestamp"][:10]].append(i)
            alt += run_new_policy(df, meta, candles, by_date, today, tgt)
        print_table([stats(alt, f"NEW @ target {int(tgt*100)}%")],
                    f"NEW POLICY — target sensitivity (full window)")

    print("\nLast-week trade log (NEW policy):")
    print(f"{'Day':<12}{'Symbol':<11}{'Strategy':<15}{'Dir':>4}{'Entry':>9}"
          f"{'Exit':>9}{'Reason':>8}{'P&L':>11}")
    for t in sorted([t for t in new_trades
                     if pd.Timestamp(t['day']).date() in last_week],
                    key=lambda x: (x["day"], x["symbol"], x["entry_ts"])):
        print(f"{t['day']:<12}{t['symbol']:<11}{t['strategy']:<15}{t['kind']:>4}"
              f"{t['entry_premium']:>9.2f}{t['exit_premium']:>9.2f}"
              f"{t['reason']:>8}{t['pnl']:>11,.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
