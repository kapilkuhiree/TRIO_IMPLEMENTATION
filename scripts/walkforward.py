"""TRIO — 60-day walk-forward harness: trade 2 days, analyse, repeat.

YOUR plan, enforced by code:
  Phase 1 (days 1-20, blocks 1-10): run each 2-day block, write a per-block
      analysis (what fired, what failed, which exit reasons). NO code/config
      changes between blocks — observation only. Changing the system
      mid-phase would contaminate the very evidence the monthly checkin
      needs.
  Monthly checkin (after block 10): aggregate all 10 analyses. Apply a
      change ONLY if the evidence table supports it across blocks
      (same rule as the time-stop/ADX/open-window rejections). One change
      max, then re-freeze.
  Phase 2 (days 21-40, blocks 11-20): LOCKED. No changes at all. This is
      the true validation — performance here is the honest number.

Why 2-day blocks: one day is noise (a single gap move dominates), a week
is too slow to react. Two days gives ~40 sessions x 20 symbols of evidence
per block while keeping each analysis cheap.

Checkpoints: output/walkforward/block_NN.json after every block, plus
output/walkforward/SUMMARY.md updated each time. A stopped run resumes
with --from-block N. Nothing is ever recomputed silently.

Usage:
  python scripts/walkforward.py --days 40              # full program
  python scripts/walkforward.py --days 40 --from-block 6   # resume
  python scripts/walkforward.py --days 4 --symbols RELIANCE.NS TCS.NS  # smoke

Config snapshot: every block records the strategy-relevant config values
(entry windows, RR, stops, filters) so a later reader can prove nothing
moved mid-phase. If values differ between blocks in Phase 1, the harness
ABORTS — that is the no-change guardrail.
"""

import argparse
import copy
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import logging
logging.disable(logging.CRITICAL)

from src.data_fetcher import fetch_market_data  # noqa: E402
import scripts.day_session as ds  # noqa: E402
from src.utils import load_config  # noqa: E402

OUT = ROOT / "output" / "walkforward"

# The 20-name watchlist from config. Overridable via --symbols for smoke runs.
DEFAULT_SYMBOLS: List[str] = []

# Strategy knobs whose movement mid-phase invalidates the experiment.
GUARD_KEYS = [
    ("signal_engine", "pullback", "trend_sma"),
    ("signal_engine", "pullback", "pullback_sma"),
    ("signal_engine", "regime_filter", "min_adx"),
    ("risk_management", "take_profit", "min_risk_reward"),
    ("risk_management", "stop_loss", "method"),
    ("trading", "allow_shorts"),
]


def strategy_fingerprint() -> Dict[str, Any]:
    """Small dict of the strategy knobs + a hash. Stored per block."""
    cfg = load_config()
    snap: Dict[str, Any] = {}
    node: Any = cfg
    for path in GUARD_KEYS:
        node = cfg
        try:
            for k in path:
                node = node[k]
            snap["/".join(path)] = node
        except (KeyError, TypeError):
            snap["/".join(path)] = None
    snap["_hash"] = hashlib.sha256(
        json.dumps(snap, sort_keys=True, default=str).encode()).hexdigest()[:12]
    return snap


def month_sessions(symbol: str, tf: str, days: int):
    """Fetch once; return (full_frame, last-N complete sessions)."""
    period = "60d" if tf == "15m" else "30d"
    md = fetch_market_data(symbol, tf, period)
    full = md.ohlcv
    sessions = ds.session_slices(full)[-days:]
    return full, sessions


