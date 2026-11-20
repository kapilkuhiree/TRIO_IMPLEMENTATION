"""Build and inspect the option-archive analytics sidecar (Phase 1).

The sidecar is additive and rebuildable.  The 3+ GB archive is opened
read-only; nothing in this script writes to it.  ``--backup`` makes a copy
first when you want belt-and-braces safety before a large rebuild.

Examples
--------
    python scripts/premium_analytics.py build
    python scripts/premium_analytics.py build --start 2026-09-22 --end 2026-10-23
    python scripts/premium_analytics.py build --backup
    python scripts/premium_analytics.py coverage
    python scripts/premium_analytics.py coverage --json output/analytics_coverage.json

The analytics sidecar stores, per quote and per option candle:
spot, DTE, moneyness, implied volatility, spread, and quality flags
(``non_executable``, ``zero_bid``, ``crossed``, ``wide_spread``,
``stale_spot``, ``no_spot``, ``iv_unavailable``, ``expired``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.options_archive_analytics import (  # noqa: E402
    FeatureConfig,
    build_analytics,
    coverage_summary,
    default_analytics_path,
)
from src.zerodha_option_archive import default_archive_path  # noqa: E402


def _spot_overrides(values):
    overrides = {}
    for item in values or ():
        if "=" not in item:
            raise SystemExit(f"--spot expects UNDERLYING=PATH, got {item!r}")
        name, path = item.split("=", 1)
        overrides[name.strip().upper()] = Path(path.strip())
    return overrides


def _print_build(summary):
    print("OPTION ARCHIVE ANALYTICS BUILD")
    print("=" * 88)
    print(f"Archive : {summary['archive']}")
    print(f"Sidecar : {summary['sidecar']}")
    if summary.get("backup"):
        print(f"Backup  : {summary['backup']}")
    print(f"Contracts: {summary['contracts']:,}")
    print(f"Spot bars: " + ", ".join(
        f"{name}={count:,}" for name, count in summary.get("spot_bars", {}).items()
    ))
    print(f"Quote rows: {summary['quotes']:,}  (IV available: {summary['iv_quotes']:,})")
    print(f"Candle rows: {summary['candles']:,}  (IV available: {summary['iv_candles']:,})")
    window = summary.get("window") or {}
    if window.get("start") or window.get("end"):
        print(f"Window  : {window.get('start') or '-'} .. {window.get('end') or '-'}")
    flags = summary.get("flags") or {}
    if flags:
        print("Flags   : " + ", ".join(f"{name}={count:,}" for name, count in flags.items()))


def _print_coverage(summary):
    print("OPTION ARCHIVE ANALYTICS COVERAGE")
    print("=" * 88)
    print(f"Sidecar : {summary['sidecar']}")
    meta = summary.get("meta") or {}
    if meta.get("built_at"):
        print(f"Built   : {meta['built_at']}")
    quotes = summary["quotes"]
    candles = summary["candles"]
    print("")
    print("QUOTES")
    print(f"  rows={quotes['rows']:,}  iv={quotes['iv']:,}  "
          f"executable={quotes['executable']:,}")
    print(f"  window: {quotes['first'] or '-'} .. {quotes['last'] or '-'}")
    if quotes["flags"]:
        print("  flags : " + ", ".join(f"{k}={v:,}" for k, v in quotes["flags"].items()))
    print("")
    print("OPTION CANDLES")
    print(f"  rows={candles['rows']:,}  iv={candles['iv']:,}")
    print(f"  window: {candles['first'] or '-'} .. {candles['last'] or '-'}")
    if candles["flags"]:
        print("  flags : " + ", ".join(f"{k}={v:,}" for k, v in candles["flags"].items()))
    print("")
    print("BY UNDERLYING (quotes)")
    for name, values in (summary.get("by_underlying") or {}).items():
        print(f"  {name:<10} rows={values['quotes']:>6,}  iv={values['iv']:>6,}  "
              f"executable={values['executable']:>6,}")
    iv = summary.get("iv") or {}
    if iv.get("quotes_n"):
        print("")
        print("IMPLIED VOL (quotes)")
        print(f"  median={iv['median']:.3f}  p25={iv['p25']:.3f}  p75={iv['p75']:.3f}  "
              f"n={iv['quotes_n']:,}")
    gap = summary.get("spot_gap_seconds") or {}
    if gap.get("n"):
        print("")
        print("SPOT AS-OF GAP (seconds)")
        print(f"  median={gap['median']:.0f}  p90={gap['p90']:.0f}  max={gap['max']:.0f}  "
              f"n={gap['n']:,}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="build/rebuild the analytics sidecar")
    build.add_argument("--archive", type=Path, default=None,
                       help="archive path (default: TRIO_OPTIONS_ARCHIVE_PATH or local state)")
    build.add_argument("--sidecar", type=Path, default=None,
                       help="analytics DB path (default: next to the archive)")
    build.add_argument("--underlyings", nargs="+", default=["NIFTY", "BANKNIFTY"])
    build.add_argument("--start", default=None, help="optional IST start date")
    build.add_argument("--end", default=None, help="optional IST end date")
    build.add_argument("--stale-seconds", type=float, default=600.0,
                       help="spot older than this is flagged stale (default 600)")
    build.add_argument("--wide-spread", type=float, default=0.10,
                       help="spread/mid above this is flagged wide (default 0.10)")
    build.add_argument("--spot", action="append", default=[],
                       metavar="UNDERLYING=PATH",
                       help="override a cached spot pickle (repeatable)")
    build.add_argument("--backup", action="store_true",
                       help="copy the archive before building (sidecar is already non-destructive)")
    build.add_argument("--json", type=Path, default=None,
                       help="write the build summary as JSON")

    coverage = sub.add_parser("coverage", help="print sidecar coverage and quality")
    coverage.add_argument("--sidecar", type=Path, default=None)
    coverage.add_argument("--json", type=Path, default=None)

    args = parser.parse_args(argv)

    if args.command == "build":
        archive = args.archive or default_archive_path()
        sidecar = args.sidecar or default_analytics_path(archive)
        summary = build_analytics(
            archive,
            sidecar,
            spot_pickles=_spot_overrides(args.spot),
            underlyings=args.underlyings,
            start=args.start,
            end=args.end,
            feature_config=FeatureConfig(
                stale_spot_seconds=args.stale_seconds,
                wide_spread_pct=args.wide_spread,
            ),
            backup=args.backup,
        )
        _print_build(summary)
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(json.dumps(summary, indent=2, default=str),
                                 encoding="utf-8")
            print(f"\nSaved JSON: {args.json}")
        return 0

    sidecar = args.sidecar or default_analytics_path()
    summary = coverage_summary(sidecar)
    _print_coverage(summary)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(summary, indent=2, default=str),
                             encoding="utf-8")
        print(f"\nSaved JSON: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
