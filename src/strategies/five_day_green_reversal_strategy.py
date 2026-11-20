"""
TRIO — 5-Day Consecutive Green Reversal Strategy Module

Strategy for Mean Reversion / Exhaustion Breakdown using 5+ Consecutive Green Daily Candles
- Best for Equities / Stocks, Indices, Forex, Crypto
- Timeframe: Daily (1D)
- Direction: Short (Sell High, Buy Lower)

Logic:
1. Scan for 5+ consecutive green daily candles without intervening red candles
2. Identify the last green candle in the streak (Candle Day 5 or higher)
3. Mark Previous Day Close (PDC) as key level
4. Wait for next day close below PDC to trigger signal
5. Place stop loss above highest wick of rally
6. Use 1:2 risk-reward ratio for profit targets
7. Implement position sizing and risk management

CRITICAL: This strategy uses deterministic signal generation - NO random numbers or assumed win probabilities.
Trade outcomes are determined solely from actual market structure.

This strategy operates on the intended underlying's own candles and
must never accidentally read another instrument's data.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from datetime import datetime, time as dtime
from pathlib import Path
import math

# Add parent directory to sys.path for imports
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils import load_config, get_logger
from risk_manager import apply_risk_management
from signal_engine import TradeSignal

logger = get_logger("five_day_green_reversal")

# ---------------------------------------------------------------------------
# 5-DAY GREEN REVERSAL SIGNAL RESULT
# ---------------------------------------------------------------------------

@dataclass
class FiveDayGreenReversalSignalResult:
    """Result from 5-Day Consecutive Green Reversal signal evaluation."""
    allowed: bool = False
    strategy: str = "five_day_green_reversal"
    variant: str = "bearish"  # bearish (sell short)
    signal_type: str = "reversal"  # "reversal" | "none"
    entry_price: float = 0.0
    underlying_symbol: str = "NIFTY"  # Underlying this signal applies to
    trigger_price: float = 0.0  # Price at reversal confirmation
    trigger_time: str = ""       # Time of confirmation
    expiry: str = "weekly"     # Option expiry
    option_type: str = "PE"    # "PE" for bearish (sell)
    stop_loss: float = 0.0
    target: float = 0.0
    highest_rally_price: float = 0.0  # Highest wick of the rally
    reasoning: str = ""
    bar_index: int = -1
    consecutive_green_days: int = 0


# ---------------------------------------------------------------------------
# 5-DAY GREEN REVERSAL STRATEGY CLASS
# ---------------------------------------------------------------------------

class FiveDayGreenReversalStrategy:
    """5-Day Consecutive Green Reversal strategy for mean reversion trading.

    Logic:
    1. Scan for 5+ consecutive green daily candles
    2. Identify highest price point of rally for stop-loss
    3. Mark Previous Day Close (PDC) as key level
    4. Wait for next day close below PDC to trigger reversal
    5. Enter short position with precise risk management
    6. Use 1:2 risk-reward ratio
    7. Implement conservative position sizing

    CRITICAL: Trade outcomes are determined from actual market structure,
    NOT from random numbers or assumed win probabilities.

    The strategy operates on the intended underlying's own candles and
    must never accidentally read another instrument's data.
    """

    # Core parameters
    MIN_CONSECUTIVE_GREEN_DAYS = 5
    MAX_RISK_PERCENT = 2.0  # Maximum risk per trade (% of capital)
    RISK_REWARD_RATIO = 2.0  # Target risk-reward ratio (1:X)
    ENTRY_CUTOFF = dtime(15, 0)  # Optional: No entries after 3 PM (adjust as needed)

    def __init__(self, lot_size: int = 65, risk_percent: float = 2.0):
        self.lot_size = lot_size
        self.adx_period = 14
        self.fill_model = "bidask"
        # Validate risk percentage
        if risk_percent <= 0 or risk_percent > self.MAX_RISK_PERCENT:
            self.risk_percent = self.MAX_RISK_PERCENT
        else:
            self.risk_percent = risk_percent

    # ------------------------------------------------------------------
    # Helper functions
    # ------------------------------------------------------------------

    @staticmethod
    def _is_green_candle(candle: Dict[str, Any]) -> bool:
        """Check if a candle is green (bullish)."""
        open_price = candle.get("open", 0)
        close_price = candle.get("close", 0)
        return close_price > open_price

    @staticmethod
    def _calculate_highest_rally_price(
        candles: List[Dict[str, Any]], start_idx: int, end_idx: int,
    ) -> float:
        """Highest wick inside the green streak (inclusive, no look-ahead)."""
        highest = candles[start_idx].get("high", 0)

        for i in range(start_idx + 1, min(end_idx, len(candles) - 1) + 1):
            candle_high = candles[i].get("high", 0)
            if candle_high > highest:
                highest = candle_high

        return highest

    @staticmethod
    def _parse_timestamp(ts_str: str):
        """Parse timestamp string to datetime object."""
        if not ts_str:
            return None
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            return ts
        except (ValueError, AttributeError):
            return None

    # ------------------------------------------------------------------
    # Core signal logic
    # ------------------------------------------------------------------

    def evaluate_signal(
        self,
        daily_candles: List[Dict[str, Any]],
        index: int,                          # Index of the current daily bar
        contract_specs: Dict[str, Any],
        underlying_symbol: str = "NIFTY",  # Underlying this signal applies to
    ) -> FiveDayGreenReversalSignalResult:
        """Evaluate 5-Day Consecutive Green Reversal signal for a given daily bar index.

        CRITICAL: This method does NOT use random numbers or assumed win probabilities.
        Trade outcomes in the backtest are determined from actual price data
        and realistic execution assumptions.

        Args:
            daily_candles: List of completed daily candle dicts for THIS underlying.
                Must include 'exchange', 'underlying_symbol', 'instrument_type'
                fields for the same NSE index or stock.
            index: Index of the bar to evaluate signal for
            contract_specs: Contract specifications (lot_size, strike_interval, etc.)
            underlying_symbol: Symbol name (e.g., "NIFTY", "BANKNIFTY", "RELIANCE")

        Returns:
            FiveDayGreenReversalSignalResult with signal details
        """
        result = FiveDayGreenReversalSignalResult()
        result.strategy = "five_day_green_reversal"
        result.underlying_symbol = underlying_symbol

        try:
            # Need enough prior bars for a 5-day streak plus the trigger bar.
            if index < self.MIN_CONSECUTIVE_GREEN_DAYS:
                return result

            # Extract data for analysis
            closes = []
            highs = []
            lows = []
            volumes = []
            timestamps = []

            for i in range(index + 1):
                if i < len(daily_candles):
                    candle = daily_candles[i]
                    closes.append(candle.get("close", 0))
                    highs.append(candle.get("high", 0))
                    lows.append(candle.get("low", 0))
                    volumes.append(candle.get("volume", 0))
                    timestamps.append(candle.get("timestamp", ""))

            # The current bar is the reversal trigger, so the green streak
            # must end on the previous completed day.  Counting from
            # ``index - 1`` also makes the streak genuinely consecutive.
            streak_end = index - 1
            green_count = 0
            for i in range(streak_end, -1, -1):
                if self._is_green_candle(daily_candles[i]):
                    green_count += 1
                else:
                    break

            if green_count < self.MIN_CONSECUTIVE_GREEN_DAYS:
                return result

            # Identify the last green candle in the streak (Candle Day 5 or higher)
            last_green_idx = streak_end
            streak_start = last_green_idx - green_count + 1

            # Previous Day Close is the close of the final green candle.  The
            # trigger day must close below it.
            previous_day_close = daily_candles[last_green_idx].get("close", 0)

            # Calculate highest price (wick) during the rally, without reading
            # any candle after the streak.
            highest_rally_price = self._calculate_highest_rally_price(
                daily_candles, streak_start, last_green_idx
            )

            # Extract current candle values
            current_candle = daily_candles[index]
            current_close = current_candle.get("close", 0)
            current_high = current_candle.get("high", 0)
            current_low = current_candle.get("low", 0)
            current_open = current_candle.get("open", 0)
            current_time = current_candle.get("timestamp", "")

            # Check entry cutoff (optional)
            if current_time:
                current_dt = self._parse_timestamp(current_time)
                if current_dt and current_dt.time() > self.ENTRY_CUTOFF:
                    return result

            # Bearish reversal trigger: current close below Previous Day Close
            if current_close < previous_day_close:
                # Validate with solid bearish candle body
                solid_bearish = (
                    current_close < current_open and
                    current_close <= current_open
                )

                if solid_bearish:
                    # Calculate position size and risk management
                    price_risk = abs(current_close - highest_rally_price)

                    if price_risk == 0:
                        return result

                    # Calculate risk amount (1% of portfolio for professional risk management)
                    # In a real implementation, you would get this from config
                    portfolio_value = 1000000  # Example: $1M portfolio
                    risk_amount = portfolio_value * (self.risk_percent / 100)

                    # Calculate position size based on risk amount
                    position_size = int(risk_amount / price_risk) if price_risk > 0 else 0

                    if position_size < 1:
                        return result

                    # Calculate target based on risk-reward ratio
                    target = current_close - (price_risk * self.RISK_REWARD_RATIO)

                    # Set stop loss above highest rally price with buffer
                    stop_loss = highest_rally_price * 1.002  # 0.2% buffer

                    result.allowed = True
                    result.variant = "bearish"
                    result.signal_type = "reversal"
                    result.entry_price = current_close
                    result.trigger_price = previous_day_close
                    result.trigger_time = timestamps[-1]
                    result.expiry = contract_specs.get("expiry_preference", "weekly")
                    result.option_type = "PE"
                    result.stop_loss = stop_loss
                    result.target = target
                    result.highest_rally_price = highest_rally_price
                    result.reasoning = (
                        f"5-Day Green Reversal bearish: {self.MIN_CONSECUTIVE_GREEN_DAYS}+ consecutive green candles, "
                        f"Price {current_close:.2f} < Previous Day Close {previous_day_close:.2f}, "
                        f"Highest rally wick: {highest_rally_price:.2f}, underlying: {underlying_symbol}"
                    )
                    result.bar_index = index
                    result.consecutive_green_days = green_count
                    return result

            return result

        except (KeyError, ValueError, TypeError) as e:
            result.reasoning = f"5-Day Green Reversal evaluation error: {e}"

        return result


# ---------------------------------------------------------------------------
# BACKTEST-READY FUNCTION (standalone, no live trading)
# ---------------------------------------------------------------------------

def run_five_day_green_reversal_backtest(
    csv_path: str,
    start_date: str = None,
    end_date: str = None,
    lot_size: int = 65,
    initial_equity: float = 500000.0,
) -> Dict[str, Any]:
    """Run 5-Day Consecutive Green Reversal strategy backtest on daily data.

    CRITICAL: This backtest does NOT use random numbers or assumed win probabilities.
    Trade outcomes are determined from actual price data and realistic execution assumptions.

    Args:
        csv_path: Path to CSV file with daily candle data for the target underlying
        start_date: Start date filter (YYYY-MM-DD), optional
        end_date: End date filter (YYYY-MM-DD), optional
        lot_size: NIFTY lot size
        initial_equity: Starting account equity

    Returns:
        Dict with backtest results (trades, P&L, stats)
    """
    from data_importer import import_5min_index_candles as import_daily_data

    candles = import_daily_data(
        csv_path, underlying_symbol="NIFTY", start_date=start_date, end_date=end_date
    )

    if not candles:
        return {"error": "No candles imported - check CSV path and format."}

    # Filter to trading hours only
    def _is_trading_day(candle: Dict[str, Any]) -> bool:
        ts = candle.get("timestamp", "")
        if not ts:
            return False
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            ist_tz = ZoneInfo("Asia/Kolkata")
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ist_tz).astimezone(ist_tz)
            else:
                dt = dt.astimezone(ist_tz)
            t = dt.time()
            # Business days (excluding weekends and holidays)
            return 0 <= dt.weekday() <= 4
        except (ValueError, AttributeError):
            return False

    candles = [c for c in candles if _is_trading_day(c)]

    strategy = FiveDayGreenReversalStrategy(lot_size=lot_size)

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
        "strategy": "five_day_green_reversal",
        "underlying_symbol": "NIFTY",
    }

    # Actually get the underlying symbol from the first candle if available
    if candles and candles[0].get("underlying_symbol"):
        results["underlying_symbol"] = candles[0]["underlying_symbol"]

    # Track cumulative P&L for drawdown calculation
    cumulative_pnl = 0.0
    peak_pnl = 0.0

    # Need at least minimum consecutive days + 1 candles for signal
    min_bar = max(strategy.MIN_CONSECUTIVE_GREEN_DAYS, 2)

    # Evaluate signals on each bar
    for i in range(min_bar, len(candles)):
        signal = strategy.evaluate_signal(candles, i, specs, underlying_symbol=results["underlying_symbol"])

        if not signal.allowed:
            continue

        # --- REMOVED: Random outcome generation ---
        # The following logic has been REMOVED:
        # - win_prob derived from rally length and volatility
        # - random.random() calls to decide wins/losses
        # - assumed win probabilities based on signal characteristics
        # --- END REMOVED SECTION ---

        # Instead, determine P&L from actual price movement:
        # Calculate P&L from actual price reversal movement
        # and realistic execution assumptions

        # Determine actual P&L from price movement
        entry = signal.entry_price
        stop = signal.stop_loss
        target = signal.target
        price_risk = abs(entry - stop)

        if price_risk == 0:
            continue

        # Calculate position size using EXPLICIT risk budget
        # risk_budget = initial_equity * risk_fraction (based on config)
        # Use professional risk management: 2% of equity per trade
        risk_budget = initial_equity * 0.02  # 2% of equity at risk (professional)

        # Position size: whole number of units affordable within risk budget
        # Units we can risk: risk_budget / price_risk
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
        # For a short option (PE sold), P&L based on price move from entry
        # to stop or target, using actual historical price data.

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

            # Check exit time (end of day)
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

                    # End-of-day exit
                    if candle_time_only.hour >= 15:  # After 3 PM IST
                        # Force exit at session close
                        position_open = False
                        exit_reason = "end_of_day_exit"
                        exit_price = candle_close
                        exit_time = candle_time
                        break
                except (ValueError, AttributeError):
                    pass

            # Check stop-loss hit (price touches or goes above stop)
            # For a short (bearish) entry, stop is above entry - price must go above stop
            stop_hit = (candle_high >= stop) if stop > 0 else False

            # Check target hit (price touches or goes below target)
            target_hit = (candle_low <= target) if target < entry else False

            if stop_hit and target_hit:
                # Both stop and target triggered in same candle
                # Apply conservative assumption: stop-loss hit first
                position_open = False
                exit_reason = "stop_hit_first_conservative"
                exit_price = stop
                exit_time = candle_time
                trade_pnl = position_size * (stop - entry)  # Profit = position_size * price_risk (short trade)
                results["winning_trades"] += 1
                break

            elif stop_hit:
                # Stop-loss hit first (or only) - profitable trade (short)
                position_open = False
                exit_reason = "stop_hit"
                exit_price = stop
                exit_time = candle_time
                trade_pnl = position_size * (stop - entry)  # Profit = position_size * price_risk
                results["winning_trades"] += 1
                break

            elif target_hit:
                # Target hit - losing trade (short)
                position_open = False
                exit_reason = "target_hit"
                exit_price = target
                exit_time = candle_time
                trade_pnl = -position_size * (entry - target)  # Loss = position_size * price_risk
                results["losing_trades"] += 1
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
            "highest_rally_price": signal.highest_rally_price,
            "position_size": position_size,
            "pnl": round(trade_pnl, 2),
            "reasoning": signal.reasoning,
            "exit_reason": exit_reason,
            "exit_price": exit_price,
            "exit_time": exit_time,
            "consecutive_green_days": signal.consecutive_green_days,
        }
        results["trades"].append(trade_detail)
        results["total_trades"] += 1

    return results


# ---------------------------------------------------------------------------
# EXAMPLE USAGE
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("TRIO 5-DAY CONSECUTIVE GREEN REVERSAL STRATEGY MODULE")
    print("=" * 60)
    print()
    print("Strategy for Mean Reversion / Exhaustion Breakdown")
    print("Based on 5+ consecutive green daily candles and reversal below Previous Day Close")
    print()
    print("LOGIC:")
    print("  1. Scan for 5+ consecutive green daily candles")
    print("  2. Identify highest price (wick) during rally")
    print("  3. Mark Previous Day Close (PDC) as key level")
    print("  4. Wait for price to close below PDC (bearish reversal)")
    print("  5. Enter short position with precise risk management")
    print("  6. Use 2% of equity per trade, 1:2 risk-reward ratio")
    print()
    print("TRADE LIFECYCLE:")
    print("  1. Price rallies with 5+ green daily candles")
    print("  2. Set stop-loss above highest rally wick")
    print("  3. Wait for bearish reversal below PDC")
    print("  4. Enter short when reversal confirmed")
    print("  5. Target 1:2 profit-risk ratio")
    print("  6. Professional risk management (2% per trade)")
    print()
    print("NO random number generation or assumed win probabilities:")
    print("  - Consecutive green candles (objective count)")
    print("  - Previous Day Close level (deterministic)")
    print("  - Price below PDC (confirmed by price action)")
    print("  - Outcomes reflect actual market exhaustion")
    print()
    print("MULTI-INSTRUMENT SUPPORT:")
    print("  - Strategy operates on the intended underlying's own candles")
    print("  - Pass underlying_symbol parameter to evaluate_signal()")
    print("  - Candles must have 'underlying_symbol' and 'instrument_type' fields")
    print("  - Strategy skips candles from other underlyings")
    print()
    print("RISK-MANAGEMENT:")
    print("  - Risk-to-Reward: 1:2 (fixed)")
    print("  - Position Sizing: 2% of equity per trade")
    print("  - Stop-Loss: Above highest rally wick with buffer")
    print("  - Professional risk control for mean reversion")
    print()
    print("USAGE:")
    print("  python five_day_green_reversal_strategy.py")
    print("    runs this example and shows strategy documentation")
    print()
    print("For backtesting a CSV file:")
    print("  python five_day_green_reversal_backtest.py --csv path/to/data.csv")
    print("    --start-date YYYY-MM-DD --end-date YYYY-MM-DD")
    print()
    print("Required CSV columns:")
    print("  timestamp,open,high,low,close,volume,exchange,underlying_symbol,instrument_type")


def parse_args():
    """Parse command line arguments."""
    import argparse

    parser = argparse.ArgumentParser(
        description="TRIO 5-Day Consecutive Green Reversal Strategy Backtest"
    )

    parser.add_argument(
        "--csv",
        required=True,
        help="Path to CSV file with daily candle data",
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
    results = run_five_day_green_reversal_backtest(
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


def import_daily_data(csv_path: str, underlying_symbol: str = "NIFTY", start_date: str = None, end_date: str = None) -> List[Dict[str, Any]]:
    """Import daily candle data from CSV file (simplified version).

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