"""
TRIO — Unit tests for indicators module.
Author: Kapil Kuhire <kapilkuhire89@gmail.com>
"""

import numpy as np
import pandas as pd
import pytest

# Add project root to path
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.indicators import (
    calc_sma,
    calc_ema,
    calc_macd,
    calc_rsi,
    calc_stochastic,
    calc_bollinger,
    calc_atr,
    calc_vwap,
    calc_obv,
    detect_support_resistance,
    compute_indicators,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_df() -> pd.DataFrame:
    """Create a sample OHLCV DataFrame with 100 bars of synthetic data."""
    np.random.seed(42)
    n = 100
    base_price = 100.0
    prices = base_price + np.cumsum(np.random.randn(n) * 0.5)

    df = pd.DataFrame({
        "Open":   prices + np.random.randn(n) * 0.2,
        "High":   prices + abs(np.random.randn(n) * 0.5),
        "Low":    prices - abs(np.random.randn(n) * 0.5),
        "Close":  prices,
        "Volume": np.random.randint(10000, 500000, n).astype(float),
    })

    # Ensure High >= Open, Close and Low <= Open, Close
    df["High"] = df[["Open", "High", "Close"]].max(axis=1) + 0.01
    df["Low"] = df[["Open", "Low", "Close"]].min(axis=1) - 0.01

    return df


@pytest.fixture
def trending_up_df() -> pd.DataFrame:
    """Create data with a clear uptrend."""
    n = 100
    prices = np.linspace(100, 150, n) + np.random.randn(n) * 0.3
    df = pd.DataFrame({
        "Open":   prices - 0.5,
        "High":   prices + 1.0,
        "Low":    prices - 1.0,
        "Close":  prices,
        "Volume": np.linspace(100000, 200000, n),
    })
    return df


@pytest.fixture
def trending_down_df() -> pd.DataFrame:
    """Create data with a clear downtrend."""
    n = 100
    prices = np.linspace(150, 100, n) + np.random.randn(n) * 0.3
    df = pd.DataFrame({
        "Open":   prices + 0.5,
        "High":   prices + 1.0,
        "Low":    prices - 1.0,
        "Close":  prices,
        "Volume": np.linspace(200000, 100000, n),
    })
    return df


# ---------------------------------------------------------------------------
# SMA Tests
# ---------------------------------------------------------------------------

class TestSMA:
    def test_sma_value(self, sample_df: pd.DataFrame):
        result = calc_sma(sample_df, 9)
        assert result.value is not None
        assert isinstance(result.value, float)

    def test_sma_signal_bullish(self, trending_up_df: pd.DataFrame):
        result = calc_sma(trending_up_df, 9)
        assert result.signal == "bullish"

    def test_sma_signal_bearish(self, trending_down_df: pd.DataFrame):
        result = calc_sma(trending_down_df, 9)
        assert result.signal == "bearish"

    def test_sma_with_short_data(self):
        df = pd.DataFrame({"Close": [1.0, 2.0, 3.0]})
        result = calc_sma(df, 50)
        assert result.value is None  # not enough data


# ---------------------------------------------------------------------------
# EMA Tests
# ---------------------------------------------------------------------------

class TestEMA:
    def test_ema_value(self, sample_df: pd.DataFrame):
        result = calc_ema(sample_df, 21)
        assert result.value is not None

    def test_ema_bullish_in_uptrend(self, trending_up_df: pd.DataFrame):
        result = calc_ema(trending_up_df, 9)
        assert result.signal == "bullish"


# ---------------------------------------------------------------------------
# MACD Tests
# ---------------------------------------------------------------------------

class TestMACD:
    def test_macd_returns_values(self, sample_df: pd.DataFrame):
        result = calc_macd(sample_df)
        assert result.value is not None
        assert "signal_line" in result.extra
        assert "histogram" in result.extra

    def test_macd_signal_is_valid(self, sample_df: pd.DataFrame):
        result = calc_macd(sample_df)
        assert result.signal in ("bullish", "bearish", "neutral")


# ---------------------------------------------------------------------------
# RSI Tests
# ---------------------------------------------------------------------------

class TestRSI:
    def test_rsi_range(self, sample_df: pd.DataFrame):
        result = calc_rsi(sample_df, 14)
        assert result.value is not None
        assert 0 <= result.value <= 100

    def test_rsi_overbought_signal(self):
        # Create data that should produce high RSI
        n = 50
        prices = np.linspace(100, 200, n)  # steady rise
        df = pd.DataFrame({"Close": prices})
        result = calc_rsi(df, 14, overbought=70, oversold=30)
        assert result.value is not None
        # In a strong uptrend, RSI should be high
        assert result.value > 50

    def test_rsi_oversold_signal(self):
        n = 50
        prices = np.linspace(200, 100, n)  # steady fall
        df = pd.DataFrame({"Close": prices})
        result = calc_rsi(df, 14, overbought=70, oversold=30)
        assert result.value is not None
        assert result.value < 50


# ---------------------------------------------------------------------------
# Stochastic Tests
# ---------------------------------------------------------------------------

class TestStochastic:
    def test_stochastic_range(self, sample_df: pd.DataFrame):
        result = calc_stochastic(sample_df)
        assert result.value is not None
        assert 0 <= result.value <= 100
        assert "k" in result.extra
        assert "d" in result.extra


# ---------------------------------------------------------------------------
# Bollinger Bands Tests
# ---------------------------------------------------------------------------

class TestBollinger:
    def test_bollinger_bands_structure(self, sample_df: pd.DataFrame):
        result = calc_bollinger(sample_df, period=20)
        assert result.extra.get("upper") is not None
        assert result.extra.get("middle") is not None
        assert result.extra.get("lower") is not None
        assert result.extra["upper"] > result.extra["middle"] > result.extra["lower"]


# ---------------------------------------------------------------------------
# ATR Tests
# ---------------------------------------------------------------------------

class TestATR:
    def test_atr_positive(self, sample_df: pd.DataFrame):
        result = calc_atr(sample_df, 14)
        assert result.value is not None
        assert result.value > 0
        assert result.signal == "info"  # ATR has no directional signal


# ---------------------------------------------------------------------------
# VWAP Tests
# ---------------------------------------------------------------------------

class TestVWAP:
    def test_vwap_value(self, sample_df: pd.DataFrame):
        result = calc_vwap(sample_df)
        assert result.value is not None
        assert result.signal in ("bullish", "bearish", "neutral")


# ---------------------------------------------------------------------------
# OBV Tests
# ---------------------------------------------------------------------------

class TestOBV:
    def test_obv_value(self, sample_df: pd.DataFrame):
        result = calc_obv(sample_df)
        assert result.value is not None
        assert "trend" in result.extra

    def test_obv_rising_in_uptrend(self, trending_up_df: pd.DataFrame):
        result = calc_obv(trending_up_df)
        assert result.extra["trend"] == "rising"
        assert result.signal == "bullish"


# ---------------------------------------------------------------------------
# Support & Resistance Tests
# ---------------------------------------------------------------------------

class TestSupportResistance:
    def test_detect_levels(self, sample_df: pd.DataFrame):
        support, resistance = detect_support_resistance(sample_df, lookback=5)
        # May or may not find levels, but should not crash
        assert isinstance(support, list)
        assert isinstance(resistance, list)

    def test_not_enough_data(self):
        df = pd.DataFrame({
            "High": [10.0, 11.0],
            "Low": [9.0, 10.0],
        })
        support, resistance = detect_support_resistance(df, lookback=5)
        assert support == []
        assert resistance == []


# ---------------------------------------------------------------------------
# Full compute_indicators Tests
# ---------------------------------------------------------------------------

class TestComputeIndicators:
    def test_compute_all(self, sample_df: pd.DataFrame):
        readings = compute_indicators(sample_df, symbol="TEST", timeframe="15m")

        assert readings.symbol == "TEST"
        assert readings.timeframe == "15m"
        assert len(readings.indicators) > 0
        assert "bullish_count" in readings.summary
        assert "bearish_count" in readings.summary
        assert readings.summary["overall"] in ("bullish", "bearish", "neutral")

    def test_insufficient_data(self):
        df = pd.DataFrame({
            "Open": [1.0, 2.0],
            "High": [1.5, 2.5],
            "Low": [0.5, 1.5],
            "Close": [1.2, 2.2],
            "Volume": [100, 200],
        })
        with pytest.raises(ValueError, match="at least 5 rows"):
            compute_indicators(df)

    def test_to_dict(self, sample_df: pd.DataFrame):
        readings = compute_indicators(sample_df, symbol="TEST")
        d = readings.to_dict()
        assert isinstance(d, dict)
        assert "indicators" in d
        assert "summary" in d
