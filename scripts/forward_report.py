"""Forward paper-trading monitor for the options-only stack.

Reads the live/paper audit ledger (``output/trade_log.jsonl``) and answers the
three questions the interim strategy stack needs watched:

  1. How is each sleeve doing?  (trades, win rate, net P&L, profit factor,
     average trade, max drawdown, winning sessions)
  2. How often is an entry blocked, and by what rule?
  3. How often is ORB specifically held back by the 10:00 entry gate?

``trade_log.jsonl`` is append-only, one JSON object per line, written by
``PaperTrader._log_event``.  Options records carry ``asset="options"`` and
closed trades carry ``strategy``/``setup``.  Skip events are now attributed
to a strategy too, so the gate count can distinguish ORB (10:00 floor) from
ORB-Retest (09:45 floor) and mean reversion (no floor).

Usage:
    python scripts/forward_report.py
    python scripts/forward_report.py --date 2026-11-10
    python scripts/forward_report.py --since 2026-11-10 --until 2026-11-21
    python scripts/forward_report.py --days 7 --json output/forward_report.json
    python scripts/forward_report.py --include-test          # include test sidecar

Notes
-----
* The 5-minute dedupe is deliberate: the runtime can re-log the same blocked
  candidate on every scan inside one completed bar.  "Unique buckets" is the
  honest count of how many distinct signals a gate rejected.
* Older records may not have a ``strategy`` field.  Those are shown as
  ``unattributed`` rather than silently dropped or guessed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_LOG = ROOT / "output" / "trade_log.jsonl"
GATE_REASON = "options earliest-entry time not reached"
SLOT_SECONDS = 300  # 5-minute completed-bar bucket


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_ts(value: Any) -> Optional[datetime]:
    """Parse an ISO timestamp and return it in IST, or ``None``."""
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(IST)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result else default  # reject NaN


def _strategy(record: Dict[str, Any]) -> str:
    value = str(
        record.get("strategy")
        or record.get("setup")
        or ""
    ).strip().lower()
    return value or "unattributed"


def _underlying(record: Dict[str, Any]) -> str:
    return str(
        record.get("option_underlying")
        or record.get("signal_underlying")
        or record.get("underlying")
        or ""
    ).strip().upper() or "UNKNOWN"


def _exit_time(record: Dict[str, Any]) -> Optional[datetime]:
    return parse_ts(record.get("closed_at")) or parse_ts(record.get("ts"))


def load_records(path: Path, include_test: bool = False) -> List[Dict[str, Any]]:
    """Read the JSONL ledger, skipping malformed lines and test rows."""
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if not include_test and str(record.get("mode") or "") == "test":
            continue
        records.append(record)
    return records


def filter_window(
    records: Iterable[Dict[str, Any]],
    *,
    date: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Filter records by IST calendar date (inclusive)."""
    selected: List[Dict[str, Any]] = []
    for record in records:
        ts = parse_ts(record.get("ts"))
        if ts is None:
            continue
        stamp = ts.date().isoformat()
        if date and stamp != date:
            continue
        if since and stamp < since:
            continue
        if until and stamp > until:
            continue
        selected.append(record)
    return selected


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def _stats(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(trades)
    if not n:
        return {
            "trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "net": 0.0, "profit_factor": 0.0, "avg": 0.0, "max_dd": 0.0,
            "win_days": 0, "sessions": 0,
        }
    pnls = [_as_float(t.get("pnl")) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    ordered = sorted(trades, key=lambda t: _exit_time(t) or datetime.min.replace(tzinfo=IST))
    cum = peak = dd = 0.0
    by_day: Dict[str, float] = defaultdict(float)
    for trade in ordered:
        pnl = _as_float(trade.get("pnl"))
        cum += pnl
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
        when = _exit_time(trade)
        if when is not None:
            by_day[when.date().isoformat()] += pnl
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / n * 100.0,
        "net": sum(pnls),
        "profit_factor": (gross_win / gross_loss) if gross_loss else float("inf"),
        "avg": sum(pnls) / n,
        "max_dd": dd,
        "win_days": sum(1 for value in by_day.values() if value > 0),
        "sessions": len(by_day),
    }


def _unique_buckets(skips: Iterable[Dict[str, Any]]) -> List[Tuple[Any, ...]]:
    """Collapse repeated scans of the same blocked signal into one bucket.

    A blocked candidate can be re-evaluated on every scan inside a completed
    5-minute bar.  The bucket key is (date, 5m slot, strategy, underlying,
    reason), which is the distinct-signal count rather than the log-line count.
    """
    buckets: set[Tuple[Any, ...]] = set()
    for record in skips:
        ts = parse_ts(record.get("ts"))
        if ts is None:
            continue
        slot = (ts.hour * 3600 + ts.minute * 60 + ts.second) // SLOT_SECONDS
        buckets.add((
            ts.date().isoformat(),
            slot,
            _strategy(record),
            _underlying(record),
            str(record.get("reason") or ""),
        ))
    return sorted(buckets)


def _shadow_summary(signals: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarise log-only shadow signals without counting them as trades.

    One signal can be re-logged on every scan inside the same completed bar,
    so the report counts distinct (day, slot, sleeve, contract, VWAP outcome)
    buckets as well as raw rows.
    """
    records = [r for r in signals if isinstance(r, dict)]
    buckets: set[Tuple[Any, ...]] = set()
    by_strategy: Counter = Counter()
    by_day: Counter = Counter()
    blocked = 0
    collisions = 0
    for record in records:
        ts = parse_ts(record.get("ts")) or parse_ts(record.get("observed_at"))
        if ts is not None:
            slot = (ts.hour * 3600 + ts.minute * 60 + ts.second) // SLOT_SECONDS
            day = ts.date().isoformat()
        else:
            slot, day = -1, ""
        bucket = (
            day, slot, _strategy(record), _underlying(record),
            str(record.get("expiry") or ""),
            _as_float(record.get("strike")),
            str(record.get("kind") or "").upper(),
            bool(record.get("vwap_would_block")),
        )
        if bucket in buckets:
            continue
        buckets.add(bucket)
        by_strategy[_strategy(record)] += 1
        if day:
            by_day[day] += 1
        blocked += 1 if record.get("vwap_would_block") else 0
        collisions += 1 if record.get("collides_with_tradable") else 0
    return {
        "raw": len(records),
        "unique": len(buckets),
        "by_strategy": dict(by_strategy.most_common()),
        "by_day": dict(sorted(by_day.items())),
        "vwap_blocked": blocked,
        "contract_collisions": collisions,
    }


def build_report(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Build the whole report from ledger records (already window-filtered)."""
    options = [r for r in records if r.get("asset") == "options"]
    exits = [r for r in options if r.get("event") in {"exit", "close"}]
    orders = [r for r in options if r.get("event") == "order"]
    skips = [r for r in options if r.get("event") == "skip"]
    shadow = [r for r in options if r.get("event") == "shadow_signal"]

    by_sleeve: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for trade in exits:
        by_sleeve[_strategy(trade)].append(trade)

    by_underlying: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for trade in exits:
        by_underlying[_underlying(trade)].append(trade)

    exit_reasons: Counter = Counter(
        str(trade.get("reason") or "unknown") for trade in exits
    )

    closed_ids = {str(t.get("position_id") or "") for t in exits}
    unclosed = [
        order for order in orders
        if str(order.get("position_id") or "") not in closed_ids
    ]

    reason_counts: Counter = Counter(
        str(skip.get("reason") or "unknown") for skip in skips
    )
    gate_skips = [s for s in skips if str(s.get("reason") or "") == GATE_REASON]
    gate_buckets = _unique_buckets(gate_skips)

    gate_by_sleeve: Counter = Counter(bucket[2] for bucket in gate_buckets)
    gate_by_underlying: Counter = Counter(bucket[3] for bucket in gate_buckets)
    gate_by_day: Counter = Counter(bucket[0] for bucket in gate_buckets)

    window_days = sorted({
        parse_ts(r.get("ts")).date().isoformat()
        for r in records
        if parse_ts(r.get("ts")) is not None
    })

    return {
        "window": {
            "start": window_days[0] if window_days else "",
            "end": window_days[-1] if window_days else "",
            "sessions": len(window_days),
        },
        "sleeves": {name: _stats(trades) for name, trades in sorted(by_sleeve.items())},
        "overall": _stats(exits),
        "by_underlying": {
            name: _stats(trades) for name, trades in sorted(by_underlying.items())
        },
        "exit_reasons": dict(exit_reasons.most_common()),
        "skips": {
            "raw": len(skips),
            "unique": len(_unique_buckets(skips)),
            "reasons": dict(reason_counts.most_common()),
        },
        "gate_blocks": {
            "reason": GATE_REASON,
            "raw": len(gate_skips),
            "unique": len(gate_buckets),
            "by_sleeve": dict(gate_by_sleeve.most_common()),
            "by_underlying": dict(gate_by_underlying.most_common()),
            "by_day": dict(sorted(gate_by_day.items())),
        },
        "orders": {"count": len(orders), "unclosed": len(unclosed)},
        "shadow": _shadow_summary(shadow),
    }


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _pf(value: float) -> str:
    return "inf" if value == float("inf") else f"{value:.2f}"


def _sleeve_table(rows: Dict[str, Dict[str, Any]], title: str,
                  first_label: str = "Sleeve") -> str:
    lines = [title, "-" * 92]
    lines.append(
        f"{first_label:<20}{'Trades':>7}{'Win%':>7}{'Net Rs':>13}"
        f"{'PF':>7}{'Avg':>9}{'MaxDD':>12}{'WinDays':>10}"
    )
    lines.append("-" * 92)
    if not rows:
        lines.append("(no closed trades in window)")
    for name, s in rows.items():
        lines.append(
            f"{name:<20}{s['trades']:>7}{s['win_rate']:>6.1f}%"
            f"{s['net']:>13,.0f}{_pf(s['profit_factor']):>7}"
            f"{s['avg']:>9,.0f}{s['max_dd']:>12,.0f}"
            f"{s['win_days']:>4}/{s['sessions']:<5}"
        )
    lines.append("-" * 92)
    return "\n".join(lines)


def format_report(report: Dict[str, Any], *, log_path: str = "") -> str:
    window = report["window"]
    lines: List[str] = []
    lines.append("FORWARD OPTIONS PAPER REPORT")
    lines.append("=" * 92)
    if log_path:
        lines.append(f"Ledger : {log_path}")
    lines.append(
        f"Window : {window['start'] or '-'} .. {window['end'] or '-'} "
        f"({window['sessions']} session(s), paper mode only)"
    )
    lines.append("")

    lines.append(_sleeve_table(report["sleeves"], "PER-SLEEVE PERFORMANCE (closed trades)"))
    overall = report["overall"]
    lines.append(
        f"\nOVERALL: {overall['trades']} trades | {overall['win_rate']:.1f}% win | "
        f"net Rs {overall['net']:,.0f} | PF {_pf(overall['profit_factor'])} | "
        f"MaxDD Rs {overall['max_dd']:,.0f}"
    )

    lines.append("")
    lines.append(_sleeve_table(report["by_underlying"], "BY UNDERLYING (closed trades)",
                              first_label="Underlying"))

    lines.append("")
    lines.append("ENTRY-TIME GATE (10:00 ORB / 09:45 ORB-Retest)")
    lines.append("-" * 92)
    gate = report["gate_blocks"]
    lines.append(
        f"Raw skip events: {gate['raw']} | Unique blocked signals: {gate['unique']}"
    )
    if gate["by_sleeve"]:
        parts = ", ".join(f"{k}={v}" for k, v in gate["by_sleeve"].items())
        lines.append(f"By sleeve      : {parts}")
    else:
        lines.append("By sleeve      : (none yet - gate has not rejected a candidate)")
    if gate["by_underlying"]:
        parts = ", ".join(f"{k}={v}" for k, v in gate["by_underlying"].items())
        lines.append(f"By underlying  : {parts}")
    if gate["by_day"]:
        parts = ", ".join(f"{k}={v}" for k, v in gate["by_day"].items())
        lines.append(f"By day         : {parts}")

    lines.append("")
    lines.append("SHADOW SIGNALS (log-only; never traded)")
    lines.append("-" * 92)
    shadow = report.get("shadow") or {}
    lines.append(
        f"Raw shadow events: {shadow.get('raw', 0)} | "
        f"Unique shadow signals: {shadow.get('unique', 0)}"
    )
    if shadow.get("by_strategy"):
        parts = ", ".join(f"{k}={v}" for k, v in shadow["by_strategy"].items())
        lines.append(f"By sleeve      : {parts}")
    else:
        lines.append("By sleeve      : (no shadow signals in window)")
    lines.append(
        f"VWAP would block: {shadow.get('vwap_blocked', 0)} | "
        f"Contract collision: {shadow.get('contract_collisions', 0)}"
    )
    if shadow.get("by_day"):
        parts = ", ".join(f"{k}={v}" for k, v in shadow["by_day"].items())
        lines.append(f"By day         : {parts}")

    lines.append("")
    lines.append("SKIPPED SIGNALS BY REASON")
    lines.append("-" * 92)
    if report["skips"]["reasons"]:
        for reason, count in report["skips"]["reasons"].items():
            marker = "  <- entry-time gate" if reason == GATE_REASON else ""
            lines.append(f"{count:>5}  {reason}{marker}")
        lines.append(
            f"      ({report['skips']['raw']} raw events -> "
            f"{report['skips']['unique']} unique signals)"
        )
    else:
        lines.append("(no skips in window)")

    lines.append("")
    lines.append("EXIT REASONS")
    lines.append("-" * 92)
    if report["exit_reasons"]:
        for reason, count in report["exit_reasons"].items():
            lines.append(f"{count:>5}  {reason}")
    else:
        lines.append("(no exits in window)")

    lines.append("")
    lines.append(
        f"Orders: {report['orders']['count']} | "
        f"still open / unclosed: {report['orders']['unclosed']}"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _date_arg(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from exc


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", default=str(DEFAULT_LOG),
                        help="path to trade_log.jsonl")
    parser.add_argument("--date", type=_date_arg, help="single IST date")
    parser.add_argument("--since", type=_date_arg, help="inclusive start date")
    parser.add_argument("--until", type=_date_arg, help="inclusive end date")
    parser.add_argument("--days", type=int,
                        help="last N IST dates present in the ledger")
    parser.add_argument("--include-test", action="store_true",
                        help="include mode=test rows (default excludes them)")
    parser.add_argument("--json", help="write the report as JSON to this path")
    args = parser.parse_args(argv)

    log_path = Path(args.log)
    records = load_records(log_path, include_test=args.include_test)

    since, until = args.since, args.until
    if args.days is not None:
        if args.days <= 0:
            parser.error("--days must be positive")
        available = sorted({
            parse_ts(r.get("ts")).date().isoformat()
            for r in records if parse_ts(r.get("ts")) is not None
        })
        if available:
            since = max(since, available[-args.days]) if since else available[-args.days]
            until = min(until, available[-1]) if until else available[-1]

    filtered = filter_window(records, date=args.date, since=since, until=until)
    report = build_report(filtered)
    print(format_report(report, log_path=str(log_path)))

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8"
        )
        print(f"\nSaved JSON: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
