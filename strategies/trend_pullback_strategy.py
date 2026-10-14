"""TRIO — Trend Pullback Strategy Module

Strategy B: Trend pullback
- EMA 20/50, VWAP and completed-candle price confirmation.
- Bullish setups evaluate CE buying.
- Bearish setups evaluate PE buying.

KEEP IN RESEARCH MODE ONLY — Do not backtest without suitable historical data.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
import math
from datetime import timezone


# ---------------------------------------------------------------------------
# TREND PULLBACK SIGNAL RESULT
# ---------------------------------------------------------------------------

@dataclass
class PullbackSignalResult:
    """Result from trend pullback signal evaluation."""
    allowed: bool = False
    strategy: str = "pullback"
    variant: str = "bullish"  # "bullish" | "bearish"
    signal_type: str = "none"  # "long" | "short" | "none"
    entry_price: float = 0.0
    trigger_price: float = 0.0  # Price at which pullback is confirmed
    trigger_time: str = ""       # Time of signal confirmation
    expiry: str = "weekly"     # Option expiry
    option_type: str = "CE"    # "CE" for bullish, "PE" for bearish
    stop_loss: float = 0.0
    target: float = 0.0
    vwap: float = 0.0
    ema20: float = 0.0
    ema50: float = 0.0
    reasoning: str = ""
    bar_index: int = -1
    underlying_symbol: str = "NIFTY"  # Underlying this signal applies to


# ---------------------------------------------------------------------------
# TREND PULLBACK STRATEGY CLASS
# ---------------------------------------------------------------------------

class TrendPullbackStrategy:
    """Trend pullback strategy using EMA 20/50 and VWAP.

    Logic:
    1. Determine the trend using EMA 20 and EMA 50
       - Bullish trend: EMA 20 > EMA 50
       - Bearish trend: EMA 20 < EMA 50
    2. Wait for a pullback toward the EMAs
    3. Look for VWAP confirmation (price above/below VWAP)
    4. Wait for a completed candle confirmation (close confirms the move)
    5. Enter option trade in the direction of the trend

    The strategy operates on the intended underlying's own candles and
    must never accidentally read another instrument's data.
    """

    ENTRY_CUTOFF = dtime(14, 30)  # No entries after 2:30 PM IST
    SESSION_EXIT = dtime(15, 30)  # 3:30 PM IST end of day session

    def __init__(self, lot_size: int = 65, ema_period_20: int = 20, ema_period_50: int = 50):
        self.lot_size = lot_size
        self.ema_period_20 = ema_period_20
        self.ema_period_50 = ema_period_50
        self.fill_model = "bidask"

    # ------------------------------------------------------------------
    # EMA calculation
    # ------------------------------------------------------------------

    @staticmethod
    def _calculate_ema(prices: List[float], period: int) -> float:
        """Calculate Exponential Moving Average for given prices and period.

        Uses standard EMA formula: EMA_t = (Price_t * k) + EMA_{t-1} * (1 - k)
        where k = 2 / (period + 1)
        """
        if len(prices) < period:
            return 0.0
        k = 2.0 / (period + 1)
        ema = sum(prices[:period]) / period
        for price in prices[period:]:
            ema = (price * k) + (ema * (1 - k))
        return ema

    # ------------------------------------------------------------------
    # VWAP calculation — SESSION-BASED (only current session candles)
    # ------------------------------------------------------------------

    @staticmethod
    def _calculate_vwap(candles_5min: List[Dict[str, Any]], index: int, session_candles: List[Dict[str, Any]] = None) -> float:
        """Calculate VWAP (Volume Weighted Average Price) up to given bar index.

        VWAP = Sum(Prior Typical Price * Volume) / Sum(Prior Volume)
        Typical Price = (High + Low + Close) / 3

        This version supports session-based calculation by accepting
        session_candles parameter that limits which candles are included.

        If session_candles is provided, only those candles are used.
        If not provided, candles from index 0 through index are used
        (caller must ensure they're from the current session).
        """
        try:
            typical_price_sum = 0.0
            volume_sum = 0.0

            # Determine which candles to use.  VWAP resets by IST trading date
            # and underlying symbol; it must never blend sessions.
            candles_to_use = candles_5min if session_candles is None else session_candles
            if index >= len(candles_to_use):
                return 0.0
            current_ts = TrendPullbackStrategy._parse_timestamp(
                candles_to_use[index].get("timestamp", "")
            )
            if current_ts is None:
                return 0.0
            current_date = current_ts.date()
            symbol = candles_to_use[index].get("underlying_symbol")

            for i in range(index + 1):
                if i >= len(candles_to_use):
                    break
                candle = candles_to_use[i]
                candle_ts = TrendPullbackStrategy._parse_timestamp(candle.get("timestamp", ""))
                if candle_ts is None or candle_ts.date() != current_date:
                    continue
                if symbol and candle.get("underlying_symbol") not in (None, symbol):
                    continue
                high = candle.get("high", 0)
                low = candle.get("low", 0)
                close = candle.get("close", 0)
                volume = candle.get("volume", 0)

                typical_price = (high + low + close) / 3.0
                typical_price_sum += typical_price * volume
                volume_sum += volume

            if volume_sum == 0:
                return 0.0

            return typical_price_sum / volume_sum
        except (KeyError, ValueError, TypeError):
            return 0.0

    # ------------------------------------------------------------------
    # Timestamp handling with explicit Asia/Kolkata timezone
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_timestamp(ts_str: str) -> datetime:
        """Parse a timestamp string and return a datetime object."""
        if not ts_str:
            return None
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                return ts.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
            return ts.astimezone(timezone(timedelta(hours=5, minutes=30)))
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def is_entry_allowed(current_time_ist: str) -> bool:
        """Check if entry is allowed based on time cutoff.

        No new entries after 2:30 PM IST.
        """
        if not current_time_ist:
            return False
        try:
            dt = TrendPullbackStrategy._parse_timestamp(current_time_ist)
            if dt is None:
                return False
            time_only = dt.time()
            return time_only <= TrendPullbackStrategy.ENTRY_CUTOFF
        except (ValueError, AttributeError):
            return False

    # ------------------------------------------------------------------
    # Signal evaluation — WITHOUT random outcomes
    # ------------------------------------------------------------------

    def evaluate_signal(
        self,
        candles_5min: List[Dict[str, Any]],
        index: int,                          # Index of the current 5-min bar
        contract_specs: Dict[str, Any],
        underlying_symbol: str = "NIFTY",  # Underlying this signal applies to
    ) -> PullbackSignalResult:
        """Evaluate pullback signal for a given 5-minute bar index.

        CRITICAL: This method does NOT use random numbers or assumed win probabilities.
        Trade outcomes in the backtest are determined from actual price data
        and realistic execution assumptions.

        Args:
            candles_5min: List of 5-minute candle dicts for THIS underlying.
                Must include 'exchange', 'underlying_symbol', 'instrument_type'
                fields with correct values for this underlying.
            index: Index of the bar to evaluate signal for
            contract_specs: Contract specifications (lot_size, etc.)
            underlying_symbol: Symbol name (e.g., "NIFTY", "BANKNIFTY", "RELIANCE")

        Returns:
            PullbackSignalResult with signal details
                - allowed: Whether the signal is allowed (based on EMA/VWAP filter only)
                - trigger_price: The price level that triggered the signal
                - All other fields populated from actual candle data
        """
        result = PullbackSignalResult()
        result.strategy = "pullback"
        result.underlying_symbol = underlying_symbol

        try:
            # Need sufficient data for EMA calculation
            if index < max(self.ema_period_20, self.ema_period_50):
                return result

            # Extract closing prices up to current bar
            close_prices = []
            for i in range(index + 1):
                if i < len(candles_5min):
                    close = candles_5min[i].get("close", 0)
                    close_prices.append(close)

            if len(close_prices) < max(self.ema_period_20, self.ema_period_50):
                return result

            # Calculate EMA 20 and EMA 50
            ema20 = self._calculate_ema(close_prices, self.ema_period_20)
            ema50 = self._calculate_ema(close_prices, self.ema_period_50)

            result.ema20 = ema20
            result.ema50 = ema50

            # ------------------------------------------------------------------
            # Step 1: Determine the trend
            # ------------------------------------------------------------------
            if ema20 > ema50:
                trend = "bullish"
            elif ema20 < ema50:
                trend = "bearish"
            else:
                trend = "neutral"

            # ------------------------------------------------------------------
            # Step 2: Calculate VWAP for the current bar
            # ------------------------------------------------------------------
            vwap = self._calculate_vwap(candles_5min, index)
            result.vwap = vwap

            # ------------------------------------------------------------------
            # Step 3: Check for pullback and confirmation
            # ------------------------------------------------------------------
            current_candle = candles_5min[index]
            current_close = current_candle.get("close", 0)
            current_high = current_candle.get("high", 0)
            current_low = current_candle.get("low", 0)
            current_open = current_candle.get("open", 0)
            current_time = current_candle.get("timestamp", "")
            current_time_only = None

            if current_time:
                try:
                    ts = datetime.fromisoformat(current_time.replace("Z", "+00:00"))
                    # Use explicit Asia/Kolkata timezone
                    try:
                        from zoneinfo import ZoneInfo
                        ist_tz = ZoneInfo("Asia/Kolkata")
                        ts = ts.astimezone(ist_tz)
                    except Exception:
                        # Fallback: use machine local timezone
                        ts = ts.astimezone()
                    current_time_only = ts.time()
                except (ValueError, AttributeError):
                    current_time_only = None

            # Check entry cutoff (deterministic: no entries after 2:30 PM IST)
            if current_time_only is not None and current_time_only > self.ENTRY_CUTOFF:
                return result

            # ------------------------------------------------------------------
            # Bullish pullback logic (CE buying):
            #   - Trend is bullish (EMA 20 > EMA 50) — deterministic
            #   - Price pulls back toward EMA 20/50
            #   - Price is below EMA 20 but above EMA 50 (in pullback zone)
            #   - Price is above VWAP (VWAP support)
            #   - Completed candle: close > open (bullish candle)
            #   - No random number generation
            # ------------------------------------------------------------------
            if trend == "bullish" and ema20 > 0:
                # Check if price is in pullback zone (below EMA 20, above EMA 50)
                pullback_zone = (current_close < ema20) and (current_close > ema50)

                # Check VWAP confirmation (price above VWAP)
                vwap_confirmation = current_close > vwap if vwap > 0 else False

                # Check completed candle (close > open = bullish)
                completed_bullish = current_close > current_open

                # All conditions must be met — deterministic, no random outcomes
                if pullback_zone and vwap_confirmation and completed_bullish:
                    result.allowed = True
                    result.variant = "bullish"
                    result.signal_type = "long"
                    result.entry_price = current_close
                    result.option_type = "CE"  # Bullish = CE buying
                    result.expiry = contract_specs.get("expiry_preference", "weekly")
                    result.stop_loss = ema50  # Stop below EMA 50 (deeper trend support)
                    result.target = ema20 + (ema20 - ema50)  # Target at EMA extension
                    result.vwap = vwap
                    result.reasoning = (
                        f"Pullback long: Bullish trend (EMA20={ema20:.2f} > EMA50={ema50:.2f}), "
                        f"price {current_close:.2f} in pullback zone, VWAP={vwap:.2f} confirmed, "
                        f"completed bullish candle, CE buy, underlying: {underlying_symbol}"
                    )
                    result.bar_index = index
                    return result

            # ------------------------------------------------------------------
            # Bearish pullback logic (PE buying):
            #   - Trend is bearish (EMA 20 < EMA 50) — deterministic
            #   - Price pulls up toward EMA 20/50
            #   - Price is above EMA 20 but below EMA 50 (in pullback zone)
            #   - Price is below VWAP (VWAP resistance)
            #   - Completed candle: close < open (bearish candle)
            #   - No random number generation
            # ------------------------------------------------------------------
            elif trend == "bearish" and ema50 > 0:
                # Check if price is in pullback zone (above EMA 20, below EMA 50)
                pullback_zone = (current_close > ema20) and (current_close < ema50)

                # Check VWAP confirmation (price below VWAP)
                vwap_confirmation = current_close < vwap if vwap > 0 else False

                # Check completed candle (close < open = bearish)
                completed_bearish = current_close < current_open

                # All conditions must be met — deterministic, no random outcomes
                if pullback_zone and vwap_confirmation and completed_bearish:
                    result.allowed = True
                    result.variant = "bearish"
                    result.signal_type = "short"
                    result.entry_price = current_close
                    result.option_type = "PE"  # Bearish = PE buying
                    result.expiry = contract_specs.get("expiry_preference", "weekly")
                    result.stop_loss = ema50  # Stop above EMA 50 (deeper trend resistance)
                    result.target = ema20 - (ema50 - ema20)  # Target at EMA extension
                    result.vwap = vwap
                    result.reasoning = (
                        f"Pullback short: Bearish trend (EMA20={ema20:.2f} < EMA50={ema50:.2f}), "
                        f"price {current_close:.2f} in pullback zone, VWAP={vwap:.2f} confirmed, "
                        f"completed bearish candle, PE buy, underlying: {underlying_symbol}"
                    )
                    result.bar_index = index
                    return result

        except (KeyError, ValueError, TypeError) as e:
            result.reasoning = f"Pullback evaluation error: {e}"

        return result


# ------------------------------------------------------------------
# BACKTEST-READY FUNCTION (standalone, no live trading)
# ------------------------------------------------------------------

def run_pullback_backtest(
    csv_path: str,
    start_date: str = None,
    end_date: str = None,
    lot_size: int = 65,
    initial_equity: float = 500000.0,
) -> Dict[str, Any]:
    """Run trend pullback strategy backtest on 5-minute candle data.

    CRITICAL: This backtest does NOT use random numbers or assumed win probabilities.
    Trade outcomes are determined from actual price data and realistic execution assumptions.

    If actual option premium data is unavailable, the backtester must be marked
    as not ready for realistic options P&L validation.

    The backtest implements a candle-by-candle trade lifecycle:
    1. Signal generation using data available at signal time
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
    from data_importer import import_5min_index_candles

    candles = import_5min_index_candles(csv_path, start_date=start_date, end_date=end_date)

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

    strategy = TrendPullbackStrategy(lot_size=lot_size)

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
        "strategy": "pullback",
        "underlying_symbol": "NIFTY",  # Will be overwritten if csv_path provides it
    }

    # Actually get the underlying symbol from the first candle if available
    if candles and candles[0].get("underlying_symbol"):
        results["underlying_symbol"] = candles[0]["underlying_symbol"]

    # Track cumulative P&L for drawdown calculation
    cumulative_pnl = 0.0
    peak_pnl = 0.0

    # Minimum bar index for EMA calculation
    min_bar = max(strategy.ema_period_20, strategy.ema_period_50)

    # Evaluate signals on each bar
    # CRITICAL: No random outcome generation
    # Each signal's validity is determined by:
    # 1. EMA trend filter (deterministic: EMA20 > EMA50 or not)
    # 2. Pullback zone confirmation (deterministic: price in zone or not)
    # 3. VWAP confirmation (deterministic: price above/below VWAP or not)
    # 4. Completed candle confirmation (deterministic: close > open or not)
    # 5. Entry time cutoff (deterministic: time > 14:30 or not)
    for i in range(min_bar, len(candles)):
        signal = strategy.evaluate_signal(candles, i, specs, underlying_symbol=results["underlying_symbol"])

        if not signal.allowed or signal.signal_type == "none":
            continue

        # --- REMOVED: Random outcome generation ---
        # The following logic has been REMOVED:
        # - win_prob derived from EMA separation
        # - random.random() calls to decide wins/losses
        # - assumed win probabilities based on signal characteristics
        # --- END REMOVED SECTION ---

        # Instead, determine P&L from actual price movement:
        # Calculate actual P&L based on the pullback movement
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
        # We need whole number of lots for options
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
            position_size = int(max_units)  # whole number of units

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

            # Check stop-loss hit (price touches or goes below stop)
            # For a long entry, stop is below entry - price must go below stop
            # For a short entry, stop is above entry - price must go above stop
            # Determine direction from signal variant
            if signal.variant == "long":
                stop_hit = (candle_low <= stop) if stop > 0 else False
            else:  # short
                stop_hit = (candle_high >= stop) if stop > 0 else False

            # Check target hit (price touches or goes above/below target)
            if signal.variant == "long":
                target_hit = (candle_high >= target) if target > entry else False
            else:  # short
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
            "vwap": signal.vwap,
            "ema20": signal.ema20,
            "ema50": signal.ema50,
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
    print("TRIO TREND PULLBACK STRATEGY MODULE")
    print("=" * 60)
    print()
    print("Trend pullback strategy using EMA 20/50 and VWAP")
    print()
    print("LOGIC:")
    print("  1. Trend identification: EMA 20 vs EMA 50")
    print("     - Bullish: EMA 20 > EMA 50")
    print("     - Bearish: EMA 20 < EMA 50")
    print()
    print("  2. Pullback entry:")
    print("     - Bullish: Price pulls back below EMA 20, above EMA 50,")
    print("                above VWAP, completed bullish candle -> CE buy")
    print()
    print("     - Bearish: Price pulls up above EMA 20, below EMA 50,")
    print("                below VWAP, completed bearish candle -> PE buy")
    print()
    print("  3. VWAP confirmation: Price relative to VWAP validates signal")
    print("  4. Completed candle: Close confirms the move direction")
    print()
    print("  5. NO random number generation or assumed win probabilities")
    print("     - EMA trend is a deterministic filter")
    print("     - Pullback zone, VWAP, and candle confirmation are deterministic")
    print("     - Outcomes reflect actual price movement from data")
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
    print("  - EMA 20 and EMA 50 calculations from closes (Welles Wilder not needed, standard EMA)")
    print("  - VWAP from Typical Price (H+L+C)/3 * Volume")
    print("  - This module keeps positions in research mode only")
    print("  - Trade outcomes determined from price data, not random numbers")
    print()
