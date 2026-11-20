"""Record the shadow-only ORB + true-VWAP sleeve on NIFTY futures.

Usage
-----
    # one session (run after the 15:10 flat)
    python scripts/shadow_orb_vwap.py --date 2026-11-11

    # seed the ledger from every futures session the broker still serves
    python scripts/shadow_orb_vwap.py --backfill

    # what the ledger says so far, plus the 10-session promotion gate
    python scripts/shadow_orb_vwap.py --report

The sleeve never places an order.  It fetches the nearest NIFTY futures
series, computes the opening range and a *true* session VWAP on that same
series, and appends one record per (day, variant).  If futures volume is not
available the record is written with ``status=data_issue`` and no trade.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.shadow_orb_vwap import (  # noqa: E402
    DEFAULT_COST_POINTS,
    DEFAULT_MAX_TRADES,
    DEFAULT_STOP_POINTS,
    FuturesUnavailable,
    POINT_VALUE_INR,
    PROMOTION_MAX_DRAWDOWN_POINTS,
    PROMOTION_MIN_SESSIONS,
    PROMOTION_MIN_TRADES,
    VARIANTS,
    append_record,
    default_ledger_path,
    evaluate_session,
    group_sessions,
    load_futures_bars,
    normalise_bars,
    promotion_status,
    read_records,
    session_is_complete,
    summarise,
)

DEFAULT_SOURCE = "backfill"


def _load_local_bars(path: Path):
    """Load a previously dumped futures CSV (offline / test use)."""
    import pandas as pd

    frame = pd.read_csv(path)
    frame = frame.rename(columns={
        "date": "date", "Date": "date", "timestamp": "timestamp",
        "Open": "open", "High": "high", "Low": "low", "Close": "close",
        "Volume": "volume",
    })
    return normalise_bars(frame.to_dict("records"))


def _trades_of(row) -> list:
    """Both record shapes: A-D carry ``trade``, E carries ``trades``."""
    if row.get("trade"):
        return [row["trade"]]
    return list(row.get("trades") or [])


def record(
    days,
    *,
    source: str,
    ledger: Path,
    reward_risk: float,
    stop_points: float,
    cost_points: float,
    variants=VARIANTS,
) -> int:
    added = skipped = 0
    for record_row in days:
        outcome = append_record(record_row, ledger)
        if outcome:
            added += 1
            if record_row["status"] == "ok":
                for trade in _trades_of(record_row):
                    print(
                        f"  {record_row['day']}  {record_row['variant']}  "
                        f"{trade['direction']:>5} {trade['entry_time']}->"
                        f"{trade['exit_time']}  net {trade['net_points']:+.2f} pts "
                        f"(Rs {trade['net_inr']:+,.0f})  "
                        f"{trade['exit_reason']}"
                    )
                if record_row.get("trades"):
                    print(
                        f"  {record_row['day']}  {record_row['variant']}  "
                        f"{len(record_row['trades'])} leg(s), "
                        f"net {record_row['net_points']:+.2f} pts "
                        f"(Rs {record_row['net_inr']:+,.0f})"
                    )
            else:
                print(
                    f"  {record_row['day']}  {record_row['variant']}  "
                    f"{record_row['status']}"
                    + (f" ({record_row['data_issue']})"
                       if record_row.get("data_issue") else "")
                )
        else:
            skipped += 1
    print(f"\nadded {added}, already present {skipped}")
    return added


def build_records(
    bars,
    days,
    *,
    source: str,
    reward_risk: float,
    stop_points: float,
    cost_points: float,
    max_trades: int,
    variants=VARIANTS,
):
    for day in days:
        for variant in variants:
            yield evaluate_session(
                bars,
                day=day,
                variant=variant,
                reward_risk=reward_risk,
                stop_points=stop_points,
                cost_points=cost_points,
                max_trades=max_trades,
            ) | {"source": source}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", help="single session, YYYY-MM-DD")
    parser.add_argument("--backfill", action="store_true",
                        help="record every session available from the broker")
    parser.add_argument("--report", action="store_true",
                        help="print the ledger summary and promotion gate")
    parser.add_argument("--underlying", default="NIFTY")
    parser.add_argument("--period", default="30d",
                        help="broker lookback for --backfill, e.g. 60d")
    parser.add_argument("--bars-csv", type=Path,
                        help="offline futures CSV instead of the broker")
    parser.add_argument("--ledger", type=Path, default=default_ledger_path())
    parser.add_argument("--variant", action="append", choices=VARIANTS,
                        help="restrict to one or more variants")
    parser.add_argument("--reward-risk", type=float, default=2.0)
    parser.add_argument("--stop-points", type=float, default=DEFAULT_STOP_POINTS,
                        help="premium-equivalent stop in underlying points")
    parser.add_argument("--cost-points", type=float, default=DEFAULT_COST_POINTS)
    parser.add_argument("--max-trades", type=int, default=DEFAULT_MAX_TRADES,
                        help="variant E only: max legs traded per session")
    parser.add_argument("--allow-partial", action="store_true",
                        help="record a session whose last bar is before the "
                             "15:10 flat (diagnostics only; never the gate)")
    args = parser.parse_args(argv)

    if args.report:
        return report(args.ledger)

    variants = tuple(args.variant) if args.variant else VARIANTS

    if args.bars_csv:
        bars = _load_local_bars(args.bars_csv)
        symbol = args.bars_csv.name
    else:
        try:
            symbol, bars = load_futures_bars(
                args.underlying, timeframe="5m", period=args.period
            )
        except FuturesUnavailable as exc:
            print(f"futures unavailable: {exc}")
            print("no record written - a zero-volume series cannot produce VWAP")
            return 2

    sessions = group_sessions(bars)
    days = sorted(sessions)
    source = "forward"
    if args.date:
        days = [day for day in days if day == args.date]
        if not days:
            print(f"no futures session for {args.date}")
            return 1
    elif args.backfill:
        source = DEFAULT_SOURCE
        days = [day for day in days if len(sessions[day]) >= 30]
    else:
        # Default: today's most recent completed session.
        days = days[-1:]
        source = "forward"

    if source == "forward":
        incomplete = [day for day in days
                      if not session_is_complete(sessions[day])]
        if incomplete and not args.allow_partial:
            print("refusing to record an incomplete session: "
                  + ", ".join(incomplete))
            print("the forward gate only accepts completed sessions "
                  "(last bar >= 15:10 IST); rerun after the flat, or pass "
                  "--allow-partial for a throwaway probe")
            return 2

    volume_ok = sum(1 for bar in bars if bar["volume"] > 0)
    print(f"{symbol}: {len(bars)} bars, {volume_ok} with volume, "
          f"{len(sessions)} sessions")
    print(f"recording {len(days)} session(s) as source={source}, "
          f"variants={','.join(variants)}, R={args.reward_risk}, "
          f"stop={args.stop_points} pts")

    record(
        build_records(
            bars, days,
            source=source,
            reward_risk=args.reward_risk,
            stop_points=args.stop_points,
            cost_points=args.cost_points,
            max_trades=args.max_trades,
            variants=variants,
        ),
        source=source,
        ledger=args.ledger,
        reward_risk=args.reward_risk,
        stop_points=args.stop_points,
        cost_points=args.cost_points,
        variants=variants,
    )
    return report(args.ledger)


def report(ledger: Path) -> int:
    records = read_records(ledger)
    if not records:
        print(f"no shadow records in {ledger}")
        return 0
    print(f"\nSHADOW ORB + TRUE VWAP - {ledger}")
    print(f"point value: Rs {POINT_VALUE_INR:.1f}/point (delta 0.5 x lot 65)")
    print()
    header = (f"  {'var':<4}{'src':<9}{'sess':>5}{'bad':>4}{'trd':>4}"
              f"{'win':>4}{'win%':>7}{'net pts':>9}{'net Rs':>10}"
              f"{'maxDD':>8}{'PF':>6}{'vwapBlk':>8}")
    print(header)
    for source in ("backfill", "forward"):
        for variant in VARIANTS:
            stats = summarise(records, variant=variant, source=source)
            if not stats["sessions"] and not stats["data_issues"]:
                continue
            pf = stats["profit_factor"]
            print(
                f'  {variant:<4}{source:<9}{stats["sessions"]:>5}'
                f'{stats["data_issues"]:>4}{stats["trades"]:>4}'
                f'{stats["wins"]:>4}{stats["win_pct"]:>6.1f}%'
                f'{stats["net_points"]:>9.1f}{stats["net_inr"]:>10,.0f}'
                f'{stats["max_drawdown_points"]:>8.1f}'
                f'{(f"{pf:.2f}" if pf is not None else "-"):>6}'
                f'{stats["vwap_binding"]:>8}'
            )

    print(f"\nPROMOTION GATE (forward only, {PROMOTION_MIN_SESSIONS} sessions / "
          f"{PROMOTION_MIN_TRADES} trades / net>0 / maxDD<="
          f"{PROMOTION_MAX_DRAWDOWN_POINTS:.0f} pts)")
    for variant in VARIANTS:
        status = promotion_status(records, variant=variant)
        mark = "READY" if status["eligible"] else "not yet"
        details = ", ".join(
            f"{name}:{'ok' if check['met'] else 'no'} ({check['detail']})"
            for name, check in status["checks"].items()
        )
        print(f"  {variant}: {mark}  {details}")

    sessions = len({str(row.get("day")) for row in records})
    if sessions < PROMOTION_MIN_SESSIONS:
        print(f"\n  {sessions} sessions on file - pilot only, not a verdict")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
