"""Sweep ladder T2 on the 21-day NIFTY 15m replay; report P&L per t2_at_r.

Sweeps T2 in 1.2–1.8R (trailing on the 20% runner) vs Phase-1-only (no T2).
Phase 2 is shadow-logged live; this sweep validates before flipping
enabled_phase2. One-setting win alone is not enough — look for a sweep.

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
from src.utils import load_config

GRID = [None, 1.2, 1.4, 1.5, 1.6, 1.8]  # None = Phase 1 (no T2)

md = fetch_market_data("^NSEI", "15m", "60d")
full = md.ohlcv
days = ds.session_slices(full)[-21:]
print(f"Loaded {len(days)} sessions, {len(full)} warmup bars\n")
print(f"{'t2_at_r':>8} {'trades':>7} {'wins':>5} {'win%':>7} {'total P&L':>10} {'shadow T2':>10}")
print("-" * 64)

# Cache per-day replay machinery; replays call through the real guard
# ladder config via src/utils.load_config cache. Mutate it per sweep.
from src.utils import load_config as _load_config

cfg = _load_config()
rm = cfg.setdefault("risk_management", {})
ladder = rm.setdefault("ladder", {})

for t2 in GRID:
    label = "off(P1)" if t2 is None else str(t2)
    # Enable Phase 2 iff sweeping a t2 level.
    if t2 is None:
        ladder["enabled_phase2"] = False
        ladder["t2_at_r"] = 1.5  # inert
    else:
        ladder["enabled_phase2"] = True
        ladder["t2_at_r"] = t2

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
    print(f"{label:>8} {tot_t:>7} {tot_w:>5} {wr:>6}% {tot_pnl:>10.2f}    --")

# Restore default: shadow-log until a winning sweep ships.
ladder["enabled_phase2"] = False
ladder["t2_at_r"] = 1.5
print("\nRestored ladder.enabled_phase2=False (shadow-log).")
