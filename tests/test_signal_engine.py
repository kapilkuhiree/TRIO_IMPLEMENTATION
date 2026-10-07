"""
TRIO — Unit tests for the signal engine.
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

These tests use config_override so they do not depend on config.yaml weights.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.indicators import IndicatorReading, TechnicalReadings
from src.signal_engine import generate_signal


def _readings(signals: dict, pullback: bool = False) -> TechnicalReadings:
    """Build TechnicalReadings from a name -> signal map."""
    indicators = {}
    for name, sig in signals.items():
        # MACD needs a value + signal_line so momentum checks behave.
        if name == "macd":
            val = 1.0 if sig == "bullish" else (-1.0 if sig == "bearish" else 0.0)
            indicators[name] = IndicatorReading(value=val, signal=sig,
                                                extra={"signal_line": 0.0})
        else:
            indicators[name] = IndicatorReading(value=1.0, signal=sig)
    bullish = sum(1 for s in signals.values() if s == "bullish")
    bearish = sum(1 for s in signals.values() if s == "bearish")
    neutral = sum(1 for s in signals.values() if s == "neutral")
    overall = "neutral"
    if bullish > bearish:
        overall = "bullish"
    elif bearish > bullish:
        overall = "bearish"
    return TechnicalReadings(
        symbol="TEST",
        timeframe="15m",
        indicators=indicators,
        summary={
            "bullish_count": bullish,
            "bearish_count": bearish,
            "neutral_count": neutral,
            "overall": overall,
        },
        pullback_detected=pullback,
    )


ENGINE_CFG = {
    "weights": {"technical": 1.0, "sentiment": 0.0, "screenshot_chart": 0.0},
    "min_confirmations": 3,
    "conflict_threshold": 0.4,
    "buy_threshold": 0.3,
    "sell_threshold": -0.3,
    "entry_strategy": "signals",  # tests exercise the raw composite logic
    "trading": {"allow_shorts": True},
}


class _Sentiment:
    def __init__(self, score: float, label: str = "neutral"):
        self.score = score
        self.label = label
        self.sources = ["news"]


def test_buy_when_technicals_confirm():
    readings = _readings({
        "sma_9": "bullish",
        "ema_9": "bullish",
        "macd": "bullish",
        "rsi_14": "bullish",
        "vwap": "neutral",
    })
    signal = generate_signal(
        symbol="TEST",
        latest_price=100.0,
        technical_readings=readings,
        config_override=ENGINE_CFG,
    )
    assert signal.action == "BUY"
    assert signal.entry_price == 100.0
    assert signal.confidence > 50
    assert signal.composite_score > 0


def test_sell_when_technicals_confirm():
    readings = _readings({
        "sma_9": "bearish",
        "ema_9": "bearish",
        "macd": "bearish",
        "rsi_14": "bearish",
    })
    signal = generate_signal(
        symbol="TEST",
        latest_price=80.0,
        technical_readings=readings,
        config_override=ENGINE_CFG,
    )
    assert signal.action == "SELL"
    assert signal.composite_score < 0


def test_hold_when_not_enough_confirmations():
    readings = _readings({
        "sma_9": "bullish",
        "ema_9": "neutral",
        "macd": "neutral",
        "rsi_14": "bearish",
    })
    signal = generate_signal(
        symbol="TEST",
        latest_price=50.0,
        technical_readings=readings,
        config_override=ENGINE_CFG,
    )
    assert signal.action == "HOLD"


def test_hold_when_no_data():
    signal = generate_signal(symbol="TEST", latest_price=10.0, config_override=ENGINE_CFG)
    assert signal.action == "HOLD"
    assert signal.confidence == 0
    assert any("No data" in r for r in signal.reasoning)


def test_conflict_blocks_buy():
    readings = _readings({
        "sma_9": "bullish",
        "ema_9": "bullish",
        "macd": "bullish",
        "rsi_14": "bullish",
    })
    cfg = dict(ENGINE_CFG)
    cfg["weights"] = {"technical": 0.6, "sentiment": 0.4, "screenshot_chart": 0.0}
    signal = generate_signal(
        symbol="TEST",
        latest_price=100.0,
        technical_readings=readings,
        sentiment_result=_Sentiment(-0.8, "bearish"),
        config_override=cfg,
    )
    assert signal.action == "HOLD"
    assert any("CONFLICT" in r for r in signal.reasoning)


def test_to_dict():
    signal = generate_signal(symbol="TEST", latest_price=1.0, config_override=ENGINE_CFG)
    data = signal.to_dict()
    assert data["symbol"] == "TEST"
    assert data["action"] == "HOLD"
    assert "disclaimer" in data


# ---------------------------------------------------------------------------
# Pullback entry gate (the validated strategy)
# ---------------------------------------------------------------------------

PULLBACK_CFG = {
    **ENGINE_CFG,
    "entry_strategy": "pullback",
    "trading": {"allow_shorts": False},
}


def _pullback_readings() -> TechnicalReadings:
    return _readings({
        "sma_9": "bullish",
        "ema_9": "bullish",
        "macd": "bullish",
        "rsi_14": "bullish",
    }, pullback=True)


def _no_pullback_readings() -> TechnicalReadings:
    return _readings({
        "sma_9": "bullish",
        "ema_9": "bullish",
        "macd": "bullish",
        "rsi_14": "bullish",
    }, pullback=False)


def test_pullback_gate_allows_long_when_setup_present():
    signal = generate_signal(
        symbol="TEST",
        latest_price=100.0,
        technical_readings=_pullback_readings(),
        config_override=PULLBACK_CFG,
    )
    assert signal.action == "BUY"


def test_pullback_gate_blocks_long_without_setup():
    signal = generate_signal(
        symbol="TEST",
        latest_price=100.0,
        technical_readings=_no_pullback_readings(),
        config_override=PULLBACK_CFG,
    )
    assert signal.action == "HOLD"
    assert any("NO PULLBACK SETUP" in r for r in signal.reasoning)


def test_signals_mode_ignores_pullback_gate():
    """Legacy mode keeps the original composite behaviour for compatibility."""
    signal = generate_signal(
        symbol="TEST",
        latest_price=100.0,
        technical_readings=_no_pullback_readings(),
        config_override=ENGINE_CFG,
    )
    assert signal.action == "BUY"


def test_short_blocked_when_macd_turned_up():
    """SELL needs MACD below its signal line, not just a negative composite."""
    signal = generate_signal(
        symbol="TEST",
        latest_price=80.0,
        technical_readings=_readings({
            "sma_9": "bearish",
            "ema_9": "bearish",
            "macd": "bullish",   # momentum turned up: value +1 > signal 0
            "rsi_14": "bearish",
        }),
        config_override=PULLBACK_CFG,
    )
    assert signal.action == "HOLD"
    assert any("SHORT BLOCKED" in r for r in signal.reasoning)

def test_short_allowed_when_macd_confirms():
    """Shorts exist only in signals mode (MIS-style accounts)."""
    cfg = dict(PULLBACK_CFG)
    cfg["entry_strategy"] = "signals"
    cfg["trading"] = {"allow_shorts": True}
    
    signal = generate_signal(
        symbol="TEST",
        latest_price=80.0,
        technical_readings=_readings({
            "sma_9": "bearish",
            "ema_9": "bearish",
            "macd": "bearish",   # value -1 < signal 0: momentum down
            "rsi_14": "bearish",
        }),
        config_override=cfg,
    )
    assert signal.action == "SELL"


def test_pullback_mode_never_shorts():
    """Long-only default: even a strongly bearish composite is HOLD."""
    import src.signal_engine as _se
    _real_load = _se.load_config
    _se.load_config = lambda: {"trading": {"allow_shorts": False}}
    try:
        signal = generate_signal(
            symbol="TEST",
            latest_price=80.0,
            technical_readings=_readings({
                "sma_9": "bearish",
                "ema_9": "bearish",
                "macd": "bearish",
                "rsi_14": "bearish",
            }),
            config_override=PULLBACK_CFG,
        )
    finally:
        _se.load_config = _real_load
    assert signal.action == "HOLD"
