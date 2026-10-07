"""Sweep ADX regime-filter threshold on the 21-day NIFTY 15m replay.
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

The regime gate (min_adx) refuses entries when ADX trend-strength is
below threshold — Sep 10-11 style losses came from trading chop.
Same entries, only the filter changes. Keep-or-revert on numbers alone.

Usage: python scripts/sweep_adx.py [--symbol ^NSEI] [--days 21]
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
import src.utils as _u


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="^NSEI")
    ap.add_argument("--days", type=int, default=21)
    args = ap.parse_args()

    md = fetch_market_data(args.symbol, "15m", "60d")
    full = md.ohlcv
    days = ds.session_slices(full)[-args.days:]
    print(f"{args.symbol}: {len(days)} sessions, {len(full)} warmup bars\n")

    base = _u.load_config()
    regime = base.get("signal_engine", {}).get("regime_filter", {})
    print(f"Current config regime_filter: {regime} (sweep overrides it)\n")

    print(f"{'min_adx':>8} {'trades':>7} {'wins':>5} {'win%':>7} {'total P&L':>10}")
    print("-" * 48)
    for thresh in [0, 15, 20, 25, 30]:
        cfg = _u.load_config()
        cfg.setdefault("signal_engine", {}).setdefault("regime_filter", {})["min_adx"] = thresh
        tot_pnl, tot_t, tot_w = 0.0, 0, 0
        for sess in days:
            end_pos = full.index.get_loc(sess.index[-1])
            hist = full.iloc[max(0, end_pos - len(sess) - 300):end_pos - len(sess) + 1]
            r = ds.replay_day(sess, args.symbol, "15m",
                              ds.WINDOW_SETS["daily-windows"], warmup=hist)
            tot_pnl += r["pnl"]
            tot_t += r["trades"]
            tot_w += r["wins"]
        wr = round(tot_w / tot_t * 100, 1) if tot_t else 0.0
        tag = "  <-- current (off)" if thresh == 0 else ""
        print(f"{thresh:>8} {tot_t:>7} {tot_w:>5} {wr:>6}% {tot_pnl:>10.2f}{tag}")


if __name__ == "__main__":
    main()
