"""Sweep TIME_STOP_BARS on the 21-day NIFTY 15m replay; report P&L per setting.
Author: Kapil Kuhire <kapilkuhire89@gmail.com>
"""
import sys
from pathlib import Path
import logging
logging.disable(logging.CRITICAL)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import scripts.day_session as ds
from src.data_fetcher import fetch_market_data

md = fetch_market_data("^NSEI", "15m", "60d")
full = md.ohlcv
days = ds.session_slices(full)[-21:]
print(f"Loaded {len(days)} sessions, {len(full)} warmup bars\n")
print(f"{'time_stop':>10} {'trades':>7} {'wins':>5} {'win%':>7} {'total P&L':>10}")
print("-" * 48)

for ts in [8, 12, 16, 20, 24, None]:
    ds.TIME_STOP_BARS = ts if ts is not None else 10 ** 9  # off
    label = str(ts) if ts is not None else "off"
    tot_pnl, tot_t, tot_w = 0.0, 0, 0
    for sess in days:
        end_pos = full.index.get_loc(sess.index[-1])
        hist = full.iloc[max(0, end_pos - len(sess) - 300):end_pos - len(sess) + 1]
        r = ds.replay_day(sess, "^NSEI", "15m",
                          ds.WINDOW_SETS["daily-windows"], warmup=hist)
        tot_pnl += r["pnl"]
        tot_t += r["trades"]
        tot_w += r["wins"]
    wr = round(tot_w / tot_t * 100, 1) if tot_t else 0.0
    print(f"{label:>10} {tot_t:>7} {tot_w:>5} {wr:>6}% {tot_pnl:>10.2f}")
