"""
TRIO — Screenshot Analyzer
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

Uses a vision model (OpenAI GPT-4o, Google Gemini, etc.) to extract trading
context from a market chart screenshot.

Output is treated as *hints* — always cross-checked against live data.

DISCLAIMER: Educational purposes only. Not financial advice.
"""

import base64
import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.utils import get_env, get_logger, load_config, retry_with_backoff, utc_now

logger = get_logger("screenshot_analyzer")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ScreenshotContext:
    """Structured output from vision model analysis of a chart screenshot.
    Author: Kapil Kuhire
    """
    symbol: str = ""
    timeframe: str = ""
    current_price: Optional[float] = None
    trend: str = "unknown"                      # bullish / bearish / sideways / unknown
    support_levels: List[float] = field(default_factory=list)
    resistance_levels: List[float] = field(default_factory=list)
    patterns_detected: List[str] = field(default_factory=list)
    visible_indicators: Dict[str, Any] = field(default_factory=dict)
    chart_notes: str = ""
    confidence: float = 0.0                     # 0.0 – 1.0
    extracted_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Vision prompt
# ---------------------------------------------------------------------------

ANALYSIS_PROMPT = """You are an expert technical analyst. Analyze this trading chart screenshot and return ONLY a valid JSON object with these fields:

{
  "symbol": "the ticker/symbol visible (e.g., RELIANCE, NIFTY, BTCUSD), or empty string if not visible",
  "timeframe": "the chart timeframe if visible (e.g., 1m, 5m, 15m, 1h, 1d), or empty string",
  "current_price": the latest price shown (number or null),
  "trend": "bullish" | "bearish" | "sideways" | "unknown",
  "support_levels": [list of key support price levels visible, up to 3],
  "resistance_levels": [list of key resistance price levels visible, up to 3],
  "patterns_detected": ["list of candlestick or chart patterns you recognize, e.g., double_bottom, head_and_shoulders, bullish_engulfing"],
  "visible_indicators": {
    "rsi": value or null,
    "macd_signal": "bullish_crossover" | "bearish_crossover" | "neutral" | null,
    "moving_averages": "description of any visible MAs and their positions"
  },
  "chart_notes": "2–3 sentence summary of what you see: price action, volume, key observations",
  "confidence": 0.0 to 1.0 — how confident you are in this analysis
}

Rules:
- Return ONLY the JSON, no markdown fences, no explanation.
- If you cannot determine a field, use null or empty.
- Price levels should be numbers, not strings.
- Be conservative with confidence if the image is blurry or cropped.
"""


# ---------------------------------------------------------------------------
# Image encoding
# ---------------------------------------------------------------------------

def encode_image_base64(image_path: str) -> str:
    """Read an image file and return its base64 encoding."""
    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _detect_mime(image_path: str) -> str:
    """Detect MIME type from file extension."""
    ext = Path(image_path).suffix.lower()
    mime_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }
    return mime_map.get(ext, "image/png")


# ---------------------------------------------------------------------------
# OpenAI GPT-4o backend
# ---------------------------------------------------------------------------

@retry_with_backoff(max_retries=3, backoff_base=2.0)
def _analyze_openai(image_path: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Send screenshot to OpenAI vision model and parse JSON response."""
    try:
        from openai import OpenAI
    except ImportError:
        raise ImportError("Install openai: pip install openai")

    api_key = get_env("OPENAI_API_KEY", required=True)
    client = OpenAI(api_key=api_key)

    model = config.get("openai_model", "gpt-4o")
    max_tokens = config.get("max_tokens", 1500)

    b64 = encode_image_base64(image_path)
    mime = _detect_mime(image_path)

    logger.info("Sending screenshot to OpenAI %s for analysis...", model)

    response = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": ANALYSIS_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime};base64,{b64}",
                            "detail": "high",
                        },
                    },
                ],
            }
        ],
    )

    raw = response.choices[0].message.content.strip()
    # Strip markdown fences if model included them anyway
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    return json.loads(raw)


# ---------------------------------------------------------------------------
# Google Gemini backend
# ---------------------------------------------------------------------------

@retry_with_backoff(max_retries=3, backoff_base=2.0)
def _analyze_gemini(image_path: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Send screenshot to Google Gemini and parse JSON response."""
    try:
        import google.generativeai as genai
    except ImportError:
        raise ImportError("Install google-generativeai: pip install google-generativeai")

    api_key = get_env("GOOGLE_GEMINI_API_KEY", required=True)
    genai.configure(api_key=api_key)

    model_name = config.get("gemini_model", "gemini-1.5-pro")
    model = genai.GenerativeModel(model_name)

    logger.info("Sending screenshot to Gemini %s for analysis...", model_name)

    import PIL.Image
    img = PIL.Image.open(image_path)

    response = model.generate_content([ANALYSIS_PROMPT, img])

    raw = response.text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    return json.loads(raw)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_screenshot(
    image_path: str,
    symbol_hint: Optional[str] = None,
    timeframe_hint: Optional[str] = None,
) -> ScreenshotContext:
    """
    Analyze a trading chart screenshot using a configured vision model.

    Args:
        image_path:     Path to the screenshot image file.
        symbol_hint:    Optional symbol override (if user already knows the ticker).
        timeframe_hint: Optional timeframe override.

    Returns:
        ScreenshotContext with extracted chart information.
    """
    cfg = load_config()
    sa_config = cfg.get("screenshot_analyzer", {})
    backend = sa_config.get("vision_model", "openai")

    backends = {
        "openai": _analyze_openai,
        "gemini": _analyze_gemini,
    }

    if backend not in backends:
        raise ValueError(f"Unsupported vision model: {backend}. Choose from {list(backends.keys())}")

    logger.info("Analyzing screenshot: %s (backend=%s)", image_path, backend)

    raw_result = backends[backend](image_path, sa_config)

    # Build structured context
    ctx = ScreenshotContext(
        symbol=symbol_hint or raw_result.get("symbol", ""),
        timeframe=timeframe_hint or raw_result.get("timeframe", ""),
        current_price=raw_result.get("current_price"),
        trend=raw_result.get("trend", "unknown"),
        support_levels=raw_result.get("support_levels", []),
        resistance_levels=raw_result.get("resistance_levels", []),
        patterns_detected=raw_result.get("patterns_detected", []),
        visible_indicators=raw_result.get("visible_indicators", {}),
        chart_notes=raw_result.get("chart_notes", ""),
        confidence=float(raw_result.get("confidence", 0.0)),
        extracted_at=utc_now(),
    )

    logger.info(
        "Screenshot analysis complete: symbol=%s trend=%s price=%s confidence=%.2f",
        ctx.symbol, ctx.trend, ctx.current_price, ctx.confidence,
    )

    return ctx