def run_block(symbols: List[str], tf: str, sessions_by_sym: Dict[str, Any],
              full_by_sym: Dict[str, Any], date_lo: str, date_hi: str) -> Dict[str, Any]:
    """Replay one 2-day block across all symbols (daily-windows only)."""
    per_symbol: Dict[str, Any] = {}
    tot_t = tot_w = 0
    tot_pnl = 0.0
    by_reason: Dict[str, int] = {}
    wins = ds.WINDOW_SETS["daily-windows"]

    for sym in symbols:
        full = full_by_sym[sym]
        t_pnl, t_t, t_w = 0.0, 0, 0
        details = []
        for sess in sessions_by_sym[sym]:
            end_pos = full.index.get_loc(sess.index[-1])
            hist = full.iloc[max(0, end_pos - len(sess) - 300):end_pos - len(sess) + 1]
            r = ds.replay_day(sess, sym, tf, wins, warmup=hist,
                              debug_empty=False)
            t_pnl += r["pnl"]
            t_t += r["trades"]
            t_w += r["wins"]
            for t in r["details"]:
                by_reason[t["reason"]] = by_reason.get(t["reason"], 0) + 1
                details.append({**t, "date": r["date"]})
        wr = round(t_w / t_t * 100, 1) if t_t else 0.0
        per_symbol[sym] = {"trades": t_t, "wins": t_w, "win_rate": wr,
                           "pnl": round(t_pnl, 2), "details": details}
        tot_t += t_t
        tot_w += t_w
        tot_pnl += t_pnl

    return {
        "dates": [date_lo, date_hi],
        "symbols": len(symbols),
        "trades": tot_t,
        "wins": tot_w,
        "win_rate": round(tot_w / tot_t * 100, 1) if tot_t else 0.0,
        "pnl": round(tot_pnl, 2),
        "exit_reasons": by_reason,
        "per_symbol": per_symbol,
        "fingerprint": strategy_fingerprint(),
    }


def analyse_block(block: Dict[str, Any], n: int) -> List[str]:
    """Mechanical per-block analysis: facts + flagged questions, no tuning."""
    notes = []
    notes.append(
        f"Block {n} ({block['dates'][0]} -> {block['dates'][1]}): "
        f"{block['trades']} trades, {block['win_rate']}% win, "
        f"P&L {block['pnl']:+.2f}.")
    if block["trades"] == 0:
        notes.append("No trades — market offered no setups (refusals, not errors).")
        return notes
    er = block["exit_reasons"]
    sq = er.get("squareoff", 0)
    if sq and sq / max(block["trades"], 1) > 0.7:
        notes.append(
            f"{sq}/{block['trades']} exits are squareoff — exits still passive; "
            f"flag for monthly checkin, do NOT change mid-phase.")
    losers = [(s, v) for s, v in block["per_symbol"].items() if v["pnl"] < 0]
    if losers:
        worst = sorted(losers, key=lambda x: x[1]["pnl"])[:3]
        notes.append("Worst names: " + ", ".join(
            f"{s} ({v['pnl']:+.2f} on {v['trades']} trades)" for s, v in worst))
    winners = [(s, v) for s, v in block["per_symbol"].items() if v["pnl"] > 0]
    if winners:
        best = sorted(winners, key=lambda x: x[1]["pnl"], reverse=True)[:3]
        notes.append("Best names: " + ", ".join(
            f"{s} ({v['pnl']:+.2f} on {v['trades']} trades)" for s, v in best))
    flats = [s for s, v in block["per_symbol"].items() if v["trades"] == 0]
    if flats:
        notes.append(f"{len(flats)} names with zero trades "
                     f"({', '.join(flats[:5])}{'...' if len(flats) > 5 else ''}).")
    return notes


