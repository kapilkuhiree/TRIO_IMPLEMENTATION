"""Measure WHERE in the session entries come from and what they make.
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

For each trade in the 21-day NIFTY 15m replay: entry bar index (0 = 09:15),
exit reason, P&L. Then bucket by entry-bar to see if early-session entries
(first 2 bars = 09:15-09:45 noise window) lose money.

Usage: python scripts/entry_timing.py [--symbol ^NSEI] [--days 21]
"""
import argparse
import sys
from pathlib import Path
import logging
logging.disable(logging.CRITICAL)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import scripts.day_session as ds
from src.data_fetcher import fetch_market_data


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="^NSEI")
    ap.add_argument("--days", type=int, default=21)
    args = ap.parse_args()

    md = fetch_market_data(args.symbol, "15m", "60d")
    full = md.ohlcv
    days = ds.session_slices(full)[-args.days:]
    print(f"{args.symbol}: {len(days)} sessions\n")

    all_trades = []
    for sess in days:
        end_pos = full.index.get_loc(sess.index[-1])
        hist = full.iloc[max(0, end_pos - len(sess) - 300):end_pos - len(sess) + 1]
        r = ds.replay_day(sess, args.symbol, "15m",
                          ds.WINDOW_SETS["daily-windows"], warmup=hist)
        for t in r["details"]:
            all_trades.append({**t, "date": r["date"]})

    print(f"Total trades: {len(all_trades)}")
    print(f"\n{'bar':>4} {'time':>6} {'n':>4} {'wins':>5} {'win%':>7} {'total P&L':>10} {'avg P&L':>9}")
    print("-" * 60)
    buckets: dict = {}
    for t in all_trades:
        key = t.get("entry_bar", -1)
        buckets.setdefault(key, []).append(t["pnl"])
    for bar in sorted(k for k in buckets if k >= 0):
        pnls = buckets[bar]
        wins = sum(1 for p in pnls if p > 0)
        tot = round(sum(pnls), 2)
        # session bar 0 = 09:15, each bar = 15 min
        hh = 9 * 60 + 15 + bar * 15
        hhmm = f"{hh // 60:02d}:{hh % 60:02d}"
        print(f"{bar:>4} {hhmm:>6} {len(pnls):>4} {wins:>5} "
              f"{wins / len(pnls) * 100:>6.1f}% {tot:>10.2f} {tot / len(pnls):>9.2f}")

    # Early (bars 0-1, 09:15-09:45) vs rest
    early = [t["pnl"] for t in all_trades if t.get("entry_bar", 99) <= 1]
    rest = [t["pnl"] for t in all_trades if t.get("entry_bar", 99) > 1]
    print()
    for name, ps in (("bars 0-1 (09:15-09:45)", early), ("bars 2+ (rest)", rest)):
        if ps:
            print(f"{name}: n={len(ps)} wins={sum(1 for p in ps if p > 0)} "
                  f"total={sum(ps):.2f} avg={sum(ps) / len(ps):.2f}")
        else:
            print(f"{name}: no trades")


if __name__ == "__main__":
    main()
