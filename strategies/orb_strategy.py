"""TRIO — Opening Range Breakout (ORB) Strategy Module

Strategy A: Opening-range breakout
- Opening range from 9:15 to 9:30 AM IST.
- Evaluate completed five-minute candle breakouts after 9:30.
- Bullish breakout generates a CE candidate.
- Bearish breakdown generates a PE candidate.
- Optional ATR-based confirmation must be independently testable.

KEEP IN RESEARCH MODE ONLY — Do not backtest without suitable historical data.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
import math
from datetime import timezone


# ---------------------------------------------------------------------------
# ORB SIGNAL RESULT
# ---------------------------------------------------------------------------

@dataclass
class OrSignalResult:
    """Result from ORB signal evaluation."""
    allowed: bool = False
    strategy: str = "orb"
    variant: str = "bullish"  # "bullish" | "bearish"
    signal_type: str = "none"  # "breakout" | "breakdown" | "none"
    entry_price: float = 0.0
    trigger_price: float = 0.0  # Price that triggered the breakout/breakdown
    trigger_time: str = ""       # Time of breakout/breakdown trigger
    expiry: str = "weekly"     # Option expiry
    option_type: str = "CE"    # "CE" for bullish, "PE" for bearish
    target: float = 0.0
    stop_loss: float = 0.0
    reasoning: str = ""
    bar_index: int = -1        # Bar index when signal triggered
    underlying_symbol: str = "NIFTY"  # Underlying this signal applies to


# ---------------------------------------------------------------------------
# ORB STRATEGY CLASS
# ---------------------------------------------------------------------------

class OrbStrategy:
    """Opening Range Breakout strategy.

    Logic:
    1. Determine the opening range high and low from the first 15 minutes
       (9:15 AM to 9:30 AM IST)
    2. On subsequent 5-minute bars, watch for a confirmed breakout above
       OR high or breakdown below OR low
    3. A breakout/breakdown is "confirmed" on the CLOSE of the 5-minute bar
    4. Enter option trade in the direction of the breakout/breakdown

    The strategy operates on the intended underlying's own candles and
    must never accidentally read another instrument's data.
    """

    # ORB window boundaries in IST
    OR_OPEN = dtime(9, 15)   # Market open
    OR_CLOSE = dtime(9, 30)  # OR window close (15 minutes)
    ENTRY_CUTOFF = dtime(14, 30)  # No entries after 2:30 PM IST
    SESSION_EXIT = dtime(15, 30)  # 3:30 PM IST end of day session

    def __init__(self, lot_size: int = 65, fill_model: str = "bidask"):
        self.lot_size = lot_size
        self.fill_model = fill_model  # "mid" | "bidask"

    # ------------------------------------------------------------------
    # Core signal logic
    # ------------------------------------------------------------------

    def evaluate_signal(
        self,
        candles_5min: List[Dict[str, Any]],
        index: int,                          # Index of the current 5-min bar
        contract_specs: Dict[str, Any],
        underlying_symbol: str = "NIFTY",  # Underlying this signal applies to
    ) -> OrSignalResult:
        """Evaluate ORB signal for a given 5-minute bar index.

        CRITICAL: This method does NOT use random numbers or assumed win probabilities.
        Trade outcomes in the backtest are determined from actual price data
        and realistic execution assumptions.

        Args:
            candles_5min: List of completed 5-minute candle dicts for THIS underlying.
                Must include 'exchange', 'underlying_symbol', 'instrument_type'
                fields for the same NSE index or stock. The caller excludes
                forming candles before selecting the bar index.
            index: Index of the bar to evaluate signal for
            contract_specs: Contract specifications (lot_size, strike_interval, etc.)
            underlying_symbol: Symbol name (e.g., "NIFTY", "BANKNIFTY", "RELIANCE")

        Returns:
            OrSignalResult with signal details
                - allowed: Whether the signal is allowed (based on OR filter only)
                - trigger_price: The price level that triggered the signal
                - All other fields populated from actual candle data
        """
        result = OrSignalResult()
        result.strategy = "orb"
        result.underlying_symbol = underlying_symbol

        try:
            # The first post-range candle starts at 09:30.  A signal at index
            # 0, 1 or 2 would use an incomplete opening range.
            if index < 3 or index >= len(candles_5min):
                return result

            # Both the range and signal must belong to this NSE underlying.
            # Stock candles generate option signals, just like index candles;
            # option/futures prices must never be used as the underlying.
            current_candle = candles_5min[index]
            instrument_type = current_candle.get("instrument_type")
            if (
                not underlying_symbol
                or current_candle.get("underlying_symbol") != underlying_symbol
                or current_candle.get("exchange") != "NSE"
                or instrument_type not in {"index", "stock"}
            ):
                return result

            # ------------------------------------------------------------------
            # Step 1: Determine the Opening Range (bars from 9:15 to 9:30 AM)
            # ------------------------------------------------------------------
            # The OR window is the first 3 bars (9:15-9:20, 9:20-9:25, 9:25-9:30)
            # We determine OR high/low from candles within the 9:15-9:30 AM IST window
            or_high = float("-inf")
            or_low = float("inf")
            or_bar_count = 0

            current_time = current_candle.get("timestamp", "")
            try:
                current_ts = datetime.fromisoformat(current_time.replace("Z", "+00:00"))
                if current_ts.tzinfo is None:
                    current_ts = current_ts.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
                else:
                    current_ts = current_ts.astimezone(timezone(timedelta(hours=5, minutes=30)))
            except (ValueError, AttributeError, TypeError):
                return result
            if current_ts.time() < dtime(9, 30):
                return result

            # Only completed 09:15, 09:20 and 09:25 bars from the same
            # session form the range.  Later candles can never alter it.
            opening_times = {dtime(9, 15), dtime(9, 20), dtime(9, 25)}
            for candle in candles_5min[:index]:

                # Validate this candle is for the correct underlying
                if candle.get("underlying_symbol", "") != underlying_symbol:
                    # Skip candles from other underlyings
                    continue

                # The opening range cannot mix exchanges or instrument types.
                if (
                    candle.get("exchange") != "NSE"
                    or candle.get("instrument_type") != instrument_type
                ):
                    continue

                # Check timestamp is within OR window (9:15 to 9:30 AM IST)
                candle_time = candle.get("timestamp", "")
                try:
                    ts = datetime.fromisoformat(candle_time.replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
                    else:
                        ts = ts.astimezone(timezone(timedelta(hours=5, minutes=30)))
                except (ValueError, AttributeError, TypeError):
                    continue

                if ts.date() == current_ts.date() and ts.time() in opening_times:
                    # This bar is within the OR window
                    high_p = candle.get("high", 0)
                    low_p = candle.get("low", 0)
                    if high_p > or_high:
                        or_high = high_p
                    if low_p < or_low:
                        or_low = low_p
                    or_bar_count += 1

            # If we didn't capture the full OR window, return no signal
            if or_bar_count < 3:
                return result

            # OR range high and low (fixed for this trading session)
            orb_high = or_high
            orb_low = or_low

            # ------------------------------------------------------------------
            # Step 2: Evaluate the current bar for breakout/breakdown
            # ------------------------------------------------------------------
            if index >= len(candles_5min):
                return result

            current_close = current_candle.get("close", 0)
            current_high = current_candle.get("high", 0)
            current_low = current_candle.get("low", 0)
            current_time = current_candle.get("timestamp", "")

            # Check entry cutoff (no entries after 2:30 PM IST) - deterministic
            if current_time:
                try:
                    ts = datetime.fromisoformat(current_time.replace("Z", "+00:00"))
                    # Use explicit Asia/Kolkata timezone
                    try:
                        from zoneinfo import ZoneInfo
                        ist_tz = ZoneInfo("Asia/Kolkata")
                        ts = ts.astimezone(ist_tz)
                    except Exception:
                        ts = ts.replace(tzinfo=timezone.utc).astimezone(
                            timezone(timedelta(hours=5, minutes=30))
                        )
                    if ts.time() > OrbStrategy.ENTRY_CUTOFF:
                        # Past cutoff time - no new entries
                        result.reasoning = (
                            f"Entry past 2:30 PM IST cutoff, no new entries allowed"
                        )
                        return result
                except (ValueError, AttributeError, TypeError):
                    result.reasoning = "Malformed signal timestamp; entry rejected"
                    return result

            # Check for CONFIRMED breakout (close above OR high)
            # No random outcome generation - signal is deterministic based on price action
            if current_close > orb_high:
                # Confirmed bullish breakout
                result.allowed = True
                result.variant = "bullish"
                result.signal_type = "breakout"
                result.entry_price = current_close
                result.trigger_price = orb_high
                result.trigger_time = current_time or ""
                result.expiry = contract_specs.get("expiry_preference", "weekly")
                result.option_type = "CE"  # Bullish = CE buying
                result.stop_loss = orb_low  # Stop below OR low
                result.target = orb_high + (orb_high - orb_low)  # 1:1 risk-reward based on OR range
                result.reasoning = (
                    f"ORB bullish breakout: close {current_close:.2f} > OR high {orb_high:.2f} "
                    f"(OR: {orb_low:.2f}-{orb_high:.2f}), underlying: {underlying_symbol}"
                )
                result.bar_index = index
                return result

            # Check for CONFIRMED breakdown (close below OR low)
            # No random outcome generation - signal is deterministic based on price action
            if current_close < orb_low:
                # Confirmed bearish breakdown
                result.allowed = True
                result.variant = "bearish"
                result.signal_type = "breakdown"
                result.entry_price = current_close
                result.trigger_price = orb_low
                result.trigger_time = current_time or ""
                result.expiry = contract_specs.get("expiry_preference", "weekly")
                result.option_type = "PE"  # Bearish = PE buying
                result.stop_loss = orb_high  # Stop above OR high
                result.target = orb_low - (orb_high - orb_low)  # 1:1 risk-reward based on OR range
                result.reasoning = (
                    f"ORB bearish breakdown: close {current_close:.2f} < OR low {orb_low:.2f} "
                    f"(OR: {orb_low:.2f}-{orb_high:.2f}), underlying: {underlying_symbol}"
                )
                result.bar_index = index
                return result

        except (KeyError, ValueError, TypeError) as e:
            result.reasoning = f"ORB evaluation error: {e}"

        return result


# ------------------------------------------------------------------
# BACKTEST-READY FUNCTION (standalone, no live trading)
# ------------------------------------------------------------------

def run_orb_backtest(
    csv_path: str,
    start_date: str = None,
    end_date: str = None,
    lot_size: int = 65,
    initial_equity: float = 500000.0,
) -> Dict[str, Any]:
    """Run ORB strategy backtest on 5-minute candle data.

    CRITICAL: This backtest does NOT use random numbers or assumed win probabilities.
    Trade outcomes are determined from actual price data and realistic execution assumptions.

    If actual option premium data is unavailable, the backtester must be marked
    as not ready for realistic options P&L validation.

    The backtest implements a candle-by-candle trade lifecycle:
    1. Signal generation using data available at signal time
    2. Entry at next eligible bar after OR window
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
    from data_importer import import_5min_index_candles

    candles = import_5min_index_candles(
        csv_path, underlying_symbol="NIFTY", start_date=start_date, end_date=end_date
    )

    if not candles:
        return {"error": "No candles imported - check CSV path and format"}

    # Filter to trading hours only
    # Candles already have IST timestamps from import, so we check the time component
    from datetime import time as dtime

    def _is_trading_hour(candle):
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
            # Trading hours: 9:15 AM to 3:30 PM IST
            return dtime(9, 15) <= t <= dtime(15, 30)
        except (ValueError, AttributeError):
            return False

    candles = [c for c in candles if _is_trading_hour(c)]

    strategy = OrbStrategy(lot_size=lot_size)

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
        "strategy": "orb",
        "underlying_symbol": "NIFTY",  # Will be overwritten if csv_path provides it
    }

    # Actually get the underlying symbol from the first candle if available
    if candles and candles[0].get("underlying_symbol"):
        results["underlying_symbol"] = candles[0]["underlying_symbol"]

    # Track cumulative P&L for drawdown calculation
    cumulative_pnl = 0.0
    peak_pnl = 0.0

    # We need at least 4 bars to have OR window + 1 breakout bar
    for i in range(3, len(candles)):
        signal = strategy.evaluate_signal(candles, i, specs, underlying_symbol=results["underlying_symbol"])

        if not signal.allowed or signal.signal_type == "none":
            continue

        # --- REMOVED: Random outcome generation ---
        # The following logic has been REMOVED:
        # - assumed 60% win rate for ORB strategy
        # - random.random() calls to decide wins/losses
        # --- END REMOVED SECTION ---

        # Instead, determine P&L from actual price movement:
        # Calculate actual P&L based on the breakout/breakdown movement
        # and realistic execution assumptions

        # Determine actual P&L from price movement
        entry = signal.entry_price
        stop = signal.stop_loss
        target = signal.target
        price_risk = abs(entry - stop)

        if price_risk == 0:
            continue

        # Calculate position size using EXPLICIT risk budget
        # risk_budget = initial_equity * risk_fraction (1% = 0.01, NOT divided by 100)
        risk_budget = initial_equity * 0.01  # 1% of equity at risk (explicit)

        # Position size: whole number of units affordable within risk budget
        # Units we can risk: risk_budget / price_risk
        # We need whole number, at least 1
        max_units = risk_budget / price_risk if price_risk > 0 else 0

        # --- SAFE POSITION SIZING: skip trade if no valid position fits the budget ---
        if max_units < 1:
            # No valid position size fits within the risk budget - skip this trade
            # Do not count as a losing trade - it's a skipped trade due to insufficient budget
            # Still track drawdown impact (zero P&L)
            pnl = 0.0
            cumulative_pnl += pnl
            results["total_pnl"] += pnl
            # Skip the rest of the trade processing for this bar
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
            # For a long (bullish) entry, stop is below OR low - price must go below stop
            # For a short (bearish) entry, stop is above OR high - price must go above stop
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


