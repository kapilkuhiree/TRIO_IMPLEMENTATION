"""Behavioral tests for the repaired mean-reversion / pullback strategy."""

import math

import pytest

from strategies.mean_reversion_strategy import (
    ATR_PERIOD,
    EMA_PERIOD,
    HISTORY_BARS,
    RSI_PERIOD,
    STRETCH_ATR_MULT,
    TREND_EMA_PERIOD,
    MeanReversionStrategy,
)


def _series(n, base=24000.0, slope=4.0, amplitude=150.0, period=25.0):
    """Deterministic 5-minute closes with a drift plus a slow sine wave.

    The wave makes price stretch away from the 50-EMA while RSI oscillates
    around 50, which is exactly the pullback regime the strategy trades.
    """
    rows = []
    for i in range(n):
        close = base + slope * i
        if period:
            close += amplitude * math.sin(i / period)
        rows.append({
            "timestamp": f"2026-09-{(i // 75) + 1:02d}T{9 + (i % 75) // 12:02d}:"
                         f"{((i % 75) % 12) * 5:02d}:00+05:30",
            "open": close,
            "high": close * 1.0005,
            "low": close * 0.9995,
            "close": close,
            "volume": 0.0,
        })
    return rows


def _state(strategy, candles, index):
    window = candles[:index + 1]
    closes = strategy._closes(window)
    ema50 = strategy._calculate_ema(closes, EMA_PERIOD)
    trend = strategy._calculate_ema(closes, TREND_EMA_PERIOD)
    rsi = strategy._rsi_series(closes, RSI_PERIOD)
    atr = strategy._atr_series(window, ATR_PERIOD)
    return strategy._setup_state(
        closes, ema50, trend, rsi, atr, len(closes) - 1)


def _first_event(strategy, candles, key, start=HISTORY_BARS + 20):
    """First bar where ``key`` turns true: the live strategy's fresh trigger."""
    previous = False
    for i in range(start, len(candles)):
        state = _state(strategy, candles, i)
        if state is None:
            continue
        current = bool(state[key])
        if current and not previous:
            return i
        previous = current
    return None


def test_requires_a_full_two_hundred_bar_history():
    strategy = MeanReversionStrategy()
    candles = _series(HISTORY_BARS - 1)

    assert strategy.evaluate_signal(candles, len(candles) - 1).allowed is False


def test_bullish_atr_stretch_with_oversold_rsi_emits_a_call():
    strategy = MeanReversionStrategy()
    candles = _series(500, slope=4.0, amplitude=400.0, period=80.0)
    index = _first_event(strategy, candles, "bull")
    assert index is not None

    state = _state(strategy, candles, index)
    assert state["bull"] is True
    assert state["rsi"] < 40
    assert state["close"] <= state["ema50"] - state["stretch"]

    signal = strategy.evaluate_signal(candles, index)
    assert signal.allowed is True
    assert signal.variant == "bullish"
    assert signal.option_type == "CE"
    assert signal.entry_price == pytest.approx(state["close"])
    assert signal.stop_loss < signal.entry_price < signal.target
    assert "RSI" in signal.reasoning


def test_bearish_atr_stretch_with_overbought_rsi_emits_a_put():
    strategy = MeanReversionStrategy()
    candles = _series(500, slope=-4.0, amplitude=300.0, period=60.0)
    index = _first_event(strategy, candles, "bear")
    assert index is not None

    state = _state(strategy, candles, index)
    assert state["bear"] is True
    assert state["rsi"] > 60
    assert state["close"] >= state["ema50"] + state["stretch"]

    signal = strategy.evaluate_signal(candles, index)
    assert signal.allowed is True
    assert signal.variant == "bearish"
    assert signal.option_type == "PE"
    assert signal.stop_loss > signal.entry_price > signal.target
    assert "RSI" in signal.reasoning


def test_a_persistent_setup_is_signalled_once_not_on_every_bar():
    """The planner only sees the latest bar; the trigger must still be an event."""
    strategy = MeanReversionStrategy()
    candles = _series(500, slope=4.0, amplitude=400.0, period=80.0)
    index = _first_event(strategy, candles, "bull")
    assert index is not None
    assert strategy.evaluate_signal(candles, index).allowed is True

    # Find a following bar inside the same persistent setup.
    follow_up = next(
        (j for j in range(index + 1, len(candles))
         if (_state(strategy, candles, j) or {}).get("bull")),
        None,
    )
    assert follow_up is not None
    assert strategy.evaluate_signal(candles, follow_up).allowed is False


def test_one_way_grind_does_not_signal():
    """A steady trend that never stretches below the 50 EMA is not a pullback."""
    strategy = MeanReversionStrategy()
    candles = _series(400, slope=4.0, amplitude=0.0, period=0)

    state = _state(strategy, candles, 300)
    assert state["bull"] is False
    assert state["bear"] is False
    assert strategy.evaluate_signal(candles, 300).allowed is False


def test_stretch_is_scaled_by_atr():
    strategy = MeanReversionStrategy()
    candles = _series(500, slope=4.0, amplitude=400.0, period=80.0)
    index = _first_event(strategy, candles, "bull")
    assert index is not None

    state = _state(strategy, candles, index)
    assert state["atr"] > 0
    assert state["stretch"] == pytest.approx(STRETCH_ATR_MULT * state["atr"])


def test_atr_series_uses_true_range_gaps():
    strategy = MeanReversionStrategy()
    rows = []
    for i in range(20):
        close = 100.0 + i
        rows.append({"high": close + 2.0, "low": close - 2.0, "close": close})

    atr = strategy._atr_series(rows, ATR_PERIOD)
    assert atr[ATR_PERIOD - 2] is None
    # True range here is always 4 (the gap adds nothing).
    assert atr[-1] == pytest.approx(4.0)


def test_wilder_rsi_is_one_hundred_without_losses():
    strategy = MeanReversionStrategy()
    closes = [100.0 + i for i in range(60)]

    assert strategy._rsi_series(closes, 14)[-1] == pytest.approx(100.0)


def test_malformed_candles_fail_closed():
    strategy = MeanReversionStrategy()
    candles = _series(HISTORY_BARS + 10)
    candles[-1] = {"close": "not-a-number"}

    assert strategy.evaluate_signal(candles, len(candles) - 1).allowed is False
