"""Run the read-only Zerodha active-option capture loop.

Credentials are read from the local environment by ``ZerodhaMarketData``;
this command never prints them and never imports an order adapter.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Iterable, List, Optional

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.utils import load_env  # noqa: E402
from src.zerodha_live_capture import (  # noqa: E402
    CaptureSettings,
    CaptureSQLiteOptionArchive,
    ZerodhaLiveCapture,
    default_archive_path,
)
from src.zerodha_market_data import ZerodhaMarketData  # noqa: E402


def _underlyings(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    for value in values:
        for item in str(value).split(","):
            name = item.strip().upper()
            if name and name not in result:
                result.append(name)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only Zerodha NFO CE/PE history and quote-depth capture")
    parser.add_argument("--db", type=Path, default=None,
                        help="SQLite archive path (default: local TRIO archive path)")
    parser.add_argument("--env-file", type=Path, default=None,
                        help="optional local .env path (credentials are never printed)")
    parser.add_argument("--underlyings", nargs="+", default=["NIFTY", "BANKNIFTY"],
                        help="Underlying names; accepts repeated values or comma-separated values")
    parser.add_argument("--period", default="30d")
    parser.add_argument("--interval", default="5minute")
    parser.add_argument("--cycles", type=int, default=1,
                        help="Number of capture cycles (default: 1)")
    parser.add_argument("--interval-seconds", type=float, default=300.0,
                        help="Delay between cycles")
    parser.add_argument("--once", action="store_true",
                        help="Capture one cycle and stop")
    parser.add_argument("--max-contracts", type=int, default=None,
                        help="Optional active-contract sample limit")
    parser.add_argument("--strike-min", type=float, default=None)
    parser.add_argument("--strike-max", type=float, default=None)
    parser.add_argument("--quote-batch-size", type=int, default=200)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--backoff-seconds", type=float, default=1.0)
    parser.add_argument("--rate-limit-seconds", type=float, default=0.25)
    parser.add_argument("--no-candles", action="store_true")
    parser.add_argument("--no-quotes", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    names = _underlyings(args.underlyings)
    try:
        if args.env_file:
            load_dotenv(args.env_file, override=True)
        else:
            load_env()
        path = args.db or default_archive_path()
        settings = CaptureSettings(
            underlyings=tuple(names), interval=args.interval, period=args.period,
            max_contracts=args.max_contracts, strike_min=args.strike_min,
            strike_max=args.strike_max, quote_batch_size=args.quote_batch_size,
            collect_candles=not args.no_candles, collect_quotes=not args.no_quotes,
            max_retries=args.max_retries, backoff_seconds=args.backoff_seconds,
            rate_limit_seconds=args.rate_limit_seconds,
        )
        adapter = ZerodhaMarketData.from_env()
        with CaptureSQLiteOptionArchive(path) as archive:
            result = ZerodhaLiveCapture(adapter, archive, settings).run(
                cycles=args.cycles, interval_seconds=args.interval_seconds,
                once=args.once)
        result["archive_path"] = str(path)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("ok") else 1
    except KeyboardInterrupt:
        print(json.dumps({
            "ok": True, "status": "stopped", "stopped_reason": "keyboard_interrupt",
            "archive_path": str(args.db or default_archive_path()), "orders_placed": 0,
        }, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({
            "ok": False, "error_type": type(exc).__name__,
            "error": str(exc)[:240],
            "archive_path": str(args.db or default_archive_path()),
            "orders_placed": 0,
        }, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
