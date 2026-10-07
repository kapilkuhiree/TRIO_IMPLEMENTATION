"""
TRIO — Trading Intelligence and Optimization
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

CLI Entry Point

Usage:
    python main.py analyze --image screenshot.png [--symbol RELIANCE.NS]
    python main.py scan --symbol RELIANCE.NS [--timeframe 15m]
    python main.py backtest --symbol RELIANCE.NS
    python main.py paper --symbols RELIANCE.NS TCS.NS [--interval 300]

DISCLAIMER: This software is for educational purposes only.
It does not constitute financial advice. Trading involves substantial
risk of loss. Use at your own risk.
"""

import argparse
import csv
import json
import logging
import sys
from pathlib import Path
from typing import List

# Setup path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.utils import load_env, load_config, setup_logging, get_logger
from src.alerts import format_signal_message, send_signal_alert

logger = get_logger("main")

DISCLAIMER = """
================================================================================
  DISCLAIMER: TRIO is for EDUCATIONAL PURPOSES ONLY.
  It does NOT constitute financial advice. Trading involves substantial risk
  of loss. The authors are not responsible for any financial losses. Always
  consult a qualified financial advisor before making investment decisions.
================================================================================
"""


def cmd_analyze(args: argparse.Namespace) -> None:
    """Analyze a screenshot and produce a trading signal."""
    from src.screenshot_analyzer import analyze_screenshot
    from src.data_fetcher import fetch_market_data
    from src.indicators import compute_indicators
    from src.cross_checker import cross_check
    from src.sentiment import analyze_sentiment
    from src.signal_engine import generate_signal
    from src.risk_manager import apply_risk_management

    # Step 1: Analyze screenshot
    logger.info("Analyzing screenshot: %s", args.image)
    ctx = analyze_screenshot(
        image_path=args.image,
        symbol_hint=args.symbol,
        timeframe_hint=args.timeframe,
    )
    print(f"\nScreenshot Analysis:")
    print(f"  Symbol:     {ctx.symbol}")
    print(f"  Timeframe:  {ctx.timeframe}")
    print(f"  Price:      {ctx.current_price}")
    print(f"  Trend:      {ctx.trend}")
    print(f"  Patterns:   {', '.join(ctx.patterns_detected) or 'none'}")
    print(f"  Confidence: {ctx.confidence:.0%}")
    print(f"  Notes:      {ctx.chart_notes}")

    if not ctx.symbol:
        print("\nCould not detect symbol from screenshot. Use --symbol to specify.")
        return

    # Step 2: Fetch live data
    symbol = ctx.symbol
    timeframe = ctx.timeframe or args.timeframe or "15m"

    print(f"\nFetching live data for {symbol} ({timeframe})...")
    market_data = fetch_market_data(symbol, timeframe)
    df = market_data.ohlcv
    print(f"  Fetched {len(df)} candles, latest price: {market_data.latest_price}")

    # Step 3: Compute indicators
    readings = compute_indicators(df, symbol, timeframe)
    print(f"\nTechnical Summary: {readings.summary}")

    # Step 4: Cross-check
    cc = cross_check(ctx, market_data.latest_price, readings)
    print(f"\nCross-Check: trust={cc.trust_score:.2f}, price_match={cc.price_match}")
    for w in cc.warnings:
        print(f"  WARNING: {w}")

    # Step 5: Sentiment
    cfg = load_config()
    sentiment = None
    if cfg.get("sentiment", {}).get("enabled", False):
        print(f"\nAnalyzing sentiment for {symbol}...")
        try:
            sentiment = analyze_sentiment(symbol)
            print(f"  Sentiment: {sentiment.label} ({sentiment.score:.3f})")
        except Exception as exc:
            print(f"  Sentiment analysis failed: {exc}")

    # Step 6: Generate signal
    signal = generate_signal(
        symbol=symbol,
        latest_price=market_data.latest_price,
        technical_readings=readings,
        sentiment_result=sentiment,
        screenshot_ctx=ctx,
        cross_check_result=cc,
    )

    # Step 7: Risk management
    atr_value = None
    for key, ind in readings.indicators.items():
        if key.startswith("atr_"):
            atr_value = ind.value
            break
    swing = readings.swing_low if signal.action == "BUY" else readings.swing_high
    signal = apply_risk_management(signal, atr_value, swing_level=swing)

    # Output
    print("\n" + format_signal_message(signal))

    # Export
    _export_signal(signal, args)

    # Alerts
    if cfg.get("alerts", {}).get("telegram", {}).get("enabled") or \
       cfg.get("alerts", {}).get("email", {}).get("enabled"):
        send_signal_alert(signal)


def cmd_scan(args: argparse.Namespace) -> None:
    """Scan a symbol (no screenshot) and produce a trading signal."""
    from src.data_fetcher import fetch_market_data
    from src.indicators import compute_indicators
    from src.sentiment import analyze_sentiment
    from src.signal_engine import generate_signal
    from src.risk_manager import apply_risk_management

    symbols = args.symbol if isinstance(args.symbol, list) else [args.symbol]
    timeframe = args.timeframe or "15m"
    cfg = load_config()

    for symbol in symbols:
        print(f"\n{'='*50}")
        print(f"Scanning {symbol} ({timeframe})")
        print(f"{'='*50}")

        try:
            market_data = fetch_market_data(symbol, timeframe)
            df = market_data.ohlcv
            print(f"  Data: {len(df)} candles, latest={market_data.latest_price}")

            readings = compute_indicators(df, symbol, timeframe)
            print(f"  Technicals: {readings.summary}")

            sentiment = None
            if cfg.get("sentiment", {}).get("enabled", False):
                try:
                    sentiment = analyze_sentiment(symbol)
                    print(f"  Sentiment: {sentiment.label} ({sentiment.score:.3f})")
                except Exception:
                    pass

            signal = generate_signal(
                symbol=symbol,
                latest_price=market_data.latest_price,
                technical_readings=readings,
                sentiment_result=sentiment,
            )

            atr_value = None
            for key, ind in readings.indicators.items():
                if key.startswith("atr_"):
                    atr_value = ind.value
                    break
            swing = readings.swing_low if signal.action == "BUY" else readings.swing_high
            signal = apply_risk_management(signal, atr_value, swing_level=swing)

            print("\n" + format_signal_message(signal))
            _export_signal(signal, args)

        except Exception as exc:
            print(f"  ERROR: {exc}")
            logger.error("Scan failed for %s: %s", symbol, exc)


