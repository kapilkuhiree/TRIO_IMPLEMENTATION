"""Capture a read-only Zerodha JSON signal artifact for research/backtesting."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.zerodha_market_data import ZerodhaMarketData  # noqa: E402
from src.zerodha_signal_capture import (  # noqa: E402
    DEFAULT_STRATEGIES,
    DEFAULT_UNDERLYINGS,
    capture_zerodha_signals,
)


def _values(repeated: list[str] | None, grouped: list[str] | None, default: tuple[str, ...]) -> list[str]:
    values = list(repeated or ()) + list(grouped or ())
    return values or list(default)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only Zerodha current-day signal capture. Produces research "
            "signals; it never places orders."
        )
    )
    parser.add_argument("--underlying", action="append", help="underlying (repeatable)")
    parser.add_argument("--underlyings", nargs="+", help="one or more underlyings")
    parser.add_argument(
        "--strategy",
        action="append",
        help="strategy: orb, pullback, momentum, or trend_pullback_breakout_research",
    )
    parser.add_argument("--strategies", nargs="+", help="one or more strategy names")
    parser.add_argument("--period", default="30d", help="Zerodha historical lookback, default 30d")
    parser.add_argument("--as-of", help="observation timestamp; defaults to current IST time")
    parser.add_argument("--output", type=Path, help="JSON artifact path; omit to print only")
    parser.add_argument("--env-file", type=Path, help="optional local .env path")
    parser.add_argument("--max-provider-clock-skew-seconds", type=float, default=120.0,
                        help="research-only Zerodha timestamp tolerance (default 120)")
    parser.add_argument("--require-demand-supply", action="store_true",
                        help="require causal demand/supply confirmation for each signal")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="explicitly document the read-only mode; broker order calls are unavailable",
    )
    args = parser.parse_args(argv)

    # The command is inherently dry/read-only.  Keep the flag as an explicit
    # operator acknowledgement without adding an order-capable code path.
    del args.dry_run
    as_of = None
    if args.as_of:
        try:
            as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
        except ValueError as exc:
            parser.error(f"invalid --as-of timestamp: {exc}")

    try:
        if args.env_file:
            load_dotenv(args.env_file, override=True)
        else:
            from src.utils import load_env
            load_env()
        adapter = ZerodhaMarketData.from_env()
        report = capture_zerodha_signals(
            adapter,
            underlyings=_values(args.underlying, args.underlyings, DEFAULT_UNDERLYINGS),
            strategy_names=_values(args.strategy, args.strategies, DEFAULT_STRATEGIES),
            period=args.period,
            as_of=as_of,
            output_path=args.output,
            max_provider_clock_skew_seconds=args.max_provider_clock_skew_seconds,
            require_demand_supply=args.require_demand_supply,
        )
    except (RuntimeError, ValueError, OSError) as exc:
        # Keep CLI failures JSON-shaped and order-free even when credentials or
        # provider data are unavailable before a capture report can be built.
        report = {
            "schema": "zerodha_signal_capture.v1",
            "status": "NOT_READY",
            "ready_for_options_premium_backtest": False,
            "provider": "zerodha",
            "read_only": True,
            "orders_placed": 0,
            "signals": [],
            "coverage": {"mode": "current_trading_day_only"},
            "diagnostics": {
                "counts": {"capture_initialization_error": 1},
                "rejected_data": [{"reason": "capture_initialization_error", "detail": str(exc)}],
                "errors": [],
            },
        }
        if args.output:
            from src.zerodha_signal_capture import write_signal_file

            write_signal_file(report, args.output)

    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
