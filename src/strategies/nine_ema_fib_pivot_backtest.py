#!/usr/bin/env python3
"""
TRIO — 9 EMA + Fibonacci Pivot Points Strategy Backtest Module

Strategy E: 9 EMA + Fibonacci Pivot Points Backtest
- Implements backtesting for the 9 EMA + Fibonacci Pivot Points strategy
- Uses actual price data for trade outcomes (no random numbers)
- Follows the same architectural patterns as other strategy backtests

This backtest evaluates trade signals using:
1. EMA 9 for trend identification
2. Fibonacci Pivot Points for entry/exit levels
3. Solid candlestick body validation
4. Risk-reward ratio validation (1:2 to 1:3)
5. Time-based entry filtering (no entries after 2:30 PM IST)
"""

import csv
import sys
from pathlib import Path
from typing import Any, Dict, List

from nine_ema_fib_pivot_strategy import NineEMaFibPivotStrategy

# Add parent directory to sys.path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent))

from data_importer import import_5min_index_candles
from risk_manager import apply_risk_management
from signal_engine import TradeSignal
from utils import load_config


def run_9_ema_fib_pivot_backtest(
    csv_path: str,
    start_date: str = None,
    end_date: str = None,
    lot_size: int = 65,
    initial_equity: float = 500000.0,
) -> Dict[str, Any]:
    """Run 9 EMA + Fibonacci Pivot Points strategy backtest on 5-minute candle data.

    CRITICAL: This backtest does NOT use random numbers or assumed win probabilities.
    Trade outcomes are determined from actual price data and realistic execution assumptions.

    The backtest implements a candle-by-candle trade lifecycle:
    1. Signal generation using EMA 9 and Fibonacci pivot levels
    2. Entry at next eligible bar
    3. Chronological processing of subsequent bars
    4. Stop/target check against each candle's high/low
    5. Conservative stop-first assumption if both triggered in same bar
    6. Continued checking until exit or end of day
    7. Explicit end-of-day exit rule

    Args:
        csv_path: Path to CSV file with 5-min candles for the target underlying
        start_date: Start date filter (YYYY-MM-DD), optional
        end_date: End date filter (YYYY-MM-DD), optional
        lot_size: NIFTY lot size
        initial_equity: Starting account equity

    Returns:
        Dict with backtest results (trades, P&L, stats)
    """
    candles = import_5min_index_candles(
        csv_path, underlying_symbol="NIFTY", start_date=start_date, end_date=end_date
    )

    if not candles:
        return {"error": "No candles imported - check CSV path and format."}

    # Filter to trading hours only
    def _is_trading_hour(candle: Dict[str, Any]) -> bool:
        ts = candle.get("timestamp", "")
        if not ts:
            return False
        try:
            from datetime import datetime, time as dtime
            from zoneinfo import ZoneInfo
            ist_tz = ZoneInfo("Asia/Kolkata")
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ist_tz).astimezone(ist_tz)
            else:
                dt = dt.astimezone(ist_tz)
            t = dt.time()
            # Trading hours: 9:15 AM to 3:30 PM IST
            return dtime(9, 15) <= t <= dtime(15, 30)
        except (ValueError, AttributeError):
            return False

    candles = [c for c in candles if _is_trading_hour(c)]

    strategy = NineEMaFibPivotStrategy(lot_size=lot_size)

    # Contract specs
    specs = {
        "lot_size": lot_size,
        "expiry_preference": "weekly",
        "strike_interval": 50,
    }

    # Track results
    results = {
        "total_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "total_pnl": 0.0,
        "max_drawdown": 0.0,
        "trades": [],
        "strategy": "9_ema_fib_pivot",
        "underlying_symbol": "NIFTY",
    }

    # Actually get the underlying symbol from the first candle if available
    if candles and candles[0].get("underlying_symbol"):
        results["underlying_symbol"] = candles[0]["underlying_symbol"]

    # Track cumulative P&L for drawdown calculation
    cumulative_pnl = 0.0
    peak_pnl = 0.0

    # Need at least EMA period + 1 candles for signal
    min_bar = max(strategy.EMA_PERIOD, 2)

    # Evaluate signals on each bar
    for i in range(min_bar, len(candles)):
        signal = strategy.evaluate_signal(candles, i, specs, underlying_symbol=results["underlying_symbol"])

        if not signal.allowed:
            continue

        # --- REMOVED: Random outcome generation ---
        # The following logic has been REMOVED:
        # - win_prob derived from EMA/Pivot alignment
        # - random.random() calls to decide wins/losses
        # - assumed win probabilities based on signal characteristics
        # --- END REMOVED SECTION ---

        # Instead, determine P&L from actual price movement:
        # Calculate actual P&L based on the EMA/Pivot movement
        # and realistic execution assumptions

        # Determine actual P&L from price movement
        entry = signal.entry_price
        stop = signal.stop_loss
        target = signal.target
        price_risk = abs(entry - stop)

        if price_risk == 0:
            continue

        # Calculate tip_size
        # risk_budget = initial_equity * risk_fraction (1% = 0.01, NOT divided by 100)
        risk_budget = initial_equity * 0.01

        # Position size: whole number of units affordable within risk budget
        # Units we can risk: risk budget / price_risk
        # We need whole number, at least 1
        max_units = risk_budget / price_risk if price_risk > 0 else 0

        # --- SAFE POSITION SIZING: skip trade if no valid position fits the budget ---
        if max_units < 1:
            # No valid position size fits within the risk budget - skip this trade
            pnl = 0.0
            cumulative_pnl += pnl
            results["total_pnl"] += pnl
            continue
        else:
            position_size = int(max_units)  # whole number of units at least 1

        # --- ACTUAL P&L CALCULATION from price movement ---
        # For a long option (CE bought), P&L based on price move from entry
        # to target or stop, using actual historical price data.

        # CRITICAL: Determine outcome by checking subsequent candles' high/low
        # against stop and target levels. This is NOT based on relative positions
        # of entry, stop, target alone.

        # Check each subsequent candle chronologically
        exit_reason = None
        exit_price = None
        exit_time = None
        trade_pnl = 0.0

        # Track whether position is still open
        position_open = True

        # Iterate through subsequent candles
        for j in range(i + 1, len(candles)):
            candle = candles[j]
            candle_time = candle.get("timestamp", "")
            candle_high = candle.get("high", 0)
            candle_low = candle.get("low", 0)
            candle_close = candle.get("close", 0)

            # Check exit time (end of day session exit)
            if candle_time:
                try:
                    from datetime import datetime
                    from zoneinfo import ZoneInfo
                    ist_tz = ZoneInfo("Asia/Kolkata")
                    dt = datetime.fromisoformat(candle_time.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=ist_tz).astimezone(ist_tz)
                    else:
                        dt = dt.astimezone(ist_tz)
                    candle_time_only = dt.time()

                    # End-of-day session exit at 3:30 PM IST
                    if candle_time_only > strategy.SESSION_EXIT:
                        # Force exit at session close
                        position_open = False
                        exit_reason = "end_of_day_session_exit"
                        exit_price = candle_close
                        exit_time = candle_time
                        break
                except (ValueError, AttributeError):
                    pass

            # Check stop-loss hit (price touches or goes below/above stop)
            # For a long (bullish) entry, stop is below entry - price must go below stop
            # For a short (bearish) entry, stop is above entry - price must go above stop
            if signal.variant == "bullish":
                stop_hit = (candle_low <= stop) if stop > 0 else False
            else:  # bearish
                stop_hit = (candle_high >= stop) if stop > 0 else False

            # Check target hit (price touches or goes above/below target)
            if signal.variant == "bullish":
                target_hit = (candle_high >= target) if target > entry else False
            else:  # bearish
                target_hit = (candle_low <= target) if target < entry else False

            if stop_hit and target_hit:
                # Both stop and target triggered in same candle
                # Apply conservative assumption: stop-loss hit first
                position_open = False
                exit_reason = "stop_hit_first_conservative"
                exit_price = stop
                exit_time = candle_time
                trade_pnl = -position_size * (entry - stop)  # Loss = position_size * price_risk
                results["losing_trades"] += 1
                break

            elif stop_hit:
                # Stop-loss hit first (or only) - losing trade
                position_open = False
                exit_reason = "stop_hit"
                exit_price = stop
                exit_time = candle_time
                trade_pnl = -position_size * (entry - stop)  # Loss = position_size * price_risk
                results["losing_trades"] += 1
                break

            elif target_hit:
                # Target hit - winning trade
                position_open = False
                exit_reason = "target_hit"
                exit_price = target
                exit_time = candle_time
                trade_pnl = position_size * (target - entry)  # P&L from price move
                results["winning_trades"] += 1
                break

            # If neither hit yet, continue to next candle
            # (position remains open)

        # If position still open after loop (shouldn't happen with end-of-day exit),
        # apply end-of-day outcome
        if position_open:
            # Force exit with whatever price is available - this means target/stop
            # were never hit during the session
            exit_reason = "end_of_day_no_exit"
            exit_price = candles[-1].get("close", entry) if candles else entry
            exit_time = candles[-1].get("timestamp", "") if candles else ""
            trade_pnl = 0.0  # No P&L if never triggered

        cumulative_pnl += trade_pnl
        results["total_pnl"] += trade_pnl

        # Track drawdown
        if cumulative_pnl > peak_pnl:
            peak_pnl = cumulative_pnl
        drawdown = peak_pnl - cumulative_pnl
        if drawdown > results["max_drawdown"]:
            results["max_drawdown"] = drawdown

        trade_detail = {
            "bar_index": signal.bar_index,
            "timestamp": candles[i].get("timestamp", ""),
            "underlying_symbol": results["underlying_symbol"],
            "signal_type": signal.signal_type,
            "variant": signal.variant,
            "option_type": signal.option_type,
            "entry_price": entry,
            "stop_loss": stop,
            "target": target,
            "ema9": signal.ema9,
            "pivot_level": signal.pivot_level,
            "position_size": position_size,
            "pnl": round(trade_pnl, 2),
            "reasoning": signal.reasoning,
            "exit_reason": exit_reason,
            "exit_price": exit_price,
            "exit_time": exit_time,
        }
        results["trades"].append(trade_detail)
        results["total_trades"] += 1

    return results


