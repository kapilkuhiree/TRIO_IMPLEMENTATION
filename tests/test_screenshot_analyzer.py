"""
TRIO — Tests for Screenshot Analyzer
Author: Kapil Kuhire <kapilkuhire89@gmail.com>
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.screenshot_analyzer import (
    ScreenshotContext,
    encode_image_base64,
    _detect_mime,
    analyze_screenshot,
)


class _DummyResp:
    class Choice:
        class Message:
            def __init__(self, content: str):
                self.content = content
        def __init__(self, content: str):
            self.message = self.Message(content)
    def __init__(self, content: str):
        self.choices = [self.Choice(content)]


def test_detect_mime():
    assert _detect_mime("test.png") == "image/png"
    assert _detect_mime("test.JPG") == "image/jpeg"
    assert _detect_mime("test.webp") == "image/webp"
    assert _detect_mime("test.unknown") == "image/png"


def test_screenshot_context_to_dict():
    ctx = ScreenshotContext(symbol="RELIANCE", trend="bullish", current_price=2800.0, confidence=0.8)
    d = ctx.to_dict()
    assert d["symbol"] == "RELIANCE"
    assert d["trend"] == "bullish"
    assert d["confidence"] == 0.8


@patch("src.screenshot_analyzer.get_env", return_value="test-key")
@patch("src.screenshot_analyzer.load_config")
@patch("src.screenshot_analyzer.encode_image_base64", return_value="xxx")
@patch("src.screenshot_analyzer.Path.exists", return_value=True)
@patch("openai.OpenAI")
def test_analyze_screenshot_openai_mock(mock_openai, mock_exists, mock_enc, mock_load_config, mock_env):
    mock_load_config.return_value = {"screenshot_analyzer": {"vision_model": "openai", "openai_model": "gpt-4o"}}
    mock_client = mock_openai.return_value
    mock_client.chat.completions.create.return_value = _DummyResp(
        """{
          "symbol": "RELIANCE",
          "timeframe": "15m",
          "current_price": 2850.5,
          "trend": "bullish",
          "support_levels": [2800, 2780],
          "resistance_levels": [2880],
          "patterns_detected": ["bullish_engulfing"],
          "visible_indicators": {"rsi": 62},
          "chart_notes": "Price above key MA",
          "confidence": 0.85
        }"""
    )
    ctx = analyze_screenshot("fake.png")
    assert ctx.symbol == "RELIANCE"
    assert ctx.trend == "bullish"
    assert ctx.current_price == 2850.5
    assert ctx.confidence == 0.85


@patch("src.screenshot_analyzer.load_config")
@patch("src.screenshot_analyzer.Path.exists", return_value=True)
def test_analyze_screenshot_missing_backend(mock_exists, mock_load_config):
    mock_load_config.return_value = {"screenshot_analyzer": {"vision_model": "unknown"}}
    try:
        analyze_screenshot("fake.png")
        assert False
    except ValueError as e:
        assert "Unsupported vision model" in str(e)
