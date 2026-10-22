"""Run the read-only historical option-premium backtest from a JSON signal file."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.options_premium_backtest import (  # noqa: E402
    load_signal_json,
    report_json,
    run_options_premium_backtest,
)


def _underlyings(args: argparse.Namespace) -> list[str]:
    values: list[str] = []
    values.extend(args.underlying or [])
    values.extend(args.underlyings or [])
    output: list[str] = []
    for value in values:
        output.extend(part.strip().upper() for part in str(value).split(",") if part.strip())
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only SQLiteOptionArchive option-premium backtest"
    )
    parser.add_argument("--db", type=Path, help="existing SQLiteOptionArchive path")
    parser.add_argument("--underlying", action="append", help="underlying (repeatable)")
    parser.add_argument("--underlyings", nargs="+", help="one or more underlyings")
    parser.add_argument("--start", help="inclusive ISO/date start timestamp")
    parser.add_argument("--end", help="inclusive ISO/date end timestamp")
    parser.add_argument(
        "--signals", "--signal-file", "--signal-json", dest="signal_file", type=Path,
        help="JSON array/object containing completed underlying signals",
    )
    parser.add_argument("--interval", default="5minute")
    parser.add_argument("--stop-fraction", type=float, default=0.20)
    parser.add_argument(
        "--target-fraction", type=float, default=0.30,
        help="final target fraction for the staged ladder (default 0.30)",
    )
    parser.add_argument(
        "--profit-step-fraction", type=float, default=0.10,
        help="profit-lock step for staged exits (default 0.10)",
    )
    parser.add_argument(
        "--exit-policy", choices=("staged_bid_ladder", "single_target"),
        default="staged_bid_ladder",
        help="staged executable-bid ladder or legacy single-target policy",
    )
    parser.add_argument("--brokerage-per-order", type=float, default=0.0)
    parser.add_argument("--slippage-bps", type=float, default=0.0)
    parser.add_argument("--tax-rate", type=float, default=0.0)
    parser.add_argument("--capital", type=float)
    parser.add_argument("--risk-fraction", type=float, default=0.01)
    parser.add_argument("--max-lots", type=int, default=1)
    parser.add_argument("--quote-policy", choices=("exact", "interval_aligned"),
                        default="exact",
                        help="exact or interval_aligned provider quote timestamps")
    args = parser.parse_args(argv)

    underlyings = _underlyings(args)
    if args.db is None or args.signal_file is None or not underlyings:
        report = run_options_premium_backtest(
            args.db, None,
            underlyings=underlyings,
            start=args.start,
            end=args.end,
            interval=args.interval,
            stop_fraction=args.stop_fraction,
            target_fraction=args.target_fraction,
            profit_step_fraction=args.profit_step_fraction,
            exit_policy=args.exit_policy,
            brokerage_per_order=args.brokerage_per_order,
            slippage_bps=args.slippage_bps,
            tax_rate=args.tax_rate,
            capital=args.capital,
            risk_fraction=args.risk_fraction,
            max_lots=args.max_lots,
            quote_policy=args.quote_policy,
        )
        print(report_json(report))
        return 0

    try:
        signals = load_signal_json(args.signal_file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        report = run_options_premium_backtest(
            args.db, None, underlyings=underlyings, start=args.start, end=args.end,
            interval=args.interval, stop_fraction=args.stop_fraction,
            target_fraction=args.target_fraction,
            profit_step_fraction=args.profit_step_fraction,
            exit_policy=args.exit_policy,
            brokerage_per_order=args.brokerage_per_order,
            slippage_bps=args.slippage_bps, tax_rate=args.tax_rate,
            capital=args.capital, risk_fraction=args.risk_fraction, max_lots=args.max_lots,
            quote_policy=args.quote_policy,
        )
        report["diagnostics"]["counts"]["signal_file_error"] = 1
        report["diagnostics"]["rejected_data"].append({
            "reason": "signal_file_error", "detail": str(exc),
        })
        print(report_json(report))
        return 0

    report = run_options_premium_backtest(
        args.db, signals,
        underlyings=underlyings,
        start=args.start,
        end=args.end,
        interval=args.interval,
        stop_fraction=args.stop_fraction,
        target_fraction=args.target_fraction,
        profit_step_fraction=args.profit_step_fraction,
        exit_policy=args.exit_policy,
        brokerage_per_order=args.brokerage_per_order,
        slippage_bps=args.slippage_bps,
        tax_rate=args.tax_rate,
        capital=args.capital,
        risk_fraction=args.risk_fraction,
        max_lots=args.max_lots,
        quote_policy=args.quote_policy,
    )
    print(report_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
