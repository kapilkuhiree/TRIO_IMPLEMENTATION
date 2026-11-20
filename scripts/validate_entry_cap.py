"""Validate the proposed 12:30 entry cutoff and daily-entry cap.

Offline only: reads the 5-minute index frames cached by
``scripts/backtest_orb_post_expiry.py`` and never calls a broker.

The model is the live book, not a single strategy stream:

  * ORB and ORB-Retest candidate streams share one daily-entry counter;
  * max two open positions, at most one per underlying;
  * ORB keeps its loss-only 300s cooldown; ORB-Retest has none;
  * one lot size derived from the Rs 18,000 premium cap and the 0.7% risk cap;
  * per-session actual DTE from the NIFTY expiry calendar, 14% IV (the
    archive says ~15%, and 18% was demonstrably too rich).

The variables under test are only the last-entry clock and the daily cap, so
every other rule is identical across variants.

Usage::

    python scripts/validate_entry_cap.py
    python scripts/validate_entry_cap.py --iv 0.18
    python scripts/validate_entry_cap.py --json-out output/entry_cap_validation.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import pandas as pd  # noqa: E402

import backtest_orb_post_expiry as bpe  # noqa: E402  (patches engine.build_candidate)
import backtest_orb_retest_v2 as engine  # noqa: E402

# 12:30 IST is 195 minutes after the 09:15 open. 14:30 is the engine default.
CUTOFF_1230 = 12 * 60 + 30 - (9 * 60 + 15)

VARIANTS = (
    # name,              daily cap, last entry (minutes after 09:15)
    ("current_cap10", 10, None),
    ("cap14", 14, None),
    ("cutoff1230", 99, CUTOFF_1230),
    ("cutoff1230_cap14", 14, CUTOFF_1230),
    ("uncapped", 99, None),
)

START = date(2026, 7, 6)
END = date(2026, 11, 20)  # last complete session
SYMBOLS = ("NIFTY", "BANKNIFTY")
ALL_STRATEGIES = ("orb", "orb_retest")
GATES = {"orb": bpe.ORB_GATE, "orb_retest": bpe.RETEST_GATE}


def load_frames(start, end, refresh):
    frames = {}
    for name in SYMBOLS:
        frame = bpe.fetch_index_5m(name, start, end, use_cache=not refresh)
        frames[name] = frame
        print(f"{name:<10} {len(frame):,} bars  "
              f"{frame.index.min():%Y-%m-%d} .. {frame.index.max():%Y-%m-%d}")
    return frames


def build_streams(frames):
    candles_by, signals = {}, {"orb": {}, "orb_retest": {}}
    for key, spec in engine.SYMBOLS.items():
        name = spec["name"]
        candles = engine.to_candles(frames[name], name)
        candles_by[name] = candles
        signals["orb"][name] = engine.collect_orb_signals(
            candles, name, spec["lot"], spec["step"])
        signals["orb_retest"][name] = engine.collect_retest_signals(
            candles, name, spec["lot"], spec["step"], **bpe.RETEST_PARAMS)
        print(f"{name:<10} ORB signals {len(signals['orb'][name]):<4} "
              f"retest signals {len(signals['orb_retest'][name])}")
    return candles_by, signals


def make_metas(dte_map, iv):
    return {
        spec["name"]: dict(spec, vol=iv, dte_by_day=dte_map, actual_dte=True)
        for spec in engine.SYMBOLS.values()
    }


def candidate_pool(candles_by, signals, metas, names, strategies):
    """Uncapped candidate list; caps and cooldowns are applied by selection."""
    pool = []
    for name in names:
        for strategy in strategies:
            pool += engine.build_candidates(
                candles_by[name], signals[strategy][name], metas[name],
                strategy=strategy, earliest_min=GATES[strategy]["earliest_min"])
    return pool


def run_portfolio(pool, *, cap, cutoff_min):
    if cutoff_min is not None:
        pool = [
            trade for trade in pool
            if engine.minute_of_day(pd.Timestamp(trade["entry_ts"])) <= cutoff_min
        ]
    return engine.select_portfolio(
        pool, max_open=2, max_entries=cap, cooldowns={"orb": 300})


def metrics(trades, label, days_in_scope):
    row = engine.stats(trades, label)
    row["days_in_scope"] = days_in_scope
    row["per_session"] = (row["net"] / days_in_scope) if days_in_scope else 0.0
    row["trades_per_session"] = (
        row["trades"] / days_in_scope if days_in_scope else 0.0)
    return row


def print_rows(rows, title):
    print(f"\n{title}")
    print("-" * 114)
    print(f"{'Variant':<20}{'Trades':>7}{'Win%':>7}{'Net Rs':>13}{'PF':>7}"
          f"{'MaxDD':>11}{'Rs/sess':>10}{'WinDays':>10}{'t':>7}")
    print("-" * 114)
    for row in rows:
        pf = "inf" if row["profit_factor"] == float("inf") else f"{row['profit_factor']:.2f}"
        print(f"{row['label']:<20}{row['trades']:>7}{row['win_rate']:>6.1f}%"
              f"{row['net']:>13,.0f}{pf:>7}{row['max_dd']:>11,.0f}"
              f"{row['per_session']:>10,.0f}"
              f"{row['win_days']:>5}/{row['days_in_scope']:<4}{row['t_stat']:>7.2f}")
    print("-" * 114)


def print_delta(current, proposed):
    print("\nPROPOSED (cutoff1230_cap14) vs CURRENT (cap10, 14:30)")
    print("-" * 72)
    print(f"  net          {current['net']:>12,.0f} -> {proposed['net']:>12,.0f}"
          f"   {proposed['net'] - current['net']:+,.0f}")
    print(f"  profit factor{current['profit_factor']:>12.2f} -> "
          f"{proposed['profit_factor']:>12.2f}"
          f"   {proposed['profit_factor'] - current['profit_factor']:+.2f}")
    print(f"  max drawdown {current['max_dd']:>12,.0f} -> {proposed['max_dd']:>12,.0f}"
          f"   {proposed['max_dd'] - current['max_dd']:+,.0f}")
    print(f"  win days     {current['win_days']:>12} -> {proposed['win_days']:>12}")
    print(f"  trades       {current['trades']:>12} -> {proposed['trades']:>12}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default=str(START))
    parser.add_argument("--end", default=str(END))
    parser.add_argument("--refresh", action="store_true",
                        help="re-fetch the 5m history instead of using the cache")
    parser.add_argument("--iv", type=float, default=0.14,
                        help="synthetic IV for the headline tables (default 0.14)")
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    frames = load_frames(start, end, args.refresh)
    sessions = bpe.session_dates(frames["NIFTY"])
    expiries = bpe.weekly_expiries(sessions, sessions[0], sessions[-1])
    cycles = bpe.expiry_cycles(sessions, expiries)
    try:
        future = bpe.listed_future_expiries(sessions[-1])
    except Exception as exc:  # chain cache missing is not fatal
        print(f"warning: forward expiries unavailable ({exc})")
        future = []
    dte_map = bpe.dte_by_day(sessions, list(expiries) + list(future))

    candles_by, signals = build_streams(frames)
    metas = make_metas(dte_map, args.iv)

    post_days = {str(d) for c in cycles for d in (c["e1"], c["e2"]) if d}
    all_days = {str(d) for d in sessions}
    other_days = all_days - post_days
    complete_cycles = sum(1 for c in cycles if c["e2"] is not None)

    print(f"\nSessions {sessions[0]} .. {sessions[-1]} ({len(sessions)})  |  "
          f"expiries {len(expiries)}  |  cycles {len(cycles)} "
          f"({complete_cycles} complete)  |  IV {args.iv:.0%}  |  actual DTE")
    print(f"Post-expiry days {len(post_days)}  |  other days {len(other_days)}")

    scopes = {
        "nifty_orb": (("NIFTY",), ("orb",)),
        "nifty_orb_retest": (("NIFTY",), ALL_STRATEGIES),
        "two_sleeve_orb_retest": (SYMBOLS, ALL_STRATEGIES),
    }
    pools = {
        key: candidate_pool(candles_by, signals, metas, names, strategies)
        for key, (names, strategies) in scopes.items()
    }

    results = {}
    for key, pool in pools.items():
        for name, cap, cutoff in VARIANTS:
            selected = run_portfolio(pool, cap=cap, cutoff_min=cutoff)
            results[(key, name)] = selected

    # ---------------- headline: NIFTY, both sleeves, all sessions ----------
    headline = [
        metrics(results[("nifty_orb_retest", name)], name, len(all_days))
        for name, _, _ in VARIANTS
    ]
    print_rows(headline, "NIFTY ORB + ORB-RETEST  -  all sessions")

    single = [
        metrics(results[("nifty_orb", name)], name, len(all_days))
        for name, _, _ in VARIANTS
    ]
    print_rows(single, "NIFTY ORB only  -  all sessions")

    two = [
        metrics(results[("two_sleeve_orb_retest", name)], name, len(all_days))
        for name, _, _ in VARIANTS
    ]
    print_rows(two, "NIFTY + BANKNIFTY, both sleeves  -  all sessions")

    # ---------------- post-expiry scope -----------------------------------
    post_rows = []
    for name, _, _ in VARIANTS:
        trades = [t for t in results[("nifty_orb_retest", name)]
                  if t["day"] in post_days]
        post_rows.append(metrics(trades, name, len(post_days)))
    print_rows(post_rows, "NIFTY ORB + ORB-RETEST  -  post-expiry E+1/E+2")

    other_rows = []
    for name, _, _ in VARIANTS:
        trades = [t for t in results[("nifty_orb_retest", name)]
                  if t["day"] in other_days]
        other_rows.append(metrics(trades, name, len(other_days)))
    print_rows(other_rows, "NIFTY ORB + ORB-RETEST  -  non-post-expiry sessions")

    # ---------------- what the clock actually removes ----------------------
    print("\nENTRIES AFTER 12:30 (uncapped, 14:30 last entry)")
    print("-" * 84)
    print(f"{'Scope':<34}{'Trades':>8}{'Win%':>8}{'Net Rs':>14}{'Best':>12}{'Worst':>12}")
    print("-" * 84)
    late_by_scope = {}
    for key, (names, strategies) in scopes.items():
        late = [
            t for t in results[(key, "uncapped")]
            if engine.minute_of_day(pd.Timestamp(t["entry_ts"])) > CUTOFF_1230
        ]
        late_by_scope[key] = late
        wins = sum(1 for t in late if t["pnl"] > 0)
        net = sum(t["pnl"] for t in late)
        best = max((t["pnl"] for t in late), default=0.0)
        worst = min((t["pnl"] for t in late), default=0.0)
        rate = (wins / len(late) * 100.0) if late else 0.0
        print(f"{key:<34}{len(late):>8}{rate:>7.1f}%{net:>14,.0f}"
              f"{best:>12,.0f}{worst:>12,.0f}")
    print("-" * 84)

    # ---------------- cycle-level -----------------------------------------
    print("\nPOST-EXPIRY CYCLES (E+1 + E+2 combined)")
    print("-" * 76)
    print(f"{'Variant':<26}{'cycles':>8}{'positive':>10}{'net Rs':>14}{'avg/cycle':>12}")
    print("-" * 76)
    cycle_out = {}
    for name, _, _ in VARIANTS:
        trades = [t for t in results[("nifty_orb_retest", name)]
                  if t["day"] in post_days]
        summary = bpe.cycle_stats(bpe.cycle_totals(trades, cycles), name)
        cycle_out[name] = summary
        print(f"{name:<26}{summary['cycles']:>8}"
              f"{summary['positive_cycles']:>6}/{summary['cycles']:<3}"
              f"{summary['net']:>14,.0f}{summary['avg_cycle']:>12,.0f}")
    print("-" * 76)

    print_delta(
        next(r for r in headline if r["label"] == "current_cap10"),
        next(r for r in headline if r["label"] == "cutoff1230_cap14"),
    )

    # ---------------- sweeps ----------------------------------------------
    print("\nCAP SWEEP  (NIFTY, both sleeves, last entry 14:30)")
    print("-" * 84)
    print(f"{'Daily cap':<12}{'Trades':>8}{'Win%':>8}{'Net Rs':>14}{'PF':>7}"
          f"{'MaxDD':>12}{'WinDays':>10}")
    print("-" * 84)
    cap_sweep = {}
    for cap in (8, 10, 12, 14, 16, 20, 99):
        row = metrics(run_portfolio(pools["nifty_orb_retest"], cap=cap,
                                    cutoff_min=None), f"cap{cap}", len(all_days))
        cap_sweep[str(cap)] = {"trades": row["trades"], "net": round(row["net"], 2),
                               "profit_factor": round(row["profit_factor"], 3),
                               "max_dd": round(row["max_dd"], 2),
                               "win_days": row["win_days"]}
        label = "none" if cap == 99 else str(cap)
        print(f"{label:<12}{row['trades']:>8}{row['win_rate']:>7.1f}%"
              f"{row['net']:>14,.0f}{row['profit_factor']:>7.2f}"
              f"{row['max_dd']:>12,.0f}{row['win_days']:>6}/{len(all_days)}")
    print("-" * 84)

    print("\nCLOCK SWEEP  (NIFTY, both sleeves, cap 14)")
    print("-" * 84)
    print(f"{'Last entry':<12}{'Trades':>8}{'Win%':>8}{'Net Rs':>14}{'PF':>7}"
          f"{'MaxDD':>12}{'WinDays':>10}")
    print("-" * 84)
    clock_sweep = {}
    for label, cutoff in (("12:00", 165), ("12:30", 195), ("13:00", 225),
                          ("13:30", 255), ("14:00", 285), ("14:30", 315)):
        row = metrics(run_portfolio(pools["nifty_orb_retest"], cap=14,
                                    cutoff_min=cutoff), label, len(all_days))
        clock_sweep[label] = {"trades": row["trades"], "net": round(row["net"], 2),
                              "profit_factor": round(row["profit_factor"], 3),
                              "max_dd": round(row["max_dd"], 2),
                              "win_days": row["win_days"]}
        print(f"{label:<12}{row['trades']:>8}{row['win_rate']:>7.1f}%"
              f"{row['net']:>14,.0f}{row['profit_factor']:>7.2f}"
              f"{row['max_dd']:>12,.0f}{row['win_days']:>6}/{len(all_days)}")
    print("-" * 84)

    # ---------------- is the late edge concentrated? ----------------------
    late = late_by_scope["nifty_orb_retest"]
    total = sum(t["pnl"] for t in late)
    by_month = defaultdict(float)
    by_scope = defaultdict(float)
    for trade in late:
        by_month[trade["day"][:7]] += trade["pnl"]
        by_scope["post_expiry" if trade["day"] in post_days else "other"] += trade["pnl"]

    print("\nENTRIES AFTER 12:30 - CONCENTRATION (NIFTY, both sleeves)")
    print("-" * 74)
    print(f"  total {len(late)} trades  Rs {total:,.0f}")
    print("  by month:", "  ".join(f"{m} {v:+,.0f}" for m, v in sorted(by_month.items())))
    print("  by scope:", "  ".join(f"{k} {v:+,.0f}" for k, v in sorted(by_scope.items())))
    top = sorted(late, key=lambda t: t["pnl"], reverse=True)[:5]
    top_pnl = sum(t["pnl"] for t in top)
    print(f"  top 5 trades Rs {top_pnl:,.0f} "
          f"({(top_pnl / total * 100 if total else 0):.0f}% of the late total)")
    for trade in top:
        print(f"    {trade['day']} {trade['entry_ts'][11:16]} "
              f"{trade['strategy']:<10} K{trade['strike']} {trade['kind']} "
              f"Rs {trade['pnl']:>8,.0f}")
    print("-" * 74)

    # ---------------- IV sensitivity --------------------------------------
    print(f"\nIV SENSITIVITY (NIFTY, both sleeves, all sessions)")
    print("-" * 62)
    print(f"{'IV':<8}{'cap10 14:30':>18}{'cutoff+cap14':>18}{'delta':>14}")
    print("-" * 62)
    sensitivity = {}
    for iv in (0.14, 0.18, 0.22):
        pool = candidate_pool(candles_by, signals, make_metas(dte_map, iv),
                              ("NIFTY",), ALL_STRATEGIES)
        base = metrics(run_portfolio(pool, cap=10, cutoff_min=None), "base", 0)
        prop = metrics(run_portfolio(pool, cap=14, cutoff_min=CUTOFF_1230), "prop", 0)
        sensitivity[f"{iv:.2f}"] = {"current": base["net"], "proposed": prop["net"]}
        print(f"{iv:<8.0%}{base['net']:>18,.0f}{prop['net']:>18,.0f}"
              f"{prop['net'] - base['net']:>+14,.0f}")
    print("-" * 62)

    if args.json_out:
        out = {
            "window": {"start": str(start), "end": str(end),
                       "sessions": len(sessions), "expiries": len(expiries),
                       "complete_cycles": complete_cycles},
            "iv": args.iv,
            "metrics": {
                scope: [metrics(results[(scope, name)], name, len(all_days))
                        for name, _, _ in VARIANTS]
                for scope in scopes
            },
            "post_expiry": post_rows,
            "late_entries_after_1230": {
                key: {
                    "trades": len(trades),
                    "net": round(sum(t["pnl"] for t in trades), 2),
                }
                for key, trades in late_by_scope.items()
            },
            "cycles": cycle_out,
            "iv_sensitivity": sensitivity,
            "cap_sweep": cap_sweep,
            "clock_sweep": clock_sweep,
            "late_entries_by_month": {k: round(v, 2) for k, v in sorted(by_month.items())},
            "late_entries_by_scope": {k: round(v, 2) for k, v in sorted(by_scope.items())},
        }
        Path(args.json_out).write_text(json.dumps(out, indent=2, default=str),
                                       encoding="utf-8")
        print(f"\nJSON written to {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
