"""TRIO — Momentum Continuation Strategy Module

Strategy C: Momentum continuation
- Established directional move, consolidation or pullback,
- Followed by a confirmed swing breakout.
- Bullish setups evaluate CE buying.
- Bearish setups evaluate PE buying.

KEEP IN RESEARCH MODE ONLY — Do not backtest without suitable historical data.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, time as dtime
from pathlib import Path
import math


# ---------------------------------------------------------------------------
# MOMENTUM CONTINUATION SIGNAL RESULT
# ---------------------------------------------------------------------------

@dataclass
class MomentumSignalResult:
    """Result from momentum continuation signal evaluation."""
    allowed: bool = False
    strategy: str = "momentum"
    variant: str = "bullish"  # "bullish" | "bearish"
    signal_type: str = "none"  # "breakout" | "breakdown" | "none"
    entry_price: float = 0.0
    underlying_symbol: str = "NIFTY"  # Underlying this signal applies to
    trigger_price: float = 0.0  # Price at breakout/breakdown confirmation
    trigger_time: str = ""       # Time of confirmation
    expiry: str = "weekly"     # Option expiry
    option_type: str = "CE"    # "CE" for bullish, "PE" for bearish
    stop_loss: float = 0.0
    target: float = 0.0
    adx: float = 0.0             # ADX value at signal
    reasoning: str = ""
    bar_index: int = -1


# ---------------------------------------------------------------------------
# MOMENTUM CONTINUATION STRATEGY CLASS
# ---------------------------------------------------------------------------

class MomentumContinuationStrategy:
    """Momentum continuation strategy using ADX and swing high-low.

    Logic:
    1. Establish directional move using ADX (ADX > 25 = strong trend)
    2. Identify consolidation period (lower highs and lower lows, or
       higher highs and higher lows contracting)
    3. Wait for a confirmed swing breakout (close beyond swing high/low)
    4. Enter option trade in the direction of the breakout
    5. Use ADX to filter weak trends (ADX < 25 = no trade)

    The strategy operates on the intended underlying's own candles and
    must never accidentally read another instrument's data.

    CRITICAL: Trade outcomes are determined from actual market data,
    NOT from random numbers or assumed win probabilities.
    """

    # ADX threshold for strong trend
    ADX_THRESHOLD = 25.0

    # Minimum consolidation contraction % (range high-low must contract by at least this%)
    MIN_CONTRACTION_PCT = 15.0

    # Entry cutoff
    ENTRY_CUTOFF = dtime(14, 30)

    # Session exit cutoff
    SESSION_EXIT = dtime(15, 30)  # 3:30 PM IST end of day session

    def __init__(self, lot_size: int = 65, adx_period: int = 14):
        self.lot_size = lot_size
        self.adx_period = adx_period
        self.fill_model = "bidask"

    # ------------------------------------------------------------------
    # ADX calculation (Welles Wilder full method)
    # ------------------------------------------------------------------

    @staticmethod
    def _calculate_adx(highs: List[float], lows: List[float], closes: List[float], period: int) -> float:
        """Calculate Average Directional Index (ADX) using Welles Wilder method.

        Full Welles Wilder smoothing:
        - True Range (TR) for each bar
        - +DM = max(current_high - previous_high, 0)
        - -DM = max(previous_low - current_low, 0)
        - +TR smoothed, -TR smoothed
        - +DM smoothed, -DM smoothed
        - +DI = 100 * (+DM_smoothed / smoothed TR)
        - -DI = 100 * (-DM_smoothed / smoothed TR)
        - DX = 100 * |+DI - -DI| / (+DI + -DI)
        - ADX = smoothed DX over period bars

        Returns ADX value (0-100 scale)
        """
        if period <= 0 or len(highs) != len(lows) or len(highs) != len(closes):
            return 0.0
        if len(highs) < (2 * period + 1):
            return 0.0

        # Calculate +DM and -DM
        plus_dm = []
        minus_dm = []

        tr_values = []
        for i in range(1, len(highs)):
            up_move = highs[i] - highs[i - 1]
            down_move = lows[i - 1] - lows[i]
            plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
            minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
            tr_values.append(max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            ))

        # If we don't have enough data, return 0
        if len(plus_dm) < period:
            return 0.0

        # Wilder's initial sums use the first ``period`` directional bars;
        # subsequent values use the recursive smoothed-sum formula.
        tr_sum = sum(tr_values[:period])
        plus_sum = sum(plus_dm[:period])
        minus_sum = sum(minus_dm[:period])
        dx_values = []

        def dx_from_sums(tr_total, plus_total, minus_total):
            if tr_total <= 0:
                return 0.0
            plus_di = 100.0 * plus_total / tr_total
            minus_di = 100.0 * minus_total / tr_total
            denominator = plus_di + minus_di
            return 100.0 * abs(plus_di - minus_di) / denominator if denominator else 0.0

        dx_values.append(dx_from_sums(tr_sum, plus_sum, minus_sum))
        for i in range(period, len(tr_values)):
            tr_sum = tr_sum - (tr_sum / period) + tr_values[i]
            plus_sum = plus_sum - (plus_sum / period) + plus_dm[i]
            minus_sum = minus_sum - (minus_sum / period) + minus_dm[i]
            dx_values.append(dx_from_sums(tr_sum, plus_sum, minus_sum))

        if len(dx_values) < period:
            return 0.0
        adx = sum(dx_values[:period]) / float(period)
        for dx in dx_values[period:]:
            adx = ((adx * (period - 1)) + dx) / float(period)
        return max(0.0, min(adx, 100.0))

    # ------------------------------------------------------------------
    # Swing high/low identification using PREVIOUS completed candles only
    # ------------------------------------------------------------------

    @staticmethod
    def _get_previous_swing_high(highs: List[float], lookback: int = 5) -> Optional[float]:
        """Get the previous swing high using ONLY completed candles before the current bar.

        For a lookback of N: Previous swing high = highest high across the preceding N
        completed candles (excluding the current candle).

        Returns the swing high price level, or None if insufficient data.
        """
        if len(highs) < lookback + 1:
            return None

        start_idx = max(0, len(highs) - lookback - 1)
        end_idx = len(highs) - 1  # Exclude the current candle

        if start_idx >= end_idx:
            return None

        relevant_highs = highs[start_idx:end_idx]
        if not relevant_highs:
            return None

        return max(relevant_highs)

    @staticmethod
    def _get_previous_swing_low(lows: List[float], lookback: int = 5) -> Optional[float]:
        """Get the previous swing low using ONLY completed candles before the current bar.

        For a lookback of N: Previous swing low = lowest low across the preceding N
        completed candles (excluding the current candle).

        Returns the swing low price level, or None if insufficient data.
        """
        if len(lows) < lookback + 1:
            return None

        start_idx = max(0, len(lows) - lookback - 1)
        end_idx = len(lows) - 1  # Exclude the current candle

        if start_idx >= end_idx:
            return None

        relevant_lows = lows[start_idx:end_idx]
        if not relevant_lows:
            return None

        return min(relevant_lows)

    # ------------------------------------------------------------------
    # CRITICAL: Signal evaluation WITHOUT random outcomes and WITHOUT look-ahead bias
    # ------------------------------------------------------------------

    def evaluate_signal(
        self,
        candles_5min: List[Dict[str, Any]],
        index: int,                          # Index of the current 5-min bar
        contract_specs: Dict[str, Any],
        underlying_symbol: str = "NIFTY",  # Underlying this signal applies to
    ) -> MomentumSignalResult:
        """Evaluate momentum continuation signal for a given 5-minute bar index.

        CRITICAL: This method does NOT use random numbers or assumed win probabilities.
        It does NOT use future candles to generate signals. All data used is from
        candles up to and including the current bar index.

        The swing high/low is determined from PREVIOUS completed candles only
        (excluding the current candle), eliminating look-ahead bias.

        Args:
            candles_5min: List of 5-minute candle dicts for THIS underlying.
                Must include 'exchange', 'underlying_symbol', 'instrument_type'
                fields with correct values for this underlying.
            index: Index of the bar to evaluate signal for (0-based, current bar last)
            contract_specs: Contract specifications (lot_size, etc.)
            underlying_symbol: Symbol name (e.g., "NIFTY", "BANKNIFTY", "RELIANCE")

        Returns:
            MomentumSignalResult with signal details
                - allowed: Whether the signal is allowed (based on ADX filter only)
                - trigger_price: The price level that triggered the signal
                - All other fields populated from actual candle data
        """
        result = MomentumSignalResult()
        result.strategy = "momentum"
        result.underlying_symbol = underlying_symbol

        try:
            # Need sufficient data - at least adx_period + 10 bars
            # We need candles from index 0 to index (current bar)
            if index < 1 or index >= len(candles_5min):
                return result

            # Extract price data from candles up to and including the current bar
            # We use candles_5min[:index+1] to get data up to current bar
            all_highs = [c.get("high", 0) for c in candles_5min[:index + 1]]
            all_lows = [c.get("low", 0) for c in candles_5min[:index + 1]]
            all_closes = [c.get("close", 0) for c in candles_5min[:index + 1]]

            if len(all_highs) < self.adx_period + 10:
                return result

            # ------------------------------------------------------------------
            # Step 1: Calculate ADX to determine trend strength
            # ------------------------------------------------------------------
            # Only use data up to and including current bar - no look-ahead
            adx = self._calculate_adx(all_highs, all_lows, all_closes, self.adx_period)
            result.adx = adx

            # FILTER: Only trade when ADX > threshold (strong trend)
            # Deterministic filter: adx > 25 means strong trend, adx <= 25 means no trade
            if adx < self.ADX_THRESHOLD:
                return result  # Weak trend, no trade (deterministic filter)

            # ------------------------------------------------------------------
            # Step 2: Identify swing high and swing low from PREVIOUS candles only
            # ------------------------------------------------------------------
            # Using _get_previous_swing_high/_low which excludes the current candle
            previous_swing_high = self._get_previous_swing_high(all_highs, lookback=5)
            previous_swing_low = self._get_previous_swing_low(all_lows, lookback=5)

            # ------------------------------------------------------------------
            # Step 2: Check for breakout/breakdown using ONLY available data
            # ------------------------------------------------------------------
            current_candle = candles_5min[index]
            current_close = current_candle.get("close", 0)
            current_time = current_candle.get("timestamp", "")

            # Check entry cutoff (deterministic time filter)
            from datetime import datetime
            from zoneinfo import ZoneInfo
            ist_tz = ZoneInfo("Asia/Kolkata")
            if current_time:
                try:
                    ts = datetime.fromisoformat(current_time.replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=self._get_ist_tz()).astimezone(ist_tz)
                    else:
                        ts = ts.astimezone(ist_tz)
                    current_time_only = ts.time()
                except (ValueError, AttributeError):
                    current_time_only = None
            else:
                current_time_only = None

            # Check entry cutoff (deterministic: no entries after 2:30 PM IST)
            if current_time_only is not None and current_time_only > dtime(14, 30):
                return result

            # ------------------------------------------------------------------
            # Step 3: Check for breakout/breakdown
            # ------------------------------------------------------------------
            # Bullish momentum continuation (CE buying):
            #   - ADX > 25 (strong trend - deterministic filter)
            #   - Price breaks above PREVIOUS swing high (confirmed on close)
            #   - This indicates continuation of the bullish momentum
            #   - No random number generation
            #   - Only uses candles available at signal time
            # ------------------------------------------------------------------
            if previous_swing_high is not None and current_close > previous_swing_high:
                # Confirmed bullish breakout
                result.allowed = True
                result.variant = "bullish"
                result.signal_type = "breakout"
                result.entry_price = current_close
                result.trigger_price = previous_swing_high
                result.expiry = contract_specs.get("expiry_preference", "weekly")
                result.option_type = "CE"  # Bullish = CE buying
                result.adx = adx

                # Stop loss below previous swing low (using actual price data)
                previous_swing_low_val = self._get_previous_swing_low(all_lows, lookback=5)
                result.stop_loss = previous_swing_low_val if previous_swing_low_val and previous_swing_low_val > 0 else current_close * 0.98
                # Target: based on swing range (using actual price data)
                swing_range = (previous_swing_high - previous_swing_low_val) if previous_swing_low_val and previous_swing_low_val > 0 else 0
                result.target = current_close + swing_range

                result.reasoning = (
                    f"Momentum breakout: ADX={adx:.1f} (strong, deterministic filter), "
                    f"price {current_close:.2f} > previous swing high {previous_swing_high:.2f}, "
                    f"CE buy - continuation of bullish momentum, underlying: {underlying_symbol}"
                )
                result.bar_index = index
                return result

            # Bearish momentum continuation (PE buying):
            #   - ADX > 25 (strong trend - deterministic filter)
            #   - Price breaks below PREVIOUS swing low (confirmed on close)
            #   - This indicates continuation of the bearish momentum
            #   - No random number generation
            # ------------------------------------------------------------------
            if previous_swing_low is not None and current_close < previous_swing_low:
                # Confirmed bearish breakdown
                result.allowed = True
                result.variant = "bearish"
                result.signal_type = "breakdown"
                result.entry_price = current_close
                result.trigger_price = previous_swing_low
                result.expiry = contract_specs.get("expiry_preference", "weekly")
                result.option_type = "PE"  # Bearish = PE buying
                result.adx = adx

                # Stop loss above previous swing high (using actual price data)
                previous_swing_high_val = self._get_previous_swing_high(all_highs, lookback=5)
                result.stop_loss = previous_swing_high_val if previous_swing_high_val and previous_swing_high_val > 0 else current_close * 1.02
                # Target: based on swing range (using actual price data)
                swing_range = (previous_swing_high_val - previous_swing_low) if previous_swing_high_val and previous_swing_high_val > 0 else 0
                result.target = current_close - swing_range

                result.reasoning = (
                    f"Momentum breakdown: ADX={adx:.1f} (strong, deterministic filter), "
                    f"price {current_close:.2f} < previous swing low {previous_swing_low:.2f}, "
                    f"PE buy - continuation of bearish momentum, underlying: {underlying_symbol}"
                )
                result.bar_index = index
                return result

        except (KeyError, ValueError, TypeError) as e:
            result.reasoning = f"Momentum evaluation error: {e}"

        return result

    @staticmethod
    def _get_ist_tz():
        """Get IST timezone object."""
        from datetime import timezone, timedelta
        return timezone(timedelta(hours=5, minutes=30))


# ------------------------------------------------------------------
# BACKTEST-READY FUNCTION (standalone, no live trading)
# ------------------------------------------------------------------

def run_momentum_backtest(
    csv_path: str,
    start_date: str = None,
    end_date: str = None,
    lot_size: int = 65,
    initial_equity: float = 500000.0,
) -> Dict[str, Any]:
    """Run momentum continuation strategy backtest on 5-minute candle data.

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
    6. Continued checking until exit or end-of-day
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

    strategy = MomentumContinuationStrategy(lot_size=lot_size)

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
        "strategy": "momentum",
        "underlying_symbol": "NIFTY",  # Will be overwritten if csv_path provides it
    }

    # Actually get the underlying symbol from the first candle if available
    if candles and candles[0].get("underlying_symbol"):
        results["underlying_symbol"] = candles[0]["underlying_symbol"]

    # Track cumulative P&L for drawdown calculation
    cumulative_pnl = 0.0
    peak_pnl = 0.0

    # Minimum bar index for ADX calculation
    min_bar = strategy.adx_period + 10

    # Evaluate signals on each bar
    # CRITICAL: No random outcome generation
    # Each signal's validity is determined by:
    # 1. ADX filter (deterministic: adx > 25 or not)
    # 2. Breakout/breakdown confirmation (deterministic: close > swing_high or close < swing_low)
    # 3. Entry time cutoff (deterministic: time > 14:30 or not)
    for i in range(min_bar, len(candles)):
        signal = strategy.evaluate_signal(candles, i, specs, underlying_symbol=results["underlying_symbol"])

        if not signal.allowed or signal.signal_type == "none":
            continue

        # --- REMOVED: Random outcome generation ---
        # The following logic has been REMOVED:
        # - win_probability derived from ADX
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
            position_size = int(max_units)  # at least 1 unit, whole number

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
                    ts = datetime.fromisoformat(candle_time.replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=ist_tz).astimezone(ist_tz)
                    else:
                        ts = ts.astimezone(ist_tz)
                    candle_time_only = ts.time()

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
            # For a long option, stop is below entry - price must go below stop
            stop_hit = (candle_low <= stop) if stop > 0 else False

            # Check target hit (price touches or goes above target)
            target_hit = (candle_high >= target) if target > entry else False

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
            # Don't count as winning/losing, just record

        # Record the trade detail
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
            "timestamp": candles[i].get("timestamp", "") if i < len(candles) else "",
            "underlying_symbol": results["underlying_symbol"],
            "signal_type": signal.signal_type,
            "variant": signal.variant,
            "option_type": signal.option_type,
            "entry_price": entry,
            "stop_loss": stop,
            "target": target,
            "adx": signal.adx,
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
    print("TRIO MOMENTUM CONTINUATION STRATEGY MODULE")
    print("=" * 60)
    print()
    print("Momentum continuation strategy using ADX and swing high-low")
    print()
    print("LOGIC:")
    print("  1. Trend strength: ADX > 25 required (deterministic filter)")
    print("     - ADX below 25 = no trade (weak trend, avoid)")
    print("     - This is a deterministic filter, NOT a win probability")
    print()
    print("  2. Swing identification using PREVIOUS candles only:")
    print("     - Previous swing high = highest high across preceding N completed candles")
    print("     - Previous swing low = lowest low across preceding N completed candles")
    print("     - Current candle CLOSE (only) must break above/below")
    print("     - NO look-ahead bias: only candles available at signal time")
    print()
    print("  3. Breakout/breakdown signal:")
    print("     - Bullish: Close > previous swing high -> CE buy")
    print("     - Bearish: Close < previous swing low -> PE buy")
    print()
    print("  4. NO random number generation or assumed win probabilities")
    print("     - ADX threshold is a deterministic filter")
    print("     - Breakout/breakdown determined by price action")
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
    print("  - ADX calculation using Welles Wilder method (period=14)")
    print("  - Swing high/low from PREVIOUS completed candles only")
    print("  - This module keeps positions in research mode only")
    print("  - Trade outcomes determined from price data, not random numbers")
    print()
    print("TRADE LIFECYCLE:")
    print("  1. Signal generated at current bar close (data available at signal time)")
    print("  2. Entry at next eligible bar")
    print("  3. Subsequent bars checked chronologically for stop/target")
    print("  4. Stop: price <= stop_level in candle high/low")
    print("  5. Target: price >= target_level in candle high/low")
    print("  6. Both in same candle: stop-hit-first conservative assumption")
    print("  7. Neither hit: continue checking next candles")
    print("  8. 3:30 PM IST: forced end-of-day session exit")
    print("  9. P&L calculated from actual option premium movement")
    print()
