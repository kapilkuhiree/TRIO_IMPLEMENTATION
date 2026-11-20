"""Audit live options entries against the corrected ORB + true-VWAP rules.

Usage
-----
    # one session (run after the 15:10 flat)
    python scripts/audit_live_vwap.py --date 2026-11-11

    # every live entry whose futures session the broker still serves
    python scripts/audit_live_vwap.py --cumulative

    # what the audit ledger says so far
    python scripts/audit_live_vwap.py --report

This never places, sizes, blocks or alters an order, and the live runtime does
not import it.  It joins the live trade log to the NIFTY futures series,
recomputes the opening range and cumulative true VWAP as of each entry, and
labels the entry as `survives`, `duplicate_entry`, `second_setup`,
`vwap_blocked`, `basis_mismatch`, `no_confirmation`, `outside_window` or
`data_issue`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.live_vwap_audit import (  # noqa: E402
    CLASSES,
    DEFAULT_CONFIRM_CLOSES,
    LiveEntry,
    append_record,
    audit_session,
    default_ledger_path,
    default_live_state_dir,
    default_live_state_path,
    default_trade_log_path,
    find_runtime_logs,
    load_entry_times_from_logs,
    load_live_entries,
    load_live_pnl,
    load_spot_bars,
    load_state_entries,
    read_records,
    summarise,
)
from src.shadow_orb_vwap import (  # noqa: E402
    FuturesUnavailable,
    group_sessions,
    load_futures_bars,
    normalise_bars,
)


def _load_csv(path: Path) -> List[Dict[str, Any]]:
    import pandas as pd

    frame = pd.read_csv(path)
    frame = frame.rename(columns={
        "Date": "date", "timestamp": "date", "Timestamp": "date",
        "Open": "open", "High": "high", "Low": "low", "Close": "close",
        "Volume": "volume",
    })
    return normalise_bars(frame.to_dict("records"))


def _entries_by_day(entries: Sequence[LiveEntry]) -> Dict[str, List[LiveEntry]]:
    grouped: Dict[str, List[LiveEntry]] = {}
    for entry in entries:
        grouped.setdefault(entry.day, []).append(entry)
    return grouped


def _merge_entries(groups: Sequence[Sequence[LiveEntry]]) -> List[LiveEntry]:
    """Union of trade-log and state-file entries, keyed like the ledger."""
    merged: Dict[Tuple[str, str], LiveEntry] = {}
    for group in groups:
        for entry in group:
            key = (entry.position_id, entry.stamp.isoformat())
            previous = merged.get(key)
            if previous is None:
                merged[key] = entry
                continue
            merged[key] = LiveEntry(
                day=previous.day,
                stamp=previous.stamp,
                strategy=(previous.strategy
                          if previous.strategy not in ("", "?")
                          else entry.strategy),
                direction=previous.direction,
                position_id=previous.position_id,
                entry_premium=(previous.entry_premium
                               if previous.entry_premium is not None
                               else entry.entry_premium),
                kind=previous.kind or entry.kind,
                strike=(previous.strike if previous.strike is not None
                        else entry.strike),
                underlying=previous.underlying or entry.underlying,
            )
    return sorted(merged.values(), key=lambda item: item.stamp)


def _state_day(path: Path) -> str:
    """Session date of a state file: its ``session_date`` or its filename."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        day = str(payload.get("session_date") or "")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            return day
    except (OSError, ValueError):
        pass
    match = re.search(r"\d{4}-\d{2}-\d{2}", path.name)
    return match.group(0) if match else ""


def audit_days(
    days: Sequence[str],
    *,
    bars: Sequence[Mapping[str, Any]],
    spot_bars: Optional[Sequence[Mapping[str, Any]]],
    entries: Mapping[str, Sequence[LiveEntry]],
    pnl: Mapping[str, float],
    confirm_closes: int,
) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for day in days:
        records.extend(audit_session(
            list(entries.get(day, [])),
            bars,
            spot_bars=spot_bars,
            confirm_closes=confirm_closes,
            pnl_by_entry=pnl,
        ))
    return records


def record(rows: Sequence[Mapping[str, Any]], ledger: Path) -> int:
    added = 0
    for row in rows:
        was_added = append_record(row, ledger)
        added += int(was_added)
        print(f"  {'+' if was_added else '='} {_line(row)}")
    print(f"\nadded {added}, already present {len(rows) - added}")
    return added


