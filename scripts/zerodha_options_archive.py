"""Capture active Zerodha NIFTY/BANKNIFTY option data, read-only.

Examples:
    python scripts/zerodha_options_archive.py --max-contracts 8
    python scripts/zerodha_options_archive.py --period 7d --no-quotes
    python scripts/zerodha_options_archive.py --coverage-only

The command never imports an order adapter and always reports
``orders_placed: 0``.  A bounded ``--max-contracts`` run is a probe/sample;
the JSON report says when coverage is partial.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.utils import load_env  # noqa: E402
from src.zerodha_market_data import ZerodhaMarketData  # noqa: E402
from src.zerodha_option_archive import (  # noqa: E402
    SQLiteOptionArchive,
    archive_active_options,
    default_archive_path,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="TRIO read-only Zerodha option archive")
    parser.add_argument("--db", type=Path, default=None,
                        help="SQLite archive path (default: TRIO_OPTIONS_ARCHIVE_PATH or local state)")
    parser.add_argument("--underlyings", nargs="+", default=["NIFTY", "BANKNIFTY"],
                        choices=["NIFTY", "BANKNIFTY"])
    parser.add_argument("--interval", default="5minute")
    parser.add_argument("--period", default="30d")
    parser.add_argument("--max-contracts", type=int, default=None,
                        help="Optional explicit sampling limit; report marks partial coverage")
    parser.add_argument("--strike-min", type=float, default=None)
    parser.add_argument("--strike-max", type=float, default=None)
    parser.add_argument("--quote-batch-size", type=int, default=200)
    parser.add_argument("--no-candles", action="store_true")
    parser.add_argument("--no-quotes", action="store_true")
    parser.add_argument("--coverage-only", action="store_true",
                        help="Report existing archive coverage without contacting Zerodha")
    args = parser.parse_args()
    load_env()
    path = args.db or default_archive_path()
    try:
        with SQLiteOptionArchive(path) as archive:
            if args.coverage_only:
                report = archive.coverage(underlyings=args.underlyings,
                                          interval=args.interval)
            else:
                adapter = ZerodhaMarketData.from_env()
                report = archive_active_options(
                    adapter, archive, underlyings=args.underlyings,
                    interval=args.interval, period=args.period,
                    collect_candles=not args.no_candles,
                    collect_quotes=not args.no_quotes,
                    quote_batch_size=args.quote_batch_size,
                    max_contracts=args.max_contracts,
                    strike_min=args.strike_min,
                    strike_max=args.strike_max,
                )
        report["archive_path"] = str(path)
        report["orders_placed"] = 0
        print(json.dumps(report, indent=2, default=str))
        return 0
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "archive_path": str(path),
            "orders_placed": 0,
        }, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
