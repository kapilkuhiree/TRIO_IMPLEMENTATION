"""Replay logged ``shadow_signal`` rows on real archived option bid/ask.

Stage 1 shadow mode writes log-only MR signals.  This tool turns those rows
into a hypothetical P&L report by replaying each distinct signal on the exact
strike family present in the local option archive.  It never places an order
and never writes to the runtime state.

The signal identity comes from the shadow ledger (underlying, expiry, strike,
CE/PE, observed time).  ``scripts.premium_replay`` does the pricing: next
option candle's ask on entry, staged bid ladder (-20% stop, +10%/+20% ratchet,
+30% target, same-day hard flat), executable quotes only.

    python scripts/shadow_replay.py
    python scripts/shadow_replay.py --start 2026-11-11 --end 2026-11-11
    python scripts/shadow_replay.py --strategies mean_reversion --json output/shadow_replay.json

If the archive does not yet contain the strike the shadow signal selected, the
signal is reported as a miss (``strike_drift`` / ``no_contract``); widen the
read-only capture with per-underlying ``--strike-range`` values to make future
signals replayable.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.premium_replay import replay_trade  # noqa: E402
from src.zerodha_option_archive import default_archive_path  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
SLOT_SECONDS = 300
DEFAULT_LOG = ROOT / "output" / "trade_log.jsonl"


def parse_ts(value: Any) -> Optional[datetime]:
    """Parse a ledger timestamp and normalise it to IST."""
    if isinstance(value, datetime):
        stamp = value
    elif value in (None, ""):
        return None
    else:
        try:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(IST)


def load_records(path: Path, include_test: bool = False) -> List[Dict[str, Any]]:
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


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def build_shadow_trades(
    records: Iterable[Dict[str, Any]],
    *,
    start: Optional[str] = None,
    end: Optional[str] = None,
    strategies: Optional[Sequence[str]] = None,
) -> tuple[List[Dict[str, Any]], int]:
    """Return one replayable trade per distinct shadow signal.

    A signal is re-logged on every scan inside a completed 5-minute bar, so the
    distinct-signal count is deduplicated by (day, slot, sleeve, contract).
    """
    wanted = {str(item).strip().lower() for item in (strategies or []) if str(item).strip()}
    seen: set[tuple] = set()
    trades: List[Dict[str, Any]] = []
    raw = 0
    for record in records:
        if str(record.get("event") or "") != "shadow_signal":
            continue
        raw += 1
        strategy = str(record.get("strategy") or "").strip().lower()
        if wanted and strategy not in wanted:
            continue
        when = parse_ts(record.get("observed_at")) or parse_ts(record.get("ts"))
        if when is None:
            continue
        day = when.date().isoformat()
        if start and day < start:
            continue
        if end and day > end:
            continue
        underlying = str(record.get("underlying") or "").strip().upper()
        expiry = str(record.get("expiry") or "").strip().upper()
        strike = _finite(record.get("strike"))
        kind = str(record.get("kind") or "").strip().upper()
        slot = (when.hour * 3600 + when.minute * 60 + when.second) // SLOT_SECONDS
        key = (day, slot, strategy, underlying, expiry, strike, kind)
        if key in seen:
            continue
        seen.add(key)
        trades.append({
            "strategy": strategy or "unattributed",
            "symbol": underlying,
            "kind": kind,
            "strike": strike,
            "expiry": expiry,
            "day": day,
            "signal_ts": when.isoformat(),
            "observed_ts": when.isoformat(),
            "entry_ts": when.isoformat(),
            "lots": 1,
            "vwap_would_block": bool(record.get("vwap_would_block")),
            "collides_with_tradable": bool(record.get("collides_with_tradable")),
            "ask": _finite(record.get("ask")),
            "trading_symbol": record.get("trading_symbol"),
        })
    trades.sort(key=lambda item: (item["signal_ts"], item["strategy"], item["symbol"]))
    return trades, raw


def _stats(values: Iterable[float],
           stamps: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    values = [float(item) for item in values]
    n = len(values)
    if not n:
        return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "net": 0.0, "profit_factor": 0.0, "avg": 0.0, "max_dd": 0.0}
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value <= 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    ordered = values
    if stamps is not None:
        ordered = [value for _, value in
                   sorted(zip(list(stamps), values), key=lambda item: item[0])]
    cumulative = peak = drawdown = 0.0
    for value in ordered:
        cumulative += value
        peak = max(peak, cumulative)
        drawdown = min(drawdown, cumulative - peak)
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / n * 100.0,
        "net": sum(values),
        "profit_factor": (gross_profit / gross_loss) if gross_loss else float("inf"),
        "avg": sum(values) / n,
        "max_dd": drawdown,
    }


def _pf(value: float) -> str:
    return "inf" if value == float("inf") else f"{value:.2f}"


def build_report(
    pairs: Sequence[Dict[str, Any]],
    *,
    trades: Sequence[Dict[str, Any]],
    raw: int,
    start: Optional[str],
    end: Optional[str],
    archive_path: Path,
    log_path: Path,
) -> Dict[str, Any]:
    hits = [pair for pair in pairs if pair["status"] == "matched"]
    misses = [pair for pair in pairs if pair["status"] == "miss"]
    real = [pair["real"] for pair in hits]
    net_stats = _stats([float(item["net_pnl"]) for item in real],
                       [item["entry_timestamp"] for item in real])
    gross_stats = _stats([float(item["gross_pnl"]) for item in real],
                         [item["entry_timestamp"] for item in real])

    by_strategy: Dict[str, Dict[str, Any]] = {}
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for pair in pairs:
        grouped[pair["trade"]["strategy"]].append(pair)
    for name, group in sorted(grouped.items()):
        group_hits = [pair for pair in group if pair["status"] == "matched"]
        group_real = [pair["real"] for pair in group_hits]
        by_strategy[name] = {
            "signals": len(group),
            "matched": len(group_hits),
            "match_rate": len(group_hits) / len(group) * 100.0 if group else 0.0,
            "hypothetical": _stats([float(item["net_pnl"]) for item in group_real],
                                   [item["entry_timestamp"] for item in group_real]),
            "miss_reasons": dict(Counter(pair.get("reason", "no_trade")
                                         for pair in group
                                         if pair["status"] == "miss").most_common()),
        }

    by_day: Dict[str, Dict[str, Any]] = {}
    day_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for pair in pairs:
        day_groups[pair["trade"]["day"]].append(pair)
    for day, group in sorted(day_groups.items()):
        group_hits = [pair for pair in group if pair["status"] == "matched"]
        group_real = [pair["real"] for pair in group_hits]
        by_day[day] = {
            "signals": len(group),
            "matched": len(group_hits),
            "net": sum(float(item["net_pnl"]) for item in group_real),
        }

    days = sorted({pair["trade"]["day"] for pair in pairs})
    return {
        "window": {
            "start": start or (days[0] if days else ""),
            "end": end or (days[-1] if days else ""),
            "sessions": len(days),
        },
        "archive": str(archive_path),
        "log": str(log_path),
        "shadow": {
            "raw_events": raw,
            "signals": len(trades),
            "replayed": len(pairs),
            "matched": len(hits),
            "match_rate": (len(hits) / len(pairs) * 100.0) if pairs else 0.0,
            "hypothetical_net": net_stats,
            "hypothetical_gross": gross_stats,
            "miss_reasons": dict(Counter(pair.get("reason", "no_trade")
                                         for pair in misses).most_common()),
        },
        "by_strategy": by_strategy,
        "by_day": by_day,
        "pairs": list(pairs),
    }


def format_report(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    rule = "-" * 104
    window = report["window"]
    shadow = report["shadow"]
    lines.append("SHADOW SIGNAL REPLAY: HYPOTHETICAL OUTCOMES ON REAL QUOTES")
    lines.append("=" * 104)
    lines.append(f"Ledger   : {report['log']}")
    lines.append(f"Archive  : {report['archive']}")
    lines.append(f"Window   : {window['start'] or '-'} .. {window['end'] or '-'} "
                 f"({window['sessions']} session(s))")
    lines.append(f"Signals  : {shadow['raw_events']} raw rows | "
                 f"{shadow['signals']} distinct | "
                 f"{shadow['matched']} matched executable quotes "
                 f"({shadow['match_rate']:.1f}%)")
    lines.append("")
    net = shadow["hypothetical_net"]
    gross = shadow["hypothetical_gross"]
    lines.append("HYPOTHETICAL PERFORMANCE (matched shadow signals, 1 lot each)")
    lines.append(rule)
    lines.append(f"{'Trades':>7}{'Win%':>7}{'Gross Rs':>14}{'Net Rs':>14}"
                 f"{'PF':>7}{'Avg Rs':>12}{'MaxDD Rs':>14}")
    lines.append(
        f"{net['trades']:>7}{net['win_rate']:>6.1f}%{gross['net']:>14,.0f}"
        f"{net['net']:>14,.0f}{_pf(net['profit_factor']):>7}"
        f"{net['avg']:>12,.0f}{net['max_dd']:>14,.0f}"
    )
    lines.append(rule)
    lines.append("Gross is raw bid/ask; net adds brokerage and tax.  One lot is a")
    lines.append("normalised unit, not a position-size recommendation.")
    lines.append("")

    lines.append("BY SLEEVE")
    lines.append(rule)
    if report["by_strategy"]:
        for name, item in report["by_strategy"].items():
            stats = item["hypothetical"]
            lines.append(
                f"{name:<18} signals={item['signals']:>4} "
                f"matched={item['matched']:>4} ({item['match_rate']:>5.1f}%) "
                f"net={stats['net']:>10,.0f} PF={_pf(stats['profit_factor'])}"
            )
            if item["miss_reasons"]:
                reasons = ", ".join(f"{key}={value}"
                                    for key, value in item["miss_reasons"].items())
                lines.append(f"{'':<18} misses: {reasons}")
    else:
        lines.append("(no shadow signals in window)")
    lines.append("")

    lines.append("BY DAY")
    lines.append(rule)
    if report["by_day"]:
        for day, item in report["by_day"].items():
            lines.append(f"{day}  signals={item['signals']:>4} "
                         f"matched={item['matched']:>4} net={item['net']:>10,.0f}")
    else:
        lines.append("(none)")
    lines.append("")

    lines.append("MISS REASONS")
    lines.append(rule)
    if shadow["miss_reasons"]:
        for reason, count in shadow["miss_reasons"].items():
            lines.append(f"{count:>5}  {reason}")
    else:
        lines.append("(none)")
    lines.append("")
    lines.append("A miss means the archive lacks the contract/quote, not that the")
    lines.append("shadow signal was invalid.  Widen capture with per-underlying")
    lines.append("--strike-range values to improve future replay coverage.")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG,
                        help="trade_log.jsonl path (default: output/trade_log.jsonl)")
    parser.add_argument("--archive", type=Path, default=None,
                        help="option archive path (default: local TRIO archive)")
    parser.add_argument("--strategies", nargs="+", default=["mean_reversion"],
                        help="shadow sleeves to replay (default: mean_reversion)")
    parser.add_argument("--start", default=None, help="first IST signal date")
    parser.add_argument("--end", default=None, help="last IST signal date")
    parser.add_argument("--max-strike-drift", type=float, default=0.02,
                        help="reject a contract substitution above this drift "
                             "(default: 0.02)")
    parser.add_argument("--quote-carry-ms", type=int, default=360_000,
                        help="bounded carry-forward for one missing quote")
    parser.add_argument("--quote-clock-skew-ms", type=int, default=5_000,
                        help="allowed provider/local clock skew")
    parser.add_argument("--include-test", action="store_true",
                        help="include mode=test ledger rows")
    parser.add_argument("--json", type=Path, default=None,
                        help="write the full report as JSON")
    args = parser.parse_args(argv)

    archive = args.archive or default_archive_path()
    records = load_records(args.log, include_test=args.include_test)
    trades, raw = build_shadow_trades(
        records, start=args.start, end=args.end, strategies=args.strategies)

    pairs: List[Dict[str, Any]] = []
    if trades and Path(archive).exists():
        connection = sqlite3.connect(str(archive))
        connection.row_factory = sqlite3.Row
        try:
            for trade in trades:
                if (not trade.get("symbol") or not trade.get("kind")
                        or trade.get("strike") is None):
                    pairs.append({
                        "trade": trade, "status": "miss",
                        "reason": "missing_contract_identity",
                    })
                    continue
                pair = replay_trade(
                    trade, connection,
                    archive_path=archive,
                    max_strike_drift=args.max_strike_drift,
                    quote_carry_forward_ms=args.quote_carry_ms,
                    quote_clock_skew_ms=args.quote_clock_skew_ms,
                )
                pairs.append(pair)
        finally:
            connection.close()

    report = build_report(
        pairs,
        trades=trades,
        raw=raw,
        start=args.start,
        end=args.end,
        archive_path=archive,
        log_path=args.log,
    )
    print(format_report(report))

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"\nSaved JSON: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