def write_summary(blocks: List[Dict[str, Any]]) -> None:
    """Rewrite SUMMARY.md from all completed blocks."""
    lines = ["# TRIO walk-forward — running summary",
             f"_Updated {datetime.now(timezone.utc).isoformat()}_", ""]
    ct = cw = 0
    cp = 0.0
    for i, b in enumerate(blocks, 1):
        ct += b["trades"]
        cw += b["wins"]
        cp += b["pnl"]
        lines.append(
            f"## Block {i} ({b['dates'][0]} -> {b['dates'][1]})")
        lines.append(
            f"- {b['trades']} trades, {b['win_rate']}% win, "
            f"P&L {b['pnl']:+.2f}, exits {b['exit_reasons']}")
        for note in b.get("analysis", []):
            lines.append(f"- {note}")
        lines.append("")
    wr = round(cw / ct * 100, 1) if ct else 0.0
    lines.insert(3, f"**Cumulative: {ct} trades, {wr}% win, "
                    f"P&L {cp:+.2f} across {len(blocks)} blocks.**")
    lines.insert(4, "")
    (OUT / "SUMMARY.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="60-day walk-forward: 2-day blocks")
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--tf", default="15m", choices=["15m", "5m"])
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--from-block", type=int, default=1)
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)

    if args.symbols:
        symbols = args.symbols
    else:
        cfg = load_config()
        symbols = list(cfg.get("symbols", []))
    assert symbols, "empty symbol list — check config `symbols:`"

    # Fetch everything once, union the calendars, take the last N sessions
    # present across names (a session counts if >= 15 of 20 names have it).
    print(f"Fetching {args.tf} data for {len(symbols)} symbols (once)...",
          flush=True)
    full_by: Dict[str, Any] = {}
    sess_by: Dict[str, Any] = {}
    for n, sym in enumerate(symbols, 1):
        full, sessions = month_sessions(sym, args.tf, args.days)
        full_by[sym] = full
        sess_by[sym] = {str(s.index[0].date()): s for s in sessions}
        print(f"  [{n}/{len(symbols)}] {sym}: {len(sessions)} sessions",
              flush=True)

    # Calendar = dates present for a majority of names, sorted, last N.
    from collections import Counter
    votes: Counter = Counter()
    for m in sess_by.values():
        for d in m:
            votes[d] += 1
    cal = sorted(d for d, v in votes.items() if v >= max(1, len(symbols) * 3 // 4))
    cal = cal[-args.days:]
    print(f"Common calendar: {len(cal)} sessions "
          f"({cal[0]} -> {cal[-1]})", flush=True)

    # 2-day blocks over the calendar.
    blocks_cal = [cal[i:i + 2] for i in range(0, len(cal), 2)]
    print(f"{len(blocks_cal)} blocks of ~2 days.\n", flush=True)

    done: List[Dict[str, Any]] = []
    # reload completed blocks for resume
    for f in sorted(OUT.glob("block_*.json")):
        try:
            done.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    done.sort(key=lambda b: b.get("block", 0))
    base_fp = done[0]["fingerprint"]["_hash"] if done else None

    for idx, dates in enumerate(blocks_cal, 1):
        if idx < args.from_block:
            continue
        if any(b.get("block") == idx for b in done):
            print(f"Block {idx} already done — skipping.", flush=True)
            continue
        per_sym_sess = {s: [sess_by[s][d] for d in dates if d in sess_by[s]]
                        for s in symbols}
        block = run_block(symbols, args.tf, per_sym_sess, full_by,
                          dates[0], dates[-1])
        block["block"] = idx
        block["tf"] = args.tf
        # No-change guardrail: strategy fingerprint must match block 1.
        fp = block["fingerprint"]["_hash"]
        if base_fp is None:
            base_fp = fp
        elif fp != base_fp:
            print(f"ABORT: strategy config moved mid-phase "
                  f"({base_fp} -> {fp}). Freeze, then rerun.", flush=True)
            raise SystemExit(2)
        block["analysis"] = analyse_block(block, idx)
        (OUT / f"block_{idx:02d}.json").write_text(
            json.dumps(block, indent=2, default=str), encoding="utf-8")
        done.append(block)
        write_summary(done)
        print(f"Block {idx} ({dates[0]} -> {dates[-1]}): "
              f"{block['trades']} trades, {block['win_rate']}% win, "
              f"P&L {block['pnl']:+.2f}  [saved]", flush=True)
        for note in block["analysis"]:
            print(f"    - {note}", flush=True)

    print("\nDone. See output/walkforward/SUMMARY.md", flush=True)


if __name__ == "__main__":
    main()