def cmd_backtest(args: argparse.Namespace) -> None:
    """Run a backtest on historical data."""
    from src.backtester import run_backtest

    symbol = args.symbol
    timeframe = args.timeframe or "1d"
    period = args.period or "365d"

    print(f"\nRunning backtest: {symbol} ({timeframe}, {period})")
    print("This may take a while...\n")

    try:
        result = run_backtest(
            symbol=symbol,
            timeframe=timeframe,
            period=period,
        )
        print(result.summary_str())

        # Export trades if requested
        if args.output:
            _export_backtest(result, args.output)

    except Exception as exc:
        print(f"Backtest failed: {exc}")
        logger.error("Backtest error: %s", exc)


def cmd_paper(args: argparse.Namespace) -> None:
    """Run paper trading loop."""
    from src.paper_trader import PaperTrader
    from src.utils import load_config

    # No --symbols given: fall back to the config watchlist (now 20 names),
    # so `python main.py paper` scans the full list without retyping it.
    symbols = args.symbols
    if not symbols:
        symbols = load_config().get("symbols", [])
    interval = args.interval or 300

    print(f"\nStarting paper trading:")
    print(f"  Symbols ({len(symbols)}):  {', '.join(symbols)}")
    print(f"  Interval: {interval}s")
    print(f"  Press Ctrl+C to stop\n")

    trader = PaperTrader(symbols=symbols, timeframe=args.timeframe)
    trader.run(interval_seconds=interval, max_iterations=args.max_iterations)


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------

def _export_signal(signal: any, args: argparse.Namespace) -> None:
    """Export signal to CSV/JSON if requested."""
    if not hasattr(args, "output") or not args.output:
        return

    output_path = Path(args.output)
    data = signal.to_dict() if hasattr(signal, "to_dict") else signal

    if output_path.suffix == ".json":
        with open(output_path, "a", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
            f.write("\n")
        print(f"Signal exported to {output_path}")

    elif output_path.suffix == ".csv":
        file_exists = output_path.exists()
        with open(output_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=data.keys())
            if not file_exists:
                writer.writeheader()
            writer.writerow({k: str(v) for k, v in data.items()})
        print(f"Signal exported to {output_path}")


def _export_backtest(result: any, path: str) -> None:
    """Export backtest results."""
    output_path = Path(path)
    data = result.to_dict()

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"Backtest results exported to {output_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    """Main CLI entry point."""
    print(DISCLAIMER)

    # Load environment (.env -> TELEGRAM_* etc.). Without this, alerts and
    # any other env-keyed feature silently see nothing — that was the live
    # bug just fixed (test send returned False until load_env ran first).
    load_env()
    setup_logging()

    parser = argparse.ArgumentParser(
        prog="trio",
        description="TRIO — Trading Intelligence and Optimization",
        epilog="DISCLAIMER: Educational purposes only. Not financial advice.",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # --- analyze ---
    p_analyze = subparsers.add_parser("analyze", help="Analyze a chart screenshot")
    p_analyze.add_argument("--image", "-i", required=True, help="Path to screenshot image")
    p_analyze.add_argument("--symbol", "-s", help="Symbol hint (if not visible in chart)")
    p_analyze.add_argument("--timeframe", "-t", help="Timeframe hint")
    p_analyze.add_argument("--output", "-o", help="Export signal to file (.csv or .json)")

    # --- scan ---
    p_scan = subparsers.add_parser("scan", help="Scan a symbol (no screenshot)")
    p_scan.add_argument("--symbol", "-s", required=True, nargs="+", help="Symbol(s) to scan")
    p_scan.add_argument("--timeframe", "-t", default="15m", help="Timeframe (default: 15m)")
    p_scan.add_argument("--output", "-o", help="Export signal to file")

    # --- backtest ---
    p_bt = subparsers.add_parser("backtest", help="Run backtest on historical data")
    p_bt.add_argument("--symbol", "-s", required=True, help="Symbol to backtest")
    p_bt.add_argument("--timeframe", "-t", default="1d", help="Timeframe (default: 1d)")
    p_bt.add_argument("--period", "-p", default="365d", help="History period (default: 365d)")
    p_bt.add_argument("--output", "-o", help="Export results to file")

    # --- paper ---
    p_paper = subparsers.add_parser("paper", help="Run paper trading loop")
    p_paper.add_argument("--symbols", "-s", nargs="+", required=False, default=None,
                         help="Symbols to trade (default: config watchlist)")
    p_paper.add_argument("--timeframe", "-t", default="15m", help="Timeframe")
    p_paper.add_argument("--interval", type=int, default=300, help="Scan interval in seconds")
    p_paper.add_argument("--max-iterations", type=int, help="Stop after N scans")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return

    commands = {
        "analyze": cmd_analyze,
        "scan": cmd_scan,
        "backtest": cmd_backtest,
        "paper": cmd_paper,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
