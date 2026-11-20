"""Signal-driven replay on real archived option bid/ask (Phase 2).

The model backtest (``scripts/backtest_orb_retest_v2.py``) prices every option
with Black-Scholes at a fixed 5 DTE / 18% IV.  This script takes the **same
accepted signals** and replays each one on the exact archived contract:

* contract selection: nearest expiry after the trade date, then nearest strike,
  with the strike substitution reported (never hidden),
* entry: next option candle's ask,
* exits: staged bid ladder (-20% stop, +10%/+20% ratchet, +30% target,
  same-day hard flat),
* executable quotes only; a bounded carry-forward fills a single missing
  5-minute quote caused by capture cadence drift and is flagged in the ledger.

Output is a per-sleeve model-vs-real comparison plus a per-trade pair table and
a coverage/miss report.  The archive is thin (one fully executable day at the
time of writing), so this is a **fidelity check**, not a new optimisation.

    python scripts/premium_replay.py
    python scripts/premium_replay.py --start 2026-10-21 --end 2026-10-21
    python scripts/premium_replay.py --json output/premium_replay.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scripts.backtest_orb_retest_v2 as model  # noqa: E402
from src.options_archive_analytics import (  # noqa: E402
    day_bounds_ms,
    default_analytics_path,
    latest_candle_timestamp,
    select_contract,
    coverage_summary,
)
from src.options_premium_backtest import run_options_premium_backtest  # noqa: E402
from src.zerodha_option_archive import default_archive_path  # noqa: E402

SLEEVES: Dict[str, Dict[str, Any]] = {
    "orb": {"collector": "orb", "earliest_min": 45, "cooldown": 300},
    "orb_retest": {"collector": "retest", "earliest_min": 30, "cooldown": 0},
    "mean_reversion": {"collector": "mr", "earliest_min": 0, "cooldown": 300},
}

STOP_FRACTION = 0.20
TARGET_FRACTION = 0.30
PROFIT_STEP_FRACTION = 0.10
RISK_CAPITAL = 1_000_000.0
RISK_FRACTION = 0.007
BROKERAGE_PER_ORDER = 20.0
TAX_RATE = 0.001
SLIPPAGE_BPS = 0.0  # the model already charges one tick each side; keep parity

MISS_PRIORITY = (
    "no_contract",
    "strike_drift",
    "no_option_candles",
    "missing_contract_identity",
    "quote_candle_alignment_failed",
    "no_next_eligible_option_candle",
    "unfillable_first_entry_candle",
    "entry_quote_not_after_signal",
    "missing_intermediate_exit_bid",
    "missing_aligned_quote",
    "ambiguous_aligned_quote",
    "ambiguous_interval_quote",
    "risk_budget_below_one_lot",
    "no_same_day_exit_candle",
    "unclosed_trade",
    "no_trade",
)


# ---------------------------------------------------------------------------
# Model signal stream (same entries as the model backtest)
# ---------------------------------------------------------------------------

def _collect(symbol_name: str,
             meta: Dict[str, Any],
             candles: Sequence[Dict[str, Any]],
             collector: str) -> Dict[int, str]:
    if collector == "orb":
        return model.collect_orb_signals(candles, symbol_name, meta["lot"], meta["step"])
    if collector == "retest":
        return model.collect_retest_signals(candles, symbol_name, meta["lot"], meta["step"])
    return model.collect_mr_signals(candles)


def build_model_trades(
    *,
    underlyings: Sequence[str] = ("NIFTY", "BANKNIFTY"),
    strategies: Sequence[str] = tuple(SLEEVES),
    start: Optional[str] = None,
    end: Optional[str] = None,
    days: int = 60,
) -> List[Dict[str, Any]]:
    """Generate the accepted model trades with their signal timestamps."""
    wanted = {str(item).strip().upper() for item in underlyings}
    output: List[Dict[str, Any]] = []
    for key, meta in model.SYMBOLS.items():
        if meta["name"] not in wanted:
            continue
        frame = model.fetch_5m(key, days, True)
        if frame is None or len(frame) <= 200:
            continue
        candles = model.to_candles(frame, meta["name"])
        by_date: Dict[str, List[int]] = defaultdict(list)
        for index, candle in enumerate(candles):
            by_date[candle["timestamp"][:10]].append(index)
        for strategy in strategies:
            spec = SLEEVES[strategy]
            signals = _collect(meta["name"], meta, candles, spec["collector"])
            trades = model.run_variant(
                candles, by_date, signals, meta,
                strategy=strategy,
                earliest_min=spec["earliest_min"],
                cooldown=spec["cooldown"],
                cooldown_rule="loss",
                as_of=str(datetime.now(model.IST).date()),
            )
            for trade in trades:
                day = trade["day"]
                if start and day < start:
                    continue
                if end and day > end:
                    continue
                index = int(trade["entry_idx"])
                signal_index = max(0, index - 1)
                enriched = dict(trade)
                enriched["signal_ts"] = candles[signal_index]["timestamp"]
                enriched["model_spot"] = float(candles[index]["open"])
                output.append(enriched)
    output.sort(key=lambda item: (item["entry_ts"], item["strategy"], item["symbol"]))
    return output


# ---------------------------------------------------------------------------
# Real replay
# ---------------------------------------------------------------------------

def build_signal(trade: Dict[str, Any], contract: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "timestamp": trade["signal_ts"],
        "completed": True,
        "underlying": contract["underlying"],
        "trading_symbol": contract["trading_symbol"],
        "expiry": contract["expiry_date"],
        "strike": contract["strike"],
        "option_type": contract["option_type"],
        "lot_size": contract["lot_size"],
        "action": "BUY",
        "signal_id": f"{trade['strategy']}:{trade['symbol']}:{trade['entry_ts']}",
    }


def _miss_reason(result: Dict[str, Any]) -> str:
    counts = (result.get("diagnostics") or {}).get("counts") or {}
    for reason in MISS_PRIORITY:
        if int(counts.get(reason, 0) or 0) > 0:
            return reason
    nonzero = [key for key, value in counts.items() if int(value or 0) > 0]
    return nonzero[0] if nonzero else "no_trade"


def replay_trade(
    trade: Dict[str, Any],
    connection: sqlite3.Connection,
    *,
    archive_path: Any,
    max_strike_drift: float,
    quote_carry_forward_ms: int,
    quote_clock_skew_ms: int,
) -> Dict[str, Any]:
    """Replay one model trade on the real archive and return a result pair."""
    contract = select_contract(
        connection,
        underlying=trade["symbol"],
        option_type=trade["kind"],
        target_strike=float(trade["strike"]),
        trade_date=trade["day"],
    )
    if contract is None:
        return {"trade": trade, "status": "miss", "reason": "no_contract"}
    drift = contract.get("strike_drift_pct") or 0.0
    if max_strike_drift is not None and drift > max_strike_drift:
        return {
            "trade": trade, "status": "miss", "reason": "strike_drift",
            "contract": contract, "strike_drift_pct": drift,
        }
    end_ts = latest_candle_timestamp(connection, contract["contract_id"], trade["day"])
    if end_ts is None:
        return {"trade": trade, "status": "miss", "reason": "no_option_candles",
                "contract": contract}
    end = datetime.fromtimestamp(end_ts / 1000.0, model.IST).isoformat()
    signal = build_signal(trade, contract)
    result = run_options_premium_backtest(
        str(archive_path),
        [signal],
        underlyings=[contract["underlying"]],
        start=trade["signal_ts"],
        end=end,
        interval="5minute",
        stop_fraction=STOP_FRACTION,
        target_fraction=TARGET_FRACTION,
        profit_step_fraction=PROFIT_STEP_FRACTION,
        exit_policy="staged_bid_ladder",
        quote_policy="interval_aligned",
        quote_carry_forward_ms=quote_carry_forward_ms,
        quote_clock_skew_ms=quote_clock_skew_ms,
        costs={
            "brokerage_per_order": BROKERAGE_PER_ORDER,
            "slippage_bps": SLIPPAGE_BPS,
            "tax_rate": TAX_RATE,
        },
        capital=RISK_CAPITAL,
        risk_fraction=RISK_FRACTION,
        max_lots=max(1, int(trade.get("lots") or 1)),
    )
    trades = result.get("trade_ledger") or []
    coverage = {
        "carried_quotes": (result.get("coverage") or {}).get("carried_quotes", 0),
        "clock_skew_quotes": (result.get("coverage") or {}).get("clock_skew_quotes", 0),
        "quote_policy": (result.get("provenance") or {}).get("quote_policy"),
    }
    if not trades:
        return {
            "trade": trade, "status": "miss", "reason": _miss_reason(result),
            "contract": contract,
            "engine_counts": (result.get("diagnostics") or {}).get("counts") or {},
            "engine_coverage": coverage,
        }
    real = trades[0]
    return {
        "trade": trade,
        "status": "matched",
        "contract": contract,
        "real": real,
        "engine_counts": (result.get("diagnostics") or {}).get("counts") or {},
        "engine_coverage": coverage,
    }


# ---------------------------------------------------------------------------
# Statistics and report
# ---------------------------------------------------------------------------

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
    ordered = list(values)
    if stamps is not None:
        pairs = sorted(zip(list(stamps), values), key=lambda item: item[0])
        ordered = [value for _, value in pairs]
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


def iv_snapshot(analytics_path: Optional[Path],
                days: Sequence[str]) -> Dict[str, Any]:
    if not analytics_path or not Path(analytics_path).exists() or not days:
        return {}
    connection = sqlite3.connect(str(analytics_path))
    connection.row_factory = sqlite3.Row
    try:
        start_ms = min(day_bounds_ms(day)[0] for day in days)
        end_ms = max(day_bounds_ms(day)[1] for day in days)
        rows = connection.execute(
            """SELECT underlying, option_type, implied_vol
                 FROM quote_analytics
                WHERE executable=1 AND implied_vol IS NOT NULL
                  AND observed_at_ms>=? AND observed_at_ms<=?""",
            (start_ms, end_ms),
        ).fetchall()
        grouped: Dict[tuple, List[float]] = defaultdict(list)
        for row in rows:
            grouped[(str(row["underlying"]), str(row["option_type"]))].append(
                float(row["implied_vol"])
            )
        output = {}
        for (underlying, kind), values in sorted(grouped.items()):
            values.sort()
            output[f"{underlying} {kind}"] = {
                "n": len(values),
                "median": values[len(values) // 2],
                "p25": values[len(values) // 4],
                "p75": values[min(len(values) - 1, (len(values) * 3) // 4)],
            }
        return output
    finally:
        connection.close()


def build_report(pairs: Sequence[Dict[str, Any]],
                 *,
                 model_trades: Sequence[Dict[str, Any]],
                 underlyings: Sequence[str],
                 strategies: Sequence[str],
                 start: Optional[str],
                 end: Optional[str],
                 analytics_path: Optional[Path],
                 archive_path: Any) -> Dict[str, Any]:
    matched = [pair for pair in pairs if pair["status"] == "matched"]
    by_sleeve: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for pair in pairs:
        by_sleeve[pair["trade"]["strategy"]].append(pair)

    sleeves: Dict[str, Any] = {}
    for name in strategies:
        group = by_sleeve.get(name, [])
        hits = [pair for pair in group if pair["status"] == "matched"]
        model_all = [pair["trade"] for pair in group]
        model_hit = [pair["trade"] for pair in hits]
        real = [pair["real"] for pair in hits]
        misses = Counter(pair.get("reason", "no_trade")
                         for pair in group if pair["status"] == "miss")
        sleeves[name] = {
            "model": _stats([float(item["pnl"]) for item in model_all],
                            [item["entry_ts"] for item in model_all]),
            "model_gross": _stats([float(item["gross_pnl"]) for item in model_all],
                                  [item["entry_ts"] for item in model_all]),
            "model_matched": _stats([float(item["pnl"]) for item in model_hit],
                                    [item["entry_ts"] for item in model_hit]),
            "model_matched_gross": _stats(
                [float(item["gross_pnl"]) for item in model_hit],
                [item["entry_ts"] for item in model_hit],
            ),
            "real_gross": _stats([float(item["gross_pnl"]) for item in real],
                                 [item["entry_timestamp"] for item in real]),
            "real_net": _stats([float(item["net_pnl"]) for item in real],
                               [item["entry_timestamp"] for item in real]),
            "match_rate": (len(hits) / len(group) * 100.0) if group else 0.0,
            "miss_reasons": dict(misses.most_common()),
        }

    days = sorted({pair["trade"]["day"] for pair in pairs})
    quality = None
    if analytics_path and Path(analytics_path).exists():
        quality = coverage_summary(analytics_path)
    integrity = {
        "carried_quotes": sum(
            int((pair.get("engine_coverage") or {}).get("carried_quotes", 0) or 0)
            for pair in matched
        ),
        "clock_skew_quotes": sum(
            int((pair.get("engine_coverage") or {}).get("clock_skew_quotes", 0) or 0)
            for pair in matched
        ),
    }
    return {
        "window": {
            "start": start or (days[0] if days else ""),
            "end": end or (days[-1] if days else ""),
            "sessions": len(days),
        },
        "underlyings": list(underlyings),
        "archive": str(archive_path),
        "analytics": str(analytics_path) if analytics_path else "",
        "model_trades": len(model_trades),
        "matched_trades": len(matched),
        "match_rate": (len(matched) / len(model_trades) * 100.0) if model_trades else 0.0,
        "sleeves": sleeves,
        "pairs": pairs,
        "quote_integrity": integrity,
        "archive_quality": quality,
        "iv_snapshot": iv_snapshot(analytics_path, days),
    }


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _carried_label(real: Dict[str, Any]) -> str:
    """Describe a carried quote explicitly instead of a bare yes/no."""
    if real.get("entry_carried") and real.get("exit_carried"):
        return "entry+exit"
    if real.get("entry_carried"):
        return "entry"
    if real.get("exit_carried"):
        return "exit"
    return "-"


def format_report(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    rule = "-" * 145
    window = report["window"]
    lines.append("PREMIUM REPLAY: MODEL (BLACK-SCHOLES) vs REAL ARCHIVE")
    lines.append("=" * 145)
    lines.append(f"Archive  : {report['archive']}")
    if report.get("analytics"):
        lines.append(f"Analytics: {report['analytics']}")
    lines.append(f"Window   : {window['start'] or '-'} .. {window['end'] or '-'} "
                 f"({window['sessions']} session(s))")
    lines.append(f"Signals  : {report['model_trades']} model trades | "
                 f"{report['matched_trades']} matched to executable quotes "
                 f"({report['match_rate']:.1f}%)")
    lines.append("")
    lines.append("PER-SLEEVE COMPARISON")
    lines.append(rule)
    header = (f"{'Sleeve':<16}{'ModelN':>7}{'ModelGross':>12}{'ModelNet':>12}"
              f"{'ModelPF':>8}{'MatchN':>7}{'RealGross':>12}{'RealNet':>12}"
              f"{'RealPF':>8}{'Match%':>8}{'dGross':>12}{'dNet':>12}")
    lines.append(header)
    lines.append(rule)
    for name, item in report["sleeves"].items():
        model_stats = item["model"]
        real = item["real_net"]
        delta_gross = item["real_gross"]["net"] - item["model_matched_gross"]["net"]
        delta_net = real["net"] - item["model_matched"]["net"]
        lines.append(
            f"{name:<16}{model_stats['trades']:>7}"
            f"{item['model_gross']['net']:>12,.0f}"
            f"{model_stats['net']:>12,.0f}{_pf(model_stats['profit_factor']):>8}"
            f"{real['trades']:>7}{item['real_gross']['net']:>12,.0f}"
            f"{real['net']:>12,.0f}{_pf(real['profit_factor']):>8}"
            f"{item['match_rate']:>7.1f}%{delta_gross:>12,.0f}{delta_net:>12,.0f}"
        )
    lines.append(rule)
    lines.append("ModelN/ModelGross/ModelNet cover every model signal in the window;")
    lines.append("RealGross/RealNet/dGross/dNet cover only matched trades, so they are")
    lines.append("apples-to-apples with the model's accepted entries.  Gross isolates")
    lines.append("pricing fidelity; net adds each side's own cost assumptions.")
    lines.append("")

    lines.append("MODEL TRADES ON THE REPLAY WINDOW, BY SLEEVE")
    lines.append(rule)
    for name, item in report["sleeves"].items():
        matched_model = item["model_matched"]
        lines.append(
            f"  {name:<16} model matched n={matched_model['trades']:>3} "
            f"gross={item['model_matched_gross']['net']:>10,.0f} "
            f"net={matched_model['net']:>10,.0f} "
            f"PF={_pf(matched_model['profit_factor']):>5} "
            f"| real gross={item['real_gross']['net']:>10,.0f} "
            f"net={item['real_net']['net']:>10,.0f} "
            f"PF={_pf(item['real_net']['profit_factor']):>5}"
        )
        if item["miss_reasons"]:
            lines.append("    misses: " + ", ".join(
                f"{reason}={count}" for reason, count in item["miss_reasons"].items()
            ))
    lines.append("")

    lines.append("PER-TRADE PAIRS")
    lines.append(rule)
    lines.append(
        f"{'Sleeve':<13}{'Entry':<17}{'MdlStk':>8}{'RealStk':>8}{'Drift':>7}"
        f"{'MdlLot':>7}{'RealLot':>8}{'MdlGross':>11}{'RealGross':>11}"
        f"{'MdlNet':>11}{'RealNet':>11} {'Exit':<18} {'Carried':<13}"
    )
    lines.append(rule)
    for pair in report["pairs"]:
        trade = pair["trade"]
        contract = pair.get("contract") or {}
        entry = str(trade.get("entry_ts", ""))[:16]
        if pair["status"] != "matched":
            lines.append(
                f"{trade['strategy']:<13}{entry:<17}"
                f"{trade['strike']:>8,.0f}{contract.get('strike', 0) or 0:>8,.0f}"
                f"{(contract.get('strike_drift_pct') or 0) * 100:>6.2f}%"
                f"{trade.get('lots', 0) or 0:>7}{'-':>8}"
                f"{trade.get('gross_pnl', 0.0):>11,.0f}{'-':>11}"
                f"{trade.get('pnl', 0.0):>11,.0f}{'-':>11} "
                f"{pair.get('reason', 'miss')[:18]:<18} {'-':<13}"
            )
            continue
        real = pair["real"]
        lines.append(
            f"{trade['strategy']:<13}{entry:<17}"
            f"{trade['strike']:>8,.0f}{contract['strike']:>8,.0f}"
            f"{(contract.get('strike_drift_pct') or 0) * 100:>6.2f}%"
            f"{trade.get('lots', 0) or 0:>7}{real.get('lots', 0) or 0:>8}"
            f"{trade.get('gross_pnl', 0.0):>11,.0f}{real['gross_pnl']:>11,.0f}"
            f"{trade.get('pnl', 0.0):>11,.0f}{real['net_pnl']:>11,.0f} "
            f"{str(real.get('exit_reason', ''))[:18]:<18} "
            f"{_carried_label(real):<13}"
        )
    lines.append(rule)
    lines.append("MdlLot/RealLot differences are sizing, not pricing.  Carried flags")
    lines.append("mark a fill taken from a carried-forward quote (never hidden).")

    if report.get("iv_snapshot"):
        lines.append("")
        lines.append("REAL IV ON THE REPLAY WINDOW (executable quotes)")
        lines.append("-" * 104)
        for name, values in report["iv_snapshot"].items():
            lines.append(
                f"  {name:<18} n={values['n']:>5}  median={values['median']:.3f}  "
                f"p25={values['p25']:.3f}  p75={values['p75']:.3f}"
            )
        lines.append("  Model backtest assumption: 18% flat IV, 5 DTE.")

    quality = report.get("archive_quality")
    if quality:
        quotes = quality["quotes"]
        lines.append("")
        lines.append("ARCHIVE QUALITY (sidecar)")
        lines.append("-" * 104)
        lines.append(
            f"  quotes={quotes['rows']:,} executable={quotes['executable']:,} "
            f"iv={quotes['iv']:,}"
        )
        if quotes.get("flags"):
            lines.append("  flags: " + ", ".join(
                f"{name}={count:,}" for name, count in quotes["flags"].items()
            ))
    else:
        lines.append("")
        lines.append("Archive quality sidecar not found.  Run:")
        lines.append("  python scripts/premium_analytics.py build")

    integrity = report.get("quote_integrity") or {}
    lines.append("")
    lines.append("REPLAY INTEGRITY")
    lines.append("-" * 104)
    lines.append(
        f"  engine coverage: carried_quotes={int(integrity.get('carried_quotes', 0)):,}  "
        f"clock_skew_quotes={int(integrity.get('clock_skew_quotes', 0)):,}"
    )
    lines.append("  These are engine-level coverage counters across loaded contracts;")
    lines.append("  per-trade carried fills are flagged explicitly in the table above.")

    lines.append("")
    lines.append("LIMITATIONS")
    lines.append("-" * 104)
    lines.append(
        f"  {window['sessions']} session(s) in this replay had executable archived quotes."
    )
    lines.append("  This is a pricing-fidelity check, not a recalibration sample.")
    lines.append("  Stops/targets stay unchanged until >=60 executable sessions and")
    lines.append("  >=80% quote match are available; do not tune on this thin sample.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=None)
    parser.add_argument("--analytics", type=Path, default=None,
                        help="sidecar path (default: next to the archive)")
    parser.add_argument("--underlyings", nargs="+", default=["NIFTY", "BANKNIFTY"])
    parser.add_argument("--strategies", nargs="+", default=list(SLEEVES),
                        choices=list(SLEEVES))
    parser.add_argument("--start", default=None, help="first IST entry date")
    parser.add_argument("--end", default=None, help="last IST entry date")
    parser.add_argument("--days", type=int, default=60,
                        help="cached spot window passed to the model (default 60)")
    parser.add_argument("--max-strike-drift", type=float, default=0.02,
                        help="reject a contract substitution above this drift (default 0.02)")
    parser.add_argument("--quote-carry-ms", type=int, default=360_000,
                        help="max quote carry-forward gap in ms (default 360000 = 6 min)")
    parser.add_argument("--quote-clock-skew-ms", type=int, default=120_000,
                        help="tolerate observed_at before source_timestamp by this much "
                             "(default 120000 = 2 min)")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    archive = args.archive or default_archive_path()
    analytics = args.analytics or default_analytics_path(archive)
    underlyings = [item.strip().upper() for item in args.underlyings]

    model_trades = build_model_trades(
        underlyings=underlyings,
        strategies=args.strategies,
        start=args.start,
        end=args.end,
        days=args.days,
    )

    connection = sqlite3.connect(str(archive))
    connection.row_factory = sqlite3.Row
    try:
        pairs = [
            replay_trade(
                trade, connection,
                archive_path=archive,
                max_strike_drift=args.max_strike_drift,
                quote_carry_forward_ms=args.quote_carry_ms,
                quote_clock_skew_ms=args.quote_clock_skew_ms,
            )
            for trade in model_trades
        ]
    finally:
        connection.close()

    report = build_report(
        pairs,
        model_trades=model_trades,
        underlyings=underlyings,
        strategies=args.strategies,
        start=args.start,
        end=args.end,
        analytics_path=analytics,
        archive_path=archive,
    )
    print(format_report(report))

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8"
        )
        print(f"\nSaved JSON: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
