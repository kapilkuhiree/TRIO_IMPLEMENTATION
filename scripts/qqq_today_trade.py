"""QQQ live check: is there a trade on, and what is the P&L?

Runs the canonical QQQ rule on the freshest daily bars and reports:

* the indicator state for the last few sessions,
* whether a position is currently open (and its unrealised P&L on $100,000),
* whether the latest close generated an entry or exit,
* the historical outcome distribution for trades with a similar entry RSI,
  so the expected P&L of a fresh signal can be sized honestly.

    python scripts/qqq_today_trade.py
    python scripts/qqq_today_trade.py --refresh
    python scripts/qqq_today_trade.py --notional 100000

DISCLAIMER: Educational purposes only. Not financial advice.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest_qqq_mean_reversion import (  # noqa: E402
    DEFAULT_CACHE,
    DEFAULT_TRADES,
    Trade,
    adjusted_bars,
    build_signals,
    load_qqq,
    run_backtest,
    summarize,
)

DEFAULT_JSON = ROOT / "output" / "qqq_today_trade.json"


def replay_position(signals: pd.DataFrame) -> Optional[int]:
    """Index of the bar that opened the still-open position, else None."""
    open_at: Optional[int] = None
    for position in range(len(signals)):
        if open_at is None and bool(signals["entry"].iloc[position]):
            open_at = position
        elif open_at is not None and bool(signals["exit"].iloc[position]):
            open_at = None
    return open_at


def conditional_stats(trades: Sequence[Trade], *, rsi_max: float,
                      notional: float) -> Dict[str, Any]:
    subset = [trade for trade in trades if trade.entry_rsi <= rsi_max]
    if not subset:
        return {"trades": 0}
    returns = np.array([trade.return_pct for trade in subset], dtype=float)
    return {
        "trades": len(subset),
        "win_rate": round(float((returns > 0).mean() * 100.0), 1),
        "mean_pct": round(float(returns.mean()), 3),
        "median_pct": round(float(np.median(returns)), 3),
        "p10_pct": round(float(np.percentile(returns, 10)), 3),
        "p90_pct": round(float(np.percentile(returns, 90)), 3),
        "worst_pct": round(float(returns.min()), 3),
        "best_pct": round(float(returns.max()), 3),
        "mean_pnl": round(float(returns.mean() / 100.0 * notional), 2),
        "median_pnl": round(float(np.median(returns) / 100.0 * notional), 2),
        "p10_pnl": round(float(np.percentile(returns, 10) / 100.0 * notional), 2),
        "p90_pnl": round(float(np.percentile(returns, 90) / 100.0 * notional), 2),
        "worst_pnl": round(float(returns.min() / 100.0 * notional), 2),
        "best_pnl": round(float(returns.max() / 100.0 * notional), 2),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true",
                        help="re-download QQQ daily bars first")
    parser.add_argument("--notional", type=float, default=100_000.0)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    args = parser.parse_args(argv)

    frame = load_qqq(args.cache, refresh=args.refresh)
    bars = adjusted_bars(frame, adjusted=False)  # canonical raw bars
    signals = build_signals(bars)
    trades = run_backtest(bars, signals, exit_timing="next_open",
                          cost_bps=0.0, capital=args.notional)

    view = bars.tail(6).copy()
    view["rsi3"] = signals["rsi"].tail(6)
    view["ema100"] = signals["ema"].tail(6)
    view["ema_rising"] = signals["ema_rising"].tail(6)
    view["vwap10"] = signals["vwap"].tail(6)
    view["vs_vwap_pct"] = (view["Close"] / view["vwap10"] - 1.0) * 100.0
    view["entry"] = signals["entry"].tail(6)
    view["exit"] = signals["exit"].tail(6)

    print(f"QQQ bars: {len(bars)} | last {bars.index[-1].date()} | "
          f"notional ${args.notional:,.0f}")
    print(f"\nLAST 6 SESSIONS")
    print("=" * 104)
    header = (f"{'Date':<12}{'Close':>10}{'RSI3':>7}{'EMA up':>8}{'VWAP10':>10}"
              f"{'vs VWAP':>9}{'Entry':>7}{'Exit':>6}")
    print(header)
    print("-" * len(header))
    for stamp, row in view.iterrows():
        print(f"{stamp.date().isoformat():<12}{row['Close']:>10,.2f}"
              f"{row['rsi3']:>7.1f}{'yes' if row['ema_rising'] else 'no':>8}"
              f"{row['vwap10']:>10,.2f}{row['vs_vwap_pct']:>8.1f}%"
              f"{'YES' if row['entry'] else '-':>7}{'YES' if row['exit'] else '-':>6}")

    latest = bars.index[-1]
    open_at = replay_position(signals)
    payload: Dict[str, Any] = {
        "last_bar": latest.date().isoformat(),
        "notional": args.notional,
        "close": round(float(bars["Close"].iloc[-1]), 2),
        "rsi3": round(float(signals["rsi"].iloc[-1]), 2),
        "entry_signal": bool(signals["entry"].iloc[-1]),
        "exit_signal": bool(signals["exit"].iloc[-1]),
        "position": None,
        "last_trades": [
            {
                "signal_date": trade.signal_date,
                "entry_date": trade.entry_date,
                "exit_date": trade.exit_date,
                "entry_price": round(trade.entry_price, 2),
                "exit_price": round(trade.exit_price, 2),
                "return_pct": round(trade.return_pct, 3),
                "pnl": round(trade.pnl, 2),
                "bars_held": trade.bars_held,
                "entry_rsi": round(trade.entry_rsi, 1),
            }
            for trade in trades[-10:]
        ],
    }

    print("\nPOSITION")
    print("=" * 104)
    if open_at is not None:
        entry_bar = bars.iloc[open_at + 1] if open_at + 1 < len(bars) else None
        entry_price = float(entry_bar["Open"]) if entry_bar is not None else float("nan")
        entry_date = bars.index[open_at + 1].date().isoformat() if entry_bar is not None else "?"
        close = float(bars["Close"].iloc[-1])
        pnl = (close / entry_price - 1.0) * args.notional
        print(f"OPEN  since {entry_date} at ${entry_price:,.2f} | "
              f"close ${close:,.2f} | unrealised ${pnl:,.2f} "
              f"({(close / entry_price - 1) * 100:+.2f}%)")
        payload["position"] = {
            "entry_date": entry_date,
            "entry_price": round(entry_price, 2),
            "close": round(close, 2),
            "unrealised_pnl": round(pnl, 2),
            "return_pct": round((close / entry_price - 1) * 100, 3),
        }
    else:
        print("FLAT  no open QQQ position; the last trade closed on "
              f"{trades[-1].exit_date if trades else 'n/a'}.")

    print("\nLAST 10 TRADES")
    print("=" * 104)
    header = (f"{'Entry':<12}{'Exit':<12}{'In':>10}{'Out':>10}{'Ret%':>8}"
              f"{'P&L $':>12}{'Days':>6}{'RSI':>7}")
    print(header)
    print("-" * len(header))
    for trade in trades[-10:]:
        print(f"{trade.entry_date:<12}{trade.exit_date:<12}"
              f"{trade.entry_price:>10,.2f}{trade.exit_price:>10,.2f}"
              f"{trade.return_pct:>8.2f}{trade.pnl:>12,.0f}"
              f"{trade.bars_held:>6}{trade.entry_rsi:>7.1f}")

    ytd = [trade for trade in trades if trade.exit_date >= "2026-01-01"]
    ytd_pnl = sum(trade.pnl for trade in ytd)
    print(f"\n2026 realised: {len(ytd)} trades, "
          f"{sum(1 for t in ytd if t.pnl > 0)} wins, "
          f"net ${ytd_pnl:,.2f}")
    payload["ytd"] = {"trades": len(ytd),
                      "wins": sum(1 for t in ytd if t.pnl > 0),
                      "net_pnl": round(ytd_pnl, 2)}

    print("\nWHAT A FRESH SIGNAL HAS HISTORICALLY PAID (same rules, raw bars)")
    print("=" * 104)
    header = (f"{'Entry RSI <= ':>14}{'Trades':>8}{'Win%':>7}{'Mean%':>8}"
              f"{'Median%':>9}{'Mean $':>11}{'Median $':>11}{'P10 $':>11}"
              f"{'P90 $':>11}{'Worst $':>11}")
    print(header)
    print("-" * len(header))
    buckets = [10, 12, 15, 18, 20, 22]
    conditional = {}
    for rsi_max in buckets:
        stats = conditional_stats(trades, rsi_max=rsi_max,
                                  notional=args.notional)
        conditional[str(rsi_max)] = stats
        if not stats["trades"]:
            continue
        print(f"{rsi_max:>14}{stats['trades']:>8}{stats['win_rate']:>6.1f}%"
              f"{stats['mean_pct']:>8.2f}{stats['median_pct']:>9.2f}"
              f"{stats['mean_pnl']:>11,.0f}{stats['median_pnl']:>11,.0f}"
              f"{stats['p10_pnl']:>11,.0f}{stats['p90_pnl']:>11,.0f}"
              f"{stats['worst_pnl']:>11,.0f}")
    payload["conditional"] = conditional

    overall = summarize(trades, capital=args.notional)
    payload["overall"] = {key: overall[key] for key in
                          ("trades", "win_rate", "profit_factor",
                           "avg_trade_return_pct", "net_pnl")}
    print(f"\nFull sample: {overall['trades']} trades, "
          f"win {overall['win_rate']:.1f}%, PF {overall['profit_factor']:.2f}, "
          f"avg {overall['avg_trade_return_pct']:.2f}% "
          f"(${overall['avg_trade_return_pct'] / 100 * args.notional:,.0f} "
          f"per trade at ${args.notional:,.0f})")

    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(payload, indent=2, default=str),
                         encoding="utf-8")
    print(f"\nSaved JSON: {args.json}")
    print(f"Trade ledger: {DEFAULT_TRADES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
