"""Record the shadow-only short-premium sleeve from archived option premiums.

Usage
-----
    # one session
    python scripts/shadow_short_premium.py --date 2026-11-11

    # every archived session (idempotent re-runs are safe)
    python scripts/shadow_short_premium.py --backfill

    # what the ledger says so far
    python scripts/shadow_short_premium.py --report

This never places an order.  It reads the SQLite option archive and the NIFTY
5m index CSV, builds the bull put spread the sleeve WOULD have taken at 09:45,
settles it at 15:05, and appends one JSON line per session.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.shadow_short_premium import (  # noqa: E402
    DEFAULT_COST_POINTS,
    DEFAULT_OTM,
    DEFAULT_WIDTH,
    LOT_SIZE,
    append_record,
    choose_expiry,
    default_archive_path,
    default_ledger_path,
    load_day_chain,
    load_spot_by_day,
    read_records,
    replay_day,
    summarise,
)

DEFAULT_INDEX = ROOT / "output" / "research" / "nifty_5m_90d.csv"


def archived_days(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT substr(k.timestamp_utc, 1, 10)
        FROM candles k JOIN contracts c ON c.contract_id = k.contract_id
        WHERE c.underlying = 'NIFTY' AND c.option_type = 'PE'
        ORDER BY 1
        """
    ).fetchall()
    return [row[0] for row in rows]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", help="single session, YYYY-MM-DD")
    parser.add_argument("--backfill", action="store_true",
                        help="record every archived session")
    parser.add_argument("--report", action="store_true",
                        help="print the ledger summary and exit")
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--archive", type=Path, default=default_archive_path())
    parser.add_argument("--ledger", type=Path, default=default_ledger_path())
    parser.add_argument("--otm", type=float, default=DEFAULT_OTM,
                        help="short-strike distance below spot (fraction)")
    parser.add_argument("--width", type=float, default=DEFAULT_WIDTH,
                        help="spread width in index points")
    parser.add_argument("--cost-points", type=float,
                        default=DEFAULT_COST_POINTS,
                        help="round-trip cost for both legs, in points")
    parser.add_argument("--lots", type=int, default=1)
    parser.add_argument("--min-strikes", type=int, default=8)
    args = parser.parse_args(argv)

    if args.report:
        return report(args.ledger)

    if not args.archive.exists():
        print(f"missing option archive: {args.archive}")
        return 1
    if not args.index.exists():
        print(f"missing index csv: {args.index}")
        return 1

    spots = load_spot_by_day(args.index)
    conn = sqlite3.connect(str(args.archive))
    days = archived_days(conn)
    if args.date:
        days = [args.date] if args.date in days else []

    if not days:
        print("no sessions matched")
        return 1

    added = skipped = no_spot = no_trade = 0
    for day in days:
        spot = spots.get(day)
        if spot is None:
            print(f"  {day}  no 09:40 spot bar")
            no_spot += 1
            continue
        record = replay_day(
            conn, day, spot, otm=args.otm, width=args.width,
            cost_points=args.cost_points, min_strikes=args.min_strikes,
            lots=args.lots,
        )
        if record is None:
            print(f"  {day}  no expressible spread")
            no_trade += 1
            continue
        if append_record(record, args.ledger):
            added += 1
            print(f"  {day}  {record['short_strike']:.0f}/"
                  f"{record['long_strike']:.0f}  credit {record['credit']:.2f} "
                  f"-> {record['exit_value']:.2f}  net "
                  f"Rs {record['net_inr']:+,.0f}")
        else:
            skipped += 1
            print(f"  {day}  already recorded")

    print()
    print(f"added {added}, already present {skipped}, "
          f"no spot {no_spot}, no trade {no_trade}")
    return report(args.ledger)


def report(ledger: Path) -> int:
    records = read_records(ledger)
    if not records:
        print(f"no shadow records in {ledger}")
        return 0
    stats = summarise(records)
    print(f"SHADOW SHORT PREMIUM — {ledger}")
    print(f"  sessions        : {stats['sessions']}")
    print(f"  green days      : {stats['green']} ({stats['green_pct']:.1f}%)")
    print(f"  net             : {stats['net_points']:+.2f} pts = "
          f"Rs {stats['net_inr']:+,.0f} /lot")
    print(f"  avg per session : Rs {stats['avg_inr']:+,.0f} /lot")
    print(f"  worst / best    : Rs {stats['worst_inr']:,.0f} / "
          f"Rs {stats['best_inr']:,.0f}")
    print(f"  profit factor   : {stats['profit_factor']}")
    print(f"  max drawdown    : {stats['max_drawdown_points']:.2f} pts")
    print(f"  max loss streak : {stats['max_loss_streak']}")
    print()
    print(f"  {'day':<12}{'expiry':<12}{'short':>7}{'credit':>8}"
          f"{'exit':>7}{'net Rs':>10}{'green':>7}")
    for record in sorted(records, key=lambda r: str(r.get("day"))):
        print(f"  {record.get('day',''):<12}{record.get('expiry',''):<12}"
              f"{float(record.get('short_strike') or 0):>7.0f}"
              f"{float(record.get('credit') or 0):>8.2f}"
              f"{float(record.get('exit_value') or 0):>7.2f}"
              f"{float(record.get('net_inr') or 0):>10,.0f}"
              f"{'yes' if record.get('green') else 'no':>7}")
    if stats["sessions"] < 20:
        print()
        print(f"  only {stats['sessions']} sessions — this is a pilot, "
              f"not a verdict (20-30 needed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
