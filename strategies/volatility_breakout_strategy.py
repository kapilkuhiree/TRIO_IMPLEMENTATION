"""
Strategy D: Volatility Breakout
- Large price movements and high volatility
- Price breaks out from consolidation range
- Breakouts driven by volatility expansion
- Bullish setups evaluate CE buying
- Bearish setups evaluate PE buying

Logic:
1. Calculate ATR (Average True Range) for volatility measurement
2. Identify consolidation period (price range between ATR-based bands)
3. Wait for significant volatility breakout (price moves beyond ATR bands)
4. Confirm breakout with volume increase
5. Enter option trade in breakout direction

CRITICAL: Trade outcomes are determined from actual market data, NOT from random numbers or assumed win probabilities.

KEEP IN RESEARCH MODE ONLY -- Do not backtest without suitable historical data.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, time as dtime
from pathlib import Path
import math


# ---------------------------------------------------------------------------
# VOLATILITY BREAKOUT SIGNAL RESULT
# ---------------------------------------------------------------------------

@dataclass
class VolatilityBreakoutSignalResult:
    """Result from volatility breakout signal evaluation."""
    allowed: bool = False
    strategy: str = "volatility_breakout"
    variant: str = "bullish"  # "bullish" | "bearish"
    signal_type: str = "breakout"  # "breakout" | "breakdown" | "none"
    entry_price: float = 0.0
    underlying_symbol: str = "NIFTY"  # Underlying this signal applies to
    trigger_price: float = 0.0  # Price at breakout/breakdown confirmation
    trigger_time: str = ""       # Time of confirmation
    expiry: str = "weekly"     # Option expiry
    option_type: str = "CE"    # "CE" for bullish, "PE" for bearish
    stop_loss: float = 0.0
    target: float = 0.0
    atr: float = 0.0             # ATR value at signal
    volume: float = 0.0
    reasoning: str = ""
    bar_index: int = -1


# ---------------------------------------------------------------------------
# VOLATILITY BREAKOUT STRATEGY CLASS
# ---------------------------------------------------------------------------

class VolatilityBreakoutStrategy:
    """Volatility breakout strategy using ATR bands and volume confirmation.

    Logic:
    1. Calculate ATR(14) for volatility measurement
    2. Define consolidation range using ATR (Price within ATR*2 bands)
    3. Wait for price to break outside ATR bands
    4. Confirm breakout with volume spike (volume > average volume * 1.5)
    5. Enter option trade in breakout direction
    6. Use strict risk management with ATR-based stops

    CRITICAL: Trade outcomes are determined from actual market data,
    NOT from random numbers or assumed win probabilities.

    The strategy operates on the intended underlying's own candles and
    must never accidentally read another instrument's data.
    """

    # ATR period for volatility calculation
    ATR_PERIOD = 14

    # ATR multiplier for consolidation range
    ATR_CONSOLIDATION_MULT = 2.0

    # ATR multiplier for breakout confirmation
    ATR_BREAKOUT_MULT = 1.0

    # Volume confirmation multiplier
    VOLUME_CONFIRMATION_MULT = 1.5

    # Entry cutoff
    ENTRY_CUTOFF = dtime(14, 30)

    # Session exit cutoff
    SESSION_EXIT = dtime(15, 30)  # 3:30 PM IST end of day session

    def __init__(self, lot_size: int = 65, atr_period: int = 14):
        self.lot_size = lot_size
        self.adx_period = atr_period
        self.fill_model = "bidask"
        # State tracking for consecutive confirmation
        self._bullish_confirmation_count = 0
        self._bearish_confirmation_count = 0

    # ------------------------------------------------------------------
    # ATR calculation (Welles Wilder method)
    # ------------------------------------------------------------------

    @staticmethod
    def _calculate_atr(highs: List[float], lows: List[float], closes: List[float], period: int) -> float:
        """Calculate Average True Range (ATR) using Welles Wilder method.

        Welles Wilder's ATR calculation:
        - True Range = max(high-low, abs(high-prev_close), abs(low-prev_close))
        - Smoothed ATR = ((previous ATR * (period - 1)) + current TR) / period
        - First ATR = sum of TR over period / period

        Returns ATR value
        """
        if period <= 0 or len(highs) != len(lows) or len(highs) != len(closes):
            return 0.0
        if len(highs) < period + 1:
            return 0.0

        # Calculate True Ranges
        tr_values: List[float] = []
        for i in range(1, len(highs)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1])
            )
            tr_values.append(tr)

        # Initial ATR (first period)
        atr = sum(tr_values[:period]) / period

        # Smooth subsequent ATR values
        for i in range(period, len(tr_values)):
            atr = ((atr * (period - 1)) + tr_values[i]) / period

        return atr

    @staticmethod
    def _calculate_average_volume(volumes: List[float], period: int) -> float:
        """Calculate average volume over period."""
        if not volumes or period <= 0 or len(volumes) < period:
            return 0.0
        return sum(volumes[-period:]) / period

    @staticmethod
    def _parse_timestamp(ts_str: str):
        """Parse timestamp string to datetime object.

        Supports ISO format with optional timezone.
        Returns None if parsing fails.
        """
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
        candles_5min: List[Dict[str, Any]],
        index: int,                          # Index of the current 5-minute bar
        contract_specs: Dict[str, Any],
        underlying_symbol: str = "NIFTY",  # Underlying this signal applies to
    ) -> VolatilityBreakoutSignalResult:
        """Evaluate volatility breakout signal for a given 5-minute bar index.

        CRITICAL: This method does NOT use random numbers or assumed win probabilities.
        Trade outcomes in the backtest are determined from actual price data
        and realistic execution assumptions.

        Args:
            candles_5min: List of completed 5-minute candle dicts for THIS underlying.
                Must include 'exchange', 'underlying_symbol', 'instrument_type'
                fields for the same NSE index or stock.
            index: Index of the bar to evaluate signal for
            contract_specs: Contract specifications (lot_size, strike_interval, etc.)
            underlying_symbol: Symbol name (e.g., "NIFTY", "BANKNIFTY", "RELIANCE")

        Returns:
            VolatilityBreakoutSignalResult with signal details
                - allowed: Whether the signal is allowed (based on ATR breakout filter only)
                - trigger_price: The price level that triggered the signal
                - All other fields populated from actual candle data
        """
        result = VolatilityBreakoutSignalResult()
        result.strategy = "volatility_breakout"
        result.underlying_symbol = underlying_symbol

        try:
            # Need sufficient data for ATR calculation
            if index < self.ATR_PERIOD:
                return result

            # Extract data for volatility calculation
            highs = []
            lows = []
            closes = []
            volumes = []
            timestamps = []

            for i in range(index + 1):
                if i < len(candles_5min):
                    candle = candles_5min[i]
                    highs.append(candle.get("high", 0))
                    lows.append(candle.get("low", 0))
                    closes.append(candle.get("close", 0))
                    volumes.append(candle.get("volume", 0))
                    timestamps.append(candle.get("timestamp", ""))

            # Need sufficient data for ATR calculation
            if len(highs) < self.ATR_PERIOD:
                return result

            # Calculate ATR for volatility measurement
            atr = self._calculate_atr(highs, lows, closes, self.ATR_PERIOD)
            if atr == 0:
                return result

            # Define consolidation range using ATR
            consolidation_high = closes[-1] - (self.ATR_CONSOLIDATION_MULT * atr)
            consolidation_low = closes[-1] + (self.ATR_CONSOLIDATION_MULT * atr)

            # Calculate average volume for confirmation
            avg_volume = self._calculate_average_volume(volumes, self.ATR_PERIOD)

            # Check entry cutoff
            current_time = self._parse_timestamp(timestamps[-1])
            if current_time and current_time.time() > self.ENTRY_CUTOFF:
                return result

            # Bullish breakout (price moves above consolidation high)
            if closes[-1] > consolidation_high:
                # Volume confirmation
                volume_confirmed = avg_volume > 0 and volumes[-1] > (avg_volume * self.VOLUME_CONFIRMATION_MULT)

                if volume_confirmed:
                    result.allowed = True
                    result.variant = "bullish"
                    result.signal_type = "breakout"
                    result.entry_price = closes[-1]
                    result.trigger_price = consolidation_high
                    result.trigger_time = timestamps[-1]
                    result.expiry = contract_specs.get("expiry_preference", "weekly")
                    result.option_type = "CE"
                    result.stop_loss = consolidation_low - (self.ATR_BREAKOUT_MULT * atr)
                    result.target = closes[-1] + (self.ATR_BREAKOUT_MULT * atr * 2)
                    result.atr = atr
                    result.volume = volumes[-1]
                    result.reasoning = (
                        f"Volatility breakout bullish: Price {closes[-1]:.2f} > consolidation high {consolidation_high:.2f}, "
                        f"volume {volumes[-1]:.0f} > average volume {avg_volume:.0f}, "
                        f"underlying: {underlying_symbol}"
                    )
                    result.bar_index = index
                    return result

            # Bearish breakdown (price moves below consolidation low)
            if closes[-1] < consolidation_low:
                # Volume confirmation
                volume_confirmed = avg_volume > 0 and volumes[-1] > (avg_volume * self.VOLUME_CONFIRMATION_MULT)

                if volume_confirmed:
                    result.allowed = True
                    result.variant = "bearish"
                    result.signal_type = "breakdown"
                    result.entry_price = closes[-1]
                    result.trigger_price = consolidation_low
                    result.trigger_time = timestamps[-1]
                    result.expiry = contract_specs.get("expiry_preference", "weekly")
                    result.option_type = "PE"
                    result.stop_loss = consolidation_high + (self.ATR_BREAKOUT_MULT * atr)
                    result.target = closes[-1] - (self.ATR_BREAKOUT_MULT * atr * 2)
                    result.atr = atr
                    result.volume = volumes[-1]
                    result.reasoning = (
                        f"Volatility breakout bearish: Price {closes[-1]:.2f} < consolidation low {consolidation_low:.2f}, "
                        f"volume {volumes[-1]:.0f} > average volume {avg_volume:.0f}, "
                        f"underlying: {underlying_symbol}"
                    )
                    result.bar_index = index
                    return result

            return result
        except Exception as e:
            result.reasoning = f"Volatility breakout evaluation error: {e}"
            return result
