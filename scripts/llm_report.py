"""Score the shadow LLM judge against what the system actually did.

Reads ``output/trade_log.jsonl`` and joins three event types:

    llm_verdict  (bar_ts, underlying, strategy, direction, verdict)
    order        (execution time, contract, position_id)
    exit         (position_id, pnl, reason)

A verdict is matched to the first order on the same underlying, strategy and
direction that lands within ``--window`` minutes after the verdict's bar.
Unmatched verdicts are reported separately: those are signals the entry gates
rejected, and they are the most interesting bucket when asking whether the LLM
would have changed the day.

Usage
-----
    python scripts/llm_report.py
    python scripts/llm_report.py --start 2026-11-11 --end 2026-11-11
    python scripts/llm_report.py --json output/llm_report.json
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = ROOT / "output" / "trade_log.jsonl"


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number else default


def _percentile(values: List[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return float(ordered[index])


def load_events(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def build_report(events: List[Dict[str, Any]], *, window_minutes: float = 20.0,
                 start: Optional[str] = None, end: Optional[str] = None) -> Dict[str, Any]:
    orders = [row for row in events if row.get("event") == "order"]
    exits = {str(row.get("position_id")): row for row in events
             if row.get("event") == "exit"}
    verdicts = [row for row in events if row.get("event") == "llm_verdict"]

    def in_range(ts: Optional[datetime]) -> bool:
        if ts is None:
            return False
        day = ts.astimezone(timezone(timedelta(hours=5, minutes=30))).date().isoformat()
        if start and day < start:
            return False
        if end and day > end:
            return False
        return True

    verdicts = [row for row in verdicts if in_range(_parse_ts(row.get("bar_ts")))]
    orders = [row for row in orders if in_range(_parse_ts(row.get("ts")))]

    by_bucket: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"trades": 0, "wins": 0, "pnl": 0.0, "r": 0.0, "trades_detail": []})
    status_counts: Counter = Counter()
    keys_used: Counter = Counter()
    latencies: List[float] = []
    matched_orders = set()
    unmatched = 0
    neutral = 0
    unusable = 0

    for verdict in verdicts:
        status = str(verdict.get("status") or "unknown")
        status_counts[status] += 1
        key_id = verdict.get("key_id")
        if key_id:
            keys_used[str(key_id)] += 1
        latency = _finite(verdict.get("latency_ms"), -1.0)
        if latency >= 0:
            latencies.append(latency)

        if status != "ok":
            # A failed or skipped call produced no opinion.  Counting it as
            # "neutral" would blur the one bucket that is supposed to mean
            # "the model looked at this and saw no edge".
            unusable += 1
            continue

        payload = verdict.get("verdict") or {}
        agrees = payload.get("agrees")
        if agrees is None:
            neutral += 1
        bucket = "neutral" if agrees is None else ("agreed" if agrees else "disagreed")
        by_bucket[bucket]["trades"] += 0  # ensure bucket exists

        bar_ts = _parse_ts(verdict.get("bar_ts"))
        if bar_ts is None:
            unmatched += 1
            continue
        deadline = bar_ts + timedelta(minutes=window_minutes)
        underlying = str(verdict.get("underlying") or "").upper()
        strategy = str(verdict.get("strategy") or "").lower()
        direction = str(verdict.get("direction") or "").lower()

        match = None
        for order in orders:
            order_ts = _parse_ts(order.get("ts"))
            if order_ts is None or order_ts < bar_ts or order_ts > deadline:
                continue
            if str(order.get("option_underlying") or "").upper() != underlying:
                continue
            if str(order.get("strategy") or "").lower() != strategy:
                continue
            if str(order.get("direction") or "").lower() != direction:
                continue
            if str(order.get("position_id")) in matched_orders:
                continue
            match = order
            break

        if match is None:
            unmatched += 1
            continue
        matched_orders.add(str(match.get("position_id")))
        exit_row = exits.get(str(match.get("position_id"))) or {}
        pnl = _finite(exit_row.get("pnl"))
        risk = _finite(match.get("risk_amount"))
        won = pnl > 0

        bucket_data = by_bucket[bucket]
        bucket_data["trades"] += 1
        bucket_data["wins"] += 1 if won else 0
        bucket_data["pnl"] += pnl
        bucket_data["r"] += (pnl / risk) if risk > 0 else 0.0
        bucket_data["trades_detail"].append({
            "position_id": match.get("position_id"),
            "strategy": strategy,
            "direction": direction,
            "bar_ts": verdict.get("bar_ts"),
            "bias": payload.get("bias"),
            "confidence": payload.get("confidence"),
            "reason_code": payload.get("reason_code"),
            "entry_premium": match.get("entry_premium"),
            "exit_bid": exit_row.get("exit_bid"),
            "exit_reason": exit_row.get("reason"),
            "pnl": round(pnl, 2),
            "r": round((pnl / risk) if risk > 0 else 0.0, 3),
        })

    summary = {}
    for bucket in ("agreed", "disagreed", "neutral"):
        data = by_bucket.get(bucket) or {"trades": 0, "wins": 0, "pnl": 0.0, "r": 0.0}
        trades = int(data["trades"])
        summary[bucket] = {
            "trades": trades,
            "win_rate": round(100.0 * data["wins"] / trades, 1) if trades else None,
            "pnl": round(float(data["pnl"]), 2),
            "r": round(float(data["r"]), 3),
        }

    return {
        "verdicts": len(verdicts),
        "verdict_status": dict(status_counts),
        "matched_orders": len(matched_orders),
        "unmatched_signals": unmatched,
        "neutral_verdicts": neutral,
        "unusable_verdicts": unusable,
        "keys_used": dict(keys_used),
        "latency_ms": {
            "p50": round(_percentile(latencies, 0.50), 1),
            "p95": round(_percentile(latencies, 0.95), 1),
            "max": round(max(latencies), 1) if latencies else 0.0,
        },
        "buckets": summary,
        "detail": by_bucket,
    }


def _print_report(report: Dict[str, Any], window_minutes: float) -> None:
    print("Shadow LLM judge report")
    print("=" * 60)
    print(f"verdicts logged        : {report['verdicts']}")
    print(f"matched to an order    : {report['matched_orders']} "
          f"(window {window_minutes:g} min)")
    print(f"signals with no order  : {report['unmatched_signals']}")
    print(f"neutral verdicts       : {report['neutral_verdicts']}")
    print(f"unusable verdicts      : {report['unusable_verdicts']} "
          f"(failed/skipped calls, no opinion)")
    print(f"status                 : {report['verdict_status']}")
    print(f"keys used              : {report['keys_used']}")
    latency = report["latency_ms"]
    print(f"latency ms p50/p95/max : {latency['p50']} / {latency['p95']} / {latency['max']}")
    print()
    print(f"{'bucket':<12} {'trades':>7} {'win%':>7} {'P&L':>12} {'R':>8}")
    for bucket in ("agreed", "disagreed", "neutral"):
        row = report["buckets"][bucket]
        win = "-" if row["win_rate"] is None else f"{row['win_rate']:.0f}%"
        print(f"{bucket:<12} {row['trades']:>7} {win:>7} "
              f"{row['pnl']:>12,.2f} {row['r']:>8.2f}")
    print()
    print("Reading this: only after ~20 matched trades in each of 'agreed' and")
    print("'disagreed' is the gap meaningful.  Fewer than that is noise.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Join shadow llm_verdict events to orders/exits and score them.")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--start", default=None, help="first IST date, YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="last IST date, YYYY-MM-DD")
    parser.add_argument("--window", type=float, default=20.0,
                        help="minutes after a verdict's bar to match an order")
    parser.add_argument("--json", dest="json_out", type=Path, default=None,
                        help="write the full report as JSON")
    return parser


def main(argv: Any = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.log.exists():
        print(f"log not found: {args.log}")
        return 1
    events = load_events(args.log)
    report = build_report(events, window_minutes=args.window,
                          start=args.start, end=args.end)
    report["window_minutes"] = args.window
    report["log"] = str(args.log)
    _print_report(report, args.window)
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