def _line(row: Mapping[str, Any]) -> str:
    decision = str(row.get("classification"))
    parts = [
        str(row.get("day")),
        str(row.get("entry_time")),
        str(row.get("direction")),
        str(row.get("strategy")),
        f"{decision:<16}",
    ]
    if row.get("futures_close") is not None:
        parts.append(f"fut {row['futures_close']:>9.2f}")
    if row.get("vwap_at_entry") is not None:
        parts.append(f"vwap {row['vwap_at_entry']:>9.2f}")
    if row.get("corrected_entry_time"):
        delay = row.get("delay_minutes")
        parts.append(f"corrected {row['corrected_entry_time']}"
                     + (f" (+{delay}m)" if delay else ""))
    if row.get("pnl_inr") is not None:
        parts.append(f"pnl {row['pnl_inr']:+,.0f}")
    if row.get("detail"):
        parts.append(f"| {row['detail']}")
    return "  ".join(parts)


def report(ledger: Path) -> int:
    rows = read_records(ledger)
    if not rows:
        print(f"no audit records in {ledger}")
        return 0
    stats = summarise(rows)
    underlyings = sorted({str(row.get("underlying")) for row in rows
                          if row.get("underlying")})
    scope = f" ({', '.join(underlyings)})" if underlyings else ""
    print(f"\nLIVE vs CORRECTED ORB + TRUE VWAP - {ledger}")
    print(f"entries audited: {stats['entries']} across {stats['days']} "
          f"session(s){scope}")
    header = (f"  {'date':<12}{'live':>5}{'surv':>5}{'late':>5}{'dup':>5}"
              f"{'set2':>5}{'vwap':>6}{'basis':>6}{'noConf':>7}"
              f"{'window':>7}{'issue':>6}{'liveRs':>10}")
    print(header)
    by_day: Dict[str, List[Mapping[str, Any]]] = {}
    for row in rows:
        by_day.setdefault(str(row.get("day")), []).append(row)
    for day in sorted(by_day):
        day_rows = by_day[day]
        day_stats = summarise(day_rows)
        counts = day_stats["counts"]
        print(
            f"  {day:<12}{len(day_rows):>5}"
            f"{counts['survives']:>5}{counts['late_entry']:>5}"
            f"{counts['duplicate_entry']:>5}"
            f"{counts['second_setup']:>5}{counts['vwap_blocked']:>6}"
            f"{day_stats['basis_flags']:>6}{counts['no_confirmation']:>7}"
            f"{counts['outside_window']:>7}{counts['data_issue']:>6}"
            f"{day_stats['live_net_inr']:>10,.0f}"
        )
    counts = stats["counts"]
    print(
        f"  {'TOTAL':<12}{stats['entries']:>5}"
        f"{counts['survives']:>5}{counts['late_entry']:>5}"
        f"{counts['duplicate_entry']:>5}"
        f"{counts['second_setup']:>5}{counts['vwap_blocked']:>6}"
        f"{stats['basis_flags']:>6}{counts['no_confirmation']:>7}"
        f"{counts['outside_window']:>7}{counts['data_issue']:>6}"
        f"{stats['live_net_inr']:>10,.0f}"
    )
    print("\nlegend: surv=exact corrected entry, late=first live entry on the "
          "leg (late),\n        dup=same-leg repeat, set2=re-armed second "
          "setup, vwap=blocked by true VWAP,\n        basis=futures range not "
          "confirmed while spot was (flagged even on repeats),\n"
          "        noConf=no two-close confirmation, window=outside "
          "10:00-14:30, liveRs=realised live P&L")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", help="single session, YYYY-MM-DD")
    parser.add_argument("--cumulative", action="store_true",
                        help="audit every live entry the futures series covers")
    parser.add_argument("--report", action="store_true",
                        help="print the audit ledger summary")
    parser.add_argument("--underlying", default="NIFTY")
    parser.add_argument("--period", default="30d",
                        help="broker lookback for the futures series")
    parser.add_argument("--trade-log", type=Path,
                        default=default_trade_log_path())
    parser.add_argument("--state", type=Path,
                        help="live runtime state file (default: "
                             "%%LOCALAPPDATA%%\\TRIO\\options_intraday"
                             "\\live_<date>.json)")
    parser.add_argument("--no-state", action="store_true",
                        help="audit only the trade log, not the live state")
    parser.add_argument("--runtime-log", type=Path, action="append",
                        help="runtime log used to recover entry times missing "
                             "from an old state file (default: logs for --date)")
    parser.add_argument("--ledger", type=Path, default=default_ledger_path())
    parser.add_argument("--futures-csv", type=Path,
                        help="offline futures CSV instead of the broker")
    parser.add_argument("--spot-csv", type=Path,
                        help="offline spot index CSV instead of yfinance")
    parser.add_argument("--no-spot", action="store_true",
                        help="skip the spot series (disables basis detection)")
    parser.add_argument("--confirm-closes", type=int,
                        default=DEFAULT_CONFIRM_CLOSES)
    args = parser.parse_args(argv)

    if args.report:
        return report(args.ledger)

    log_entries = load_live_entries(args.trade_log, underlying=args.underlying)
    pnl = load_live_pnl(log_entries, args.trade_log)

    state_paths: List[Path] = []
    if not args.no_state:
        if args.state:
            if not args.state.exists():
                print(f"live state file not found: {args.state}")
                return 2
            state_paths = [args.state]
        elif args.date:
            candidate = default_live_state_path(args.date)
            if candidate.exists():
                state_paths = [candidate]
        else:
            directory = default_live_state_dir()
            if directory.is_dir():
                state_paths = sorted(directory.glob("live_*.json"))

    state_entries: List[LiveEntry] = []
    unresolved: List[Dict[str, Any]] = []
    for state_path in state_paths:
        day = _state_day(state_path)
        logs = (args.runtime_log
                or (find_runtime_logs(day) if day else []))
        found, state_pnl, missing = load_state_entries(
            state_path,
            underlying=args.underlying,
            entry_stamps=load_entry_times_from_logs(logs),
        )
        state_entries.extend(found)
        pnl.update(state_pnl)
        unresolved.extend(missing)

    entries = _merge_entries((log_entries, state_entries))
    if unresolved:
        print(f"{len(unresolved)} closed live leg(s) have no entry timestamp "
              f"and were not audited:")
        for item in unresolved:
            print(f"  {item['position_id']} @ {item['entry_premium']} "
                  f"(closed {item['closed_at']})")
    if not entries:
        print(f"no live options entries in {args.trade_log} or the live state")
        return 1
    grouped = _entries_by_day(entries)

    if args.futures_csv:
        bars = _load_csv(args.futures_csv)
        symbol = args.futures_csv.name
    else:
        try:
            symbol, bars = load_futures_bars(
                args.underlying, timeframe="5m", period=args.period
            )
        except FuturesUnavailable as exc:
            print(f"futures unavailable: {exc}")
            print("no record written - the audit needs a volume-bearing series")
            return 2

    covered = set(group_sessions(bars))
    available = [day for day in sorted(grouped) if day in covered]

    if args.date:
        if args.date not in grouped:
            print(f"no live options entries on {args.date}")
            return 1
        if args.date not in covered:
            print(f"no futures session for {args.date} in {symbol}")
            print("cannot audit without the same instrument; nothing written")
            return 1
        days = [args.date]
    elif args.cumulative:
        days = available
        skipped = [day for day in sorted(grouped) if day not in covered]
    else:
        days = available[-1:]
        skipped = [day for day in sorted(grouped) if day not in covered]

    if not days:
        print("no auditable session: the live log has no entry covered by futures")
        return 1

    spot_bars: Optional[Sequence[Mapping[str, Any]]] = None
    if not args.no_spot:
        if args.spot_csv:
            spot_bars = _load_csv(args.spot_csv)
        else:
            spot_bars = load_spot_bars(args.underlying, period=args.period)
        if not spot_bars:
            print("spot series unavailable - basis detection disabled "
                  "(entries are classified on futures only)")

    print(f"{symbol}: {len(bars)} bars, {len(covered)} sessions")
    if spot_bars:
        print(f"spot: {len(spot_bars)} bars")
    print(f"auditing {len(days)} session(s), "
          f"{sum(len(grouped[day]) for day in days)} live entr(ies)")
    if args.cumulative and skipped:
        print(f"not covered by the futures series: {', '.join(skipped)}")

    rows = audit_days(
        days,
        bars=bars,
        spot_bars=spot_bars,
        entries=grouped,
        pnl=pnl,
        confirm_closes=args.confirm_closes,
    )
    record(rows, args.ledger)
    return report(args.ledger)


if __name__ == "__main__":
    raise SystemExit(main())
