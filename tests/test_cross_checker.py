"""
TRIO — Cross Checker Tests
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.cross_checker import cross_check, CrossCheckResult
from src.screenshot_analyzer import ScreenshotContext
from src.indicators import TechnicalReadings, IndicatorReading


def _tr(support=None, resistance=None):
    support = support or []
    resistance = resistance or []
    return TechnicalReadings(
        summary={"overall": "bullish", "bullish_count": 3, "bearish_count": 0, "neutral_count": 1},
        indicators={
            "rsi_14": IndicatorReading(value=62.0, signal="neutral"),
            "macd": IndicatorReading(value=1.2, signal="bullish"),
        },
        support=support,
        resistance=resistance,
    )


def test_cross_check_price_match():
    ctx = ScreenshotContext(symbol="RELIANCE", current_price=2850.5, trend="bullish")
    tr = _tr()
    res = cross_check(ctx, live_price=2850.5, technical_readings=tr)
    assert isinstance(res, CrossCheckResult)
    assert res.price_match is True
    assert res.trust_score >= 0.7


def test_cross_check_price_mismatch_warns():
    ctx = ScreenshotContext(symbol="RELIANCE", current_price=3000.0, trend="bullish")
    tr = _tr()
    res = cross_check(ctx, live_price=2850.0, technical_readings=tr)
    assert res.price_match is False
    assert len(res.warnings) > 0
    assert res.trust_score < 1.0


def test_trend_mismatch_penalizes_trust():
    ctx = ScreenshotContext(symbol="RELIANCE", current_price=2850.0, trend="bearish")
    tr = _tr()
    res = cross_check(ctx, live_price=2850.0, technical_readings=tr)
    assert res.trend_match is False
    assert res.trust_score < 0.9


def test_rsi_spot_check():
    ctx = ScreenshotContext(symbol="RELIANCE", current_price=2850.0, trend="bullish",
                            visible_indicators={"rsi": 60.0})
    tr = _tr()
    res = cross_check(ctx, live_price=2850.0, technical_readings=tr)
    assert "rsi" in res.indicator_matches
    assert res.indicator_matches["rsi"]["match"] is True


def test_macd_spot_check():
    ctx = ScreenshotContext(symbol="RELIANCE", current_price=2850.0, trend="bullish",
                            visible_indicators={"macd_signal": "bullish_crossover"})
    tr = _tr()
    res = cross_check(ctx, live_price=2850.0, technical_readings=tr)
    assert "macd_signal" in res.indicator_matches
    assert res.indicator_matches["macd_signal"]["match"] is True


def test_trust_score_clamped():
    ctx = ScreenshotContext(symbol="RELIANCE", current_price=10000.0, trend="bearish",
                            visible_indicators={"rsi": 10, "macd_signal": "bearish"})
    tr = _tr()
    res = cross_check(ctx, live_price=100.0, technical_readings=tr)
    assert 0.0 <= res.trust_score <= 1.0