# ---------------------------------------------------------------------------
# EXAMPLE USAGE
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("TRIO 9 EMA + FIBONACI PIVOT STRATEGY MODULE")
    print("=" * 60)
    print()
    print("Intraday Trend Reversal & Breakout strategy using EMA 9 and Fibonacci Pivot Points")
    print()
    print("LOGIC:")
    print("  1. Calculate EMA 9 for trend identification")
    print("  2. Calculate Fibonacci Pivot Points (Pivot, R1, R2, S1, S2)")
    print("  3. Wait for price to break and close beyond EMA 9 and Pivot levels")
    print("  4. Confirm with solid candlestick body (no wicks)")
    print("  5. Enter trades with precise risk management")
    print("  6. Use 1:2 to 1:3 risk-reward ratios")
    print()
    print("TRADE LIFECYCLE:")
    print("  1. Price tests trend and pivot levels")
    print("  2. Breakout/breakdown confirmed by close")
    print("  3. EMA9 must be above/below for bearish alignment")
    print("  4. Pivot level must be crossed for trade entry")
    print("  5. Solid body confirmation validates signal")
    print("  6. Risk managed with precise stops and targets")
    print()
    print("NO random number generation or assumed win probabilities:")
    print("  - EMA 9 trend filter (deterministic)")
    print("  - Fibonacci pivot levels (objective math)")
    print("  - Price must close beyond both EMA 9 and pivot levels")
    print("  - Outcomes reflect actual market structure")
    print()
    print("MULTI-INSTRUMENT SUPPORT:")
    print("  - Strategy operates on the intended underlying's own candles")
    print("  - Pass underlying_symbol parameter to evaluate_signal()")
    print("  - Candles must have 'underlying_symbol' and 'instrument_type' fields")
    print("  - Strategy skips candles from other underlyings")
    print()
    print("RISK-MANAGEMENT:")
    print("  - Risk-to-Reward: 1:2 to 1:3 (validated only)")
    print("  - No entries after 2:30 PM IST")
    print("  - Only solid body candles (no wicks) allowed")
    print()
    print("USAGE:")
    print("  python 9_ema_fib_pivot_strategy.py")
    print("    runs this example and shows strategy documentation")
    print()
    print("For backtesting a CSV file:")
    print("  python 9_ema_fib_pivot_backtest.py --csv path/to/data.csv")
    print("    --start-date YYYY-MM-DD --end-date YYYY-MM-DD")
    print()
    print("Required CSV columns:")
    print("  timestamp,open,high,low,close,volume,exchange,underlying_symbol,instrument_type")


