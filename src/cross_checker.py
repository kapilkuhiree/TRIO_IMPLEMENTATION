"""
TRIO — Cross Checker
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

Verifies that values extracted from a screenshot (price, trend, indicators)
match live market data. Produces a trust score.

The principle: screenshot data is a *hint*, live data is *truth*.

DISCLAIMER: Educational purposes only. Not financial advice.
"""

import logging
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from src.utils import get_logger, load_config

logger = get_logger("cross_checker")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CrossCheckResult:
    """Result of cross-checking screenshot context against live data.
    Author: Kapil Kuhire
    """
    symbol: str = ""
    price_match: bool = True
    price_deviation_pct: float = 0.0
    trend_match: bool = True
    indicator_matches: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    trust_score: float = 1.0   # 0.0 – 1.0
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def cross_check(
    screenshot_ctx: Any,
    live_price: Optional[float],
    technical_readings: Any,
) -> CrossCheckResult:
    """
    Cross-check screenshot-extracted values against live market data.

    Args:
        screenshot_ctx:    ScreenshotContext from the vision model.
        live_price:        Latest price from data_fetcher.
        technical_readings: TechnicalReadings from indicators module.

    Returns:
        CrossCheckResult with trust score and warnings.
    """
    cfg = load_config()
    threshold = cfg.get("screenshot_analyzer", {}).get("price_deviation_threshold", 1.0)

    result = CrossCheckResult(symbol=screenshot_ctx.symbol)
    warnings: List[str] = []
    trust = 1.0

    # ----- Price check -----
    ss_price = screenshot_ctx.current_price
    if ss_price is not None and live_price is not None and live_price != 0:
        dev = abs(ss_price - live_price) / live_price * 100
        result.price_deviation_pct = round(dev, 3)

        if dev > threshold:
            result.price_match = False
            trust -= 0.3
            warnings.append(
                f"Price mismatch: screenshot={ss_price}, live={live_price} "
                f"(deviation {dev:.2f}% > threshold {threshold}%)"
            )
            logger.warning(warnings[-1])
        else:
            result.price_match = True
    elif ss_price is None:
        trust -= 0.1
        warnings.append("Screenshot did not detect a price — using live data only")
    elif live_price is None:
        trust -= 0.2
        warnings.append("Could not fetch live price — using screenshot price as fallback")

    # ----- Trend check -----
    ss_trend = screenshot_ctx.trend
    live_trend = technical_readings.summary.get("overall", "unknown") if technical_readings else "unknown"

    if ss_trend != "unknown" and live_trend != "unknown":
        if ss_trend == live_trend:
            result.trend_match = True
        else:
            result.trend_match = False
            trust -= 0.2
            warnings.append(
                f"Trend mismatch: screenshot={ss_trend}, live indicators={live_trend}"
            )
    else:
        # Can't compare
        trust -= 0.05

    # ----- Indicator spot checks -----
    vis = screenshot_ctx.visible_indicators or {}
    live_ind = technical_readings.indicators if technical_readings else {}

    # RSI
    ss_rsi = vis.get("rsi")
    if ss_rsi is not None:
        # Find the live RSI indicator (any key starting with rsi_)
        for key, reading in live_ind.items():
            if key.startswith("rsi_"):
                live_rsi = reading.value
                match = live_rsi is not None and abs(ss_rsi - live_rsi) < 5
                result.indicator_matches["rsi"] = {
                    "screenshot": ss_rsi,
                    "live": live_rsi,
                    "match": match,
                }
                if not match:
                    trust -= 0.1
                    warnings.append(f"RSI mismatch: screenshot={ss_rsi}, live={live_rsi}")
                break

    # MACD signal direction
    ss_macd = vis.get("macd_signal")
    if ss_macd and "macd" in live_ind:
        live_macd_signal = live_ind["macd"].signal
        match = ss_macd.replace("_crossover", "") == live_macd_signal.replace("_crossover", "")
        result.indicator_matches["macd_signal"] = {
            "screenshot": ss_macd,
            "live": live_macd_signal,
            "match": match,
        }
        if not match:
            trust -= 0.1
            warnings.append(f"MACD signal mismatch: screenshot={ss_macd}, live={live_macd_signal}")

    # Clamp trust
    trust = max(0.0, min(1.0, trust))
    result.trust_score = round(trust, 3)
    result.warnings = warnings

    logger.info(
        "Cross-check %s: trust=%.2f, price_match=%s, trend_match=%s, warnings=%d",
        result.symbol, trust, result.price_match, result.trend_match, len(warnings),
    )

    return result
