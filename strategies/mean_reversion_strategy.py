"""
TRIO — Mean Reversion Strategy Module (Pullback focused)
Author: Claude Code
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from datetime import datetime
import math

# The 200-EMA trend needs a real 200-bar window.  Fewer bars used to produce a
# silently understated average (dividing a short window by 200), which made the
# "bullish trend" branch almost always true.
HISTORY_BARS = 200
EMA_PERIOD = 50
TREND_EMA_PERIOD = 200
# Mean reversion needs a genuine extension away from the mean, not mere
# proximity.  A fixed 0.5% band fired on trend-continuation bars that happened
# to hug the 50 EMA.  Scaling the required stretch by ATR(14) adapts to
# volatility and was materially more robust out of sample (see
# tests/test_mean_reversion_strategy.py and the session research notes).
STRETCH_ATR_MULT = 0.5
ATR_PERIOD = 14
RSI_PERIOD = 14
RSI_LONG_MAX = 40.0   # documented long trigger: RSI(14) below 40
RSI_SHORT_MIN = 60.0  # documented short trigger: RSI(14) above 60


@dataclass
class MeanReversionSignal:
    allowed: bool = False
    strategy: str = "mean_reversion"
    variant: str = "bullish"  # bullish | bearish
    entry_price: float = 0.0
    option_type: str = "CE"
    stop_loss: float = 0.0
    target: float = 0.0
    reasoning: str = ""


class MeanReversionStrategy:
    """Mean Reversion / Pullback Strategy.

    Logic:
    1. Trend: Close vs 200 EMA
    2. Setup: Price stretched at least 0.5 * ATR(14) beyond the 50 EMA in the
       direction opposite to the trade (a real pullback, not mere proximity)
    3. Trigger: RSI(14) < 40 (long) or RSI(14) > 60 (short)

    The trigger is treated as an event, not a state: the same directional setup
    must be false on the previous completed bar.  The shared planner only ever
    evaluates the latest completed bar, so a persistent condition used to be
    re-signalled on every scan (and re-entered immediately after an exit).
    """

    def __init__(self):
        pass

    def evaluate_signal(
        self,
        candles: List[Dict[str, Any]],
        index: int,
    ) -> MeanReversionSignal:
        result = MeanReversionSignal()
        if not isinstance(candles, (list, tuple)):
            return result
        if index < HISTORY_BARS - 1 or index >= len(candles):
            return result

        closes = self._closes(candles[:index + 1])
        if closes is None:
            return result

        ema50 = self._calculate_ema(closes, EMA_PERIOD)
        trend = self._calculate_ema(closes, TREND_EMA_PERIOD)
        rsi = self._rsi_series(closes, RSI_PERIOD)
        atr = self._atr_series(candles[:index + 1], ATR_PERIOD)
        current = self._setup_state(
            closes, ema50, trend, rsi, atr, len(closes) - 1)
        if current is None:
            return result
        previous = self._setup_state(
            closes, ema50, trend, rsi, atr, len(closes) - 2)

        candle = candles[index]
        try:
            low = float(candle["low"])
            high = float(candle["high"])
        except (KeyError, TypeError, ValueError):
            return result

        if current["bull"] and not (previous and previous["bull"]):
            result.allowed = True
            result.variant = "bullish"
            result.option_type = "CE"
            result.entry_price = current["close"]
            result.stop_loss = low * 0.99
            result.target = current["close"] * 1.015
            result.reasoning = (
                "Bullish Trend + 50 EMA ATR Stretch "
                f"(RSI {current['rsi']:.1f} < {RSI_LONG_MAX:g}, "
                f"{current['stretch']:.2f} pts beyond 50 EMA)"
            )
        elif current["bear"] and not (previous and previous["bear"]):
            result.allowed = True
            result.variant = "bearish"
            result.option_type = "PE"
            result.entry_price = current["close"]
            result.stop_loss = high * 1.01
            result.target = current["close"] * 0.985
            result.reasoning = (
                "Bearish Trend + 50 EMA ATR Stretch "
                f"(RSI {current['rsi']:.1f} > {RSI_SHORT_MIN:g}, "
                f"{current['stretch']:.2f} pts beyond 50 EMA)"
            )

        return result

    # ------------------------------------------------------------------
    # Indicators and setup evaluation
    # ------------------------------------------------------------------

    @staticmethod
    def _closes(rows: List[Dict[str, Any]]) -> Optional[List[float]]:
        closes: List[float] = []
        for row in rows:
            try:
                value = float(row["close"])
            except (KeyError, TypeError, ValueError):
                return None
            if not math.isfinite(value) or value <= 0:
                return None
            closes.append(value)
        return closes

    @staticmethod
    def _setup_state(
        closes: List[float],
        ema50: List[float],
        trend: List[float],
        rsi: List[Optional[float]],
        atr: List[Optional[float]],
        bar: int,
    ) -> Optional[Dict[str, Any]]:
        """Evaluate the trend + ATR-stretch + RSI setup at an absolute bar index."""
        if bar < HISTORY_BARS - 1 or bar >= len(closes):
            return None

        close = closes[bar]
        ema50_value = ema50[bar]
        trend_value = trend[bar]
        rsi_value = rsi[bar]
        atr_value = atr[bar]
        if (ema50_value is None or ema50_value <= 0
                or trend_value is None or trend_value <= 0
                or rsi_value is None
                or atr_value is None or atr_value <= 0):
            return None

        stretch = STRETCH_ATR_MULT * atr_value
        pulled_back = abs(close - ema50_value) >= stretch
        return {
            "close": close,
            # ``sma200`` kept as an alias so existing callers/tests that only
            # read the trend level keep working; the trend is now a 200 EMA.
            "sma200": trend_value,
            "trend": trend_value,
            "ema50": ema50_value,
            "atr": atr_value,
            "stretch": stretch,
            "rsi": rsi_value,
            "pullback": pulled_back,
            "bull": (close > trend_value
                     and close <= ema50_value - stretch
                     and rsi_value < RSI_LONG_MAX),
            "bear": (close < trend_value
                     and close >= ema50_value + stretch
                     and rsi_value > RSI_SHORT_MIN),
        }

    @staticmethod
    def _atr_series(rows: List[Dict[str, Any]],
                    period: int = ATR_PERIOD) -> List[Optional[float]]:
        """Simple-average ATR aligned to ``rows`` (``None`` until enough data)."""
        out: List[Optional[float]] = [None] * len(rows)
        if period <= 0 or len(rows) < period:
            return out

        true_ranges: List[float] = []
        prev_close: Optional[float] = None
        for row in rows:
            try:
                high = float(row["high"])
                low = float(row["low"])
                close = float(row["close"])
            except (KeyError, TypeError, ValueError):
                return out
            if not (math.isfinite(high) and math.isfinite(low)
                    and math.isfinite(close)):
                return out
            if prev_close is None:
                true_range = high - low
            else:
                true_range = max(high - low,
                                 abs(high - prev_close),
                                 abs(low - prev_close))
            true_ranges.append(max(true_range, 0.0))
            prev_close = close

        for i in range(period - 1, len(true_ranges)):
            window = true_ranges[i - period + 1:i + 1]
            out[i] = sum(window) / period
        return out

    @staticmethod
    def _rsi_series(closes: List[float],
                    period: int = RSI_PERIOD) -> List[Optional[float]]:
        """Wilder's RSI aligned to ``closes`` (``None`` until enough history)."""
        out: List[Optional[float]] = [None] * len(closes)
        if period <= 0 or len(closes) < period + 1:
            return out

        avg_gain = 0.0
        avg_loss = 0.0
        for i in range(1, period + 1):
            delta = closes[i] - closes[i - 1]
            if delta > 0:
                avg_gain += delta
            else:
                avg_loss -= delta
        avg_gain /= period
        avg_loss /= period
        out[period] = (100.0 if avg_loss == 0
                       else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss))

        for i in range(period + 1, len(closes)):
            delta = closes[i] - closes[i - 1]
            gain = delta if delta > 0 else 0.0
            loss = -delta if delta < 0 else 0.0
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
            out[i] = (100.0 if avg_loss == 0
                      else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
        return out

    def _calculate_ema(self, data, period):
        ema = [data[0]]
        alpha = 2 / (period + 1)
        for val in data[1:]:
            ema.append(alpha * val + (1 - alpha) * ema[-1])
        return ema