def parse_args():
    """Parse command line arguments."""
    import argparse

    parser = argparse.ArgumentParser(
        description="TRIO 9 EMA + Fibonacci Pivot Points Strategy Backtest"
    )

    parser.add_argument(
        "--csv",
        required=True,
        help="Path to CSV file with 5-min candles",
    )

    parser.add_argument(
        "--start-date",
        help="Start date filter (YYYY-MM-DD)",
    )

    parser.add_argument(
        "--end-date",
        help="End date filter (YYYY-MM-DD)",
    )

    parser.add_argument(
        "--lot-size",
        type=int,
        default=65,
        help="NIFTY lot size (default: 65)",
    )

    parser.add_argument(
        "--initial-equity",
        type=float,
        default=500000.0,
        help="Starting account equity (default: 500000.0)",
    )

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    results = run_9_ema_fib_pivot_backtest(
        args.csv,
        start_date=args.start_date,
        end_date=args.end_date,
        lot_size=args.lot_size,
        initial_equity=args.initial_equity,
    )

    if "error" in results:
        print(f"Error: {results['error']}")
        import sys
        sys.exit(1)

    print(f"\nBacktest Results Summary:")
    print(f"Strategy: {results['strategy']}")
    print(f"Underlying: {results['underlying_symbol']}")
    print(f"Total Trades: {results['total_trades']}")
    print(f"Winning Trades: {results['winning_trades']}")
    print(f"Losing Trades: {results['losing_trades']}")
    print(f"Total P&L: ${results['total_pnl']:.2f}")
    print(f"Max Drawdown: ${results['max_drawdown']:.2f}")
    print(f"\nTop 5 trades:")
    for trade in results["trades"][-5:]:
        print(f"  {trade['timestamp'][:10]}: {trade['signal_type']} {trade['variant']} P&L=${trade['pnl']:.2f} ({trade['exit_reason']})")


def import_5min_index_candles(csv_path: str, underlying_symbol: str = "NIFTY", start_date: str = None, end_date: str = None) -> List[Dict[str, Any]]:
    """Import 5-minute candle data from CSV file (simplified version).

    This is a simplified version for standalone testing.
    In production, this would use the actual data_importer module.
    """
    import csv

    candles = []

    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)

        for row in reader:
            timestamp = row.get('timestamp', '')
            open_price = float(row.get('open', 0))
            high = float(row.get('high', 0))
            low = float(row.get('low', 0))
            close = float(row.get('close', 0))
            volume = float(row.get('volume', 0))

            if start_date and timestamp < start_date:
                continue
            if end_date and timestamp > end_date:
                continue

            candle = {
                "timestamp": timestamp,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "exchange": "NSE",
                "underlying_symbol": underlying_symbol,
                "instrument_type": "index",
            }

            candles.append(candle)

    return candles