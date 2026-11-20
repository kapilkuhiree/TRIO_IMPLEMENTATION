"""Wide-sample research run for the SMC liquidity-sweep pattern.

The NIFTY/BANKNIFTY window only yields ~20 trades, which is far too few to
tell an improvement from noise.  This harness runs the *same* strategy over
every instrument in the 5m cache (indices + optionable stocks) so the pattern
can be judged on a sample large enough to have statistical power.

The headline statistic is the per-trade R multiple, which is independent of
lot size and option premium:

    R = signed (exit_spot - entry_spot) / (entry_spot - stop)

A pattern with no edge has mean R ~ 0.  Mean R > 0 with |t| > 2 across a few
hundred independent trades is real evidence; a handful of trades on one index
is not.

    python scripts/backtest_smc_wide.py
    python scripts/backtest_smc_wide.py --indices-only
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest_active_wide import nfo_universe, to_candles  # noqa: E402
from scripts.backtest_smc import MAX_TRADES_PER_DAY, simulate  # noqa: E402
from strategies.smc_liquidity_strategy import SmcLiquidityStrategy  # noqa: E402

DATA = ROOT / "output" / "research_5m_wide"
INDEXES = ("NIFTY", "BANKNIFTY", "FINNIFTY")


def r_multiple(trade: Dict[str, Any]) -> float:
    risk = abs(trade["entry_spot"] - trade["stop"])
    if risk <= 0:
        return 0.0
    if trade["variant"] == "bullish":
        return (trade["exit_spot"] - trade["entry_spot"]) / risk
    return (trade["entry_spot"] - trade["exit_spot"]) / risk


def summarise(trades: List[Dict[str, Any]], label: str) -> Dict[str, Any]:
    rs = [r_multiple(t) for t in trades]
    n = len(rs)
    if not n:
        return {"label": label, "trades": 0}
    mean = sum(rs) / n
    var = sum((r - mean) ** 2 for r in rs) / (n - 1) if n > 1 else 0.0
    se = math.sqrt(var / n) if n > 1 else 0.0
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    return {
        "label": label,
        "trades": n,
        "win_rate": 100.0 * len(wins) / n,
        "mean_r": mean,
        "median_r": sorted(rs)[n // 2],
        "t_stat": mean / se if se else 0.0,
        "net": sum(t["pnl"] for t in trades),
        "pf": gross_win / gross_loss if gross_loss else float("inf"),
        "r_win_rate": 100.0 * sum(1 for r in rs if r > 0) / n,
    }


def row(s: Dict[str, Any]) -> str:
    return (f"{s['label']:<22}{s['trades']:>7}{s['win_rate']:>8.1f}"
            f"{s['mean_r']:>8.2f}{s['median_r']:>8.2f}{s['t_stat']:>7.2f}"
            f"{s['net']:>11,.0f}{s['pf']:>7.2f}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--indices-only", action="store_true")
    parser.add_argument("--rr", type=float, default=2.0)
    parser.add_argument("--moneyness-steps", type=int, default=0)
    parser.add_argument("--min-premium", type=float, default=5.0,
                        help="tradeability floor on entry premium (Rs)")
    parser.add_argument("--fill", choices=("limit", "next_open"), default="limit")
    parser.add_argument("--json", type=Path,
                        default=ROOT / "output" / "smc_wide.json")
    args = parser.parse_args(argv)

    universe = nfo_universe()
    files = sorted(DATA.glob("*.pkl"))
    if args.indices_only:
        files = [f for f in files if f.stem in INDEXES]

    trades: List[Dict[str, Any]] = []
    by_name: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    funnel_total: Counter = Counter()
    failures: List[str] = []

    for path in files:
        name = path.stem
        meta = dict(universe.get(name, {}))
        if not meta:
            failures.append(f"{name}: not in NFO universe")
            continue
        meta["name"] = name
        try:
            frame = pd.read_pickle(path)
            candles = to_candles(frame, name, meta["kind"])
            strategy = SmcLiquidityStrategy()
            signals, funnel = strategy.scan_signals(candles, name)
            if not signals:
                continue
            funnel_total.update(funnel)
            if args.rr != 2.0:
                signals = {i: {**p, "risk_reward": args.rr}
                           for i, p in signals.items()}
            got = simulate(candles, signals, meta, exit_mode="underlying",
                           max_trades_per_day=MAX_TRADES_PER_DAY,
                           fill=args.fill,
                           moneyness_steps=args.moneyness_steps,
                           min_premium=args.min_premium)
        except Exception as exc:  # noqa: BLE001 - research run, keep going
            failures.append(f"{name}: {exc}")
            continue
        trades.extend(got)
        by_name[name].extend(got)

    print(f"instruments scanned: {len(files) - len(failures)}")
    print(f"instruments traded : {len(by_name)}")
    print(f"failures           : {len(failures)}")
    for f in failures[:10]:
        print("  " + f)
    print("funnel total: " + "  ".join(f"{k}={v}" for k, v in funnel_total.items()))

    headers = (f"{'group':<22}{'trades':>7}{'win%':>8}{'meanR':>8}"
               f"{'medR':>8}{'t':>7}{'net':>11}{'PF':>7}")
    print("\n" + headers)
    print("-" * len(headers))

    index_trades = [t for t in trades if t["symbol"] in INDEXES]
    stock_trades = [t for t in trades if t["symbol"] not in INDEXES]
    print(row(summarise(trades, "ALL")))
    print(row(summarise(index_trades, "indices")))
    print(row(summarise(stock_trades, "stocks")))

    print("\nby month")
    print(headers)
    print("-" * len(headers))
    by_month: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for t in trades:
        by_month[t["day"][:7]].append(t)
    for month in sorted(by_month):
        print(row(summarise(by_month[month], month)))

    print("\nby side / exit")
    print(headers)
    print("-" * len(headers))
    for side in ("bullish", "bearish"):
        print(row(summarise([t for t in trades if t["variant"] == side], side)))
    for reason in ("target", "stop", "eod"):
        print(row(summarise([t for t in trades if t["reason"] == reason], reason)))

    print("\nby year (stability)")
    print(headers)
    print("-" * len(headers))
    by_year: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for t in trades:
        by_year[t["day"][:4]].append(t)
    for year in sorted(by_year):
        print(row(summarise(by_year[year], year)))

    per_name = {name: summarise(group, name) for name, group in by_name.items()}
    positive = sum(1 for s in per_name.values() if s["net"] > 0)
    print(f"\ninstruments with positive net: {positive}/{len(per_name)} "
          f"({100.0 * positive / max(1, len(per_name)):.1f}%)")

    print("\nbest / worst instruments (min 3 trades)")
    eligible = [(n, s) for n, s in per_name.items() if s["trades"] >= 3]
    eligible.sort(key=lambda kv: kv[1]["mean_r"])
    for name, s in eligible[-5:][::-1]:
        print("  " + row(s))
    print("  ...")
    for name, s in eligible[:5]:
        print("  " + row(s))

    args.json.write_text(json.dumps({
        "config": {"rr": args.rr, "moneyness_steps": args.moneyness_steps,
                   "min_premium": args.min_premium, "fill": args.fill},
        "instruments": len(files) - len(failures),
        "traded": len(by_name),
        "funnel": dict(funnel_total),
        "all": summarise(trades, "ALL"),
        "indices": summarise(index_trades, "indices"),
        "stocks": summarise(stock_trades, "stocks"),
        "by_month": {m: summarise(g, m) for m, g in sorted(by_month.items())},
        "by_name": per_name,
        "trades": trades,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nSaved: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