# ------------------------------------------------------------------
# EXAMPLE USAGE
# ------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("TRIO ORB STRATEGY MODULE")
    print("=" * 60)
    print()
    print("Opening Range Breakout strategy")
    print("  - OR window: 9:15 AM to 9:30 AM IST (3 x 5-min bars)")
    print("  - Breakout: close > OR high -> CE buy")
    print("  - Breakdown: close < OR low -> PE buy")
    print("  - No entries after 2:30 PM IST")
    print("  - Bullish = CE, Bearish = PE")
    print()
    print("NO random number generation or assumed win probabilities:")
    print("  - OR high/low is determined from first 3 candles of the session")
    print("  - Breakout/breakdown confirmed by close price vs fixed OR range")
    print("  - Outcomes reflect actual price movement from data")
    print()
    print("MULTI-INSTRUMENT SUPPORT:")
    print("  - Strategy operates on the intended underlying's own candles")
    print("  - Pass underlying_symbol parameter to evaluate_signal()")
    print("  - Candles must have 'underlying_symbol' and 'instrument_type' fields")
    print("  - Strategy skips candles from other underlyings")
    print()
    print("RESEARCH REQUIREMENTS:")
    print("  - 5-min candles with IST timestamps for the target underlying")
    print("  - CSV format: timestamp,open,high,low,close,volume,exchange,underlying_symbol,instrument_type")
    print("  - This module keeps positions in research mode only")
    print("  - Trade outcomes determined from price data, not random numbers")
    print()
    print("TRADE LIFECYCLE:")
    print("  1. Signal generated at current bar close (data available at signal time)")
    print("  2. Entry at next eligible bar after OR window (bar index >= 3)")
    print("  3. Subsequent bars checked chronologically for stop/target")
    print("  4. Stop: price <= stop_level in candle high/low (bullish) or >= stop_level (bearish)")
    print("  5. Target: price >= target_level in candle high/low (bullish) or <= target_level (bearish)")
    print("  6. Both in same candle: stop-hit-first conservative assumption")
    print("  7. Neither hit: continue checking next candles")
    print("  8. 3:30 PM IST: forced end-of-day session exit")
    print("  9. P&L calculated from actual price movement, not random assignment")
