"""
TRIO — Signal Engine
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

Combines technical indicator readings, sentiment scores, and screenshot
chart analysis into a weighted composite signal: BUY / SELL / HOLD with
a confidence level (0–100%).

Requires confirmation from multiple indicators and blocks signals when
technicals and sentiment strongly conflict.

DISCLAIMER: Educational purposes only. Not financial advice.
"""

import logging
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from src.utils import get_logger, load_config, utc_now

logger = get_logger("signal_engine")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TradeSignal:
    """Generated trade signal with full reasoning.
    Author: Kapil Kuhire
    """
    symbol: str = ""
    action: str = "HOLD"              # BUY / SELL / HOLD
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    trailing_stop: bool = False
    position_size: int = 0
    risk_amount: float = 0.0
    risk_reward_ratio: str = ""
    confidence: int = 0               # 0 – 100
    composite_score: float = 0.0      # raw weighted score
    reasoning: List[str] = field(default_factory=list)
    risk_check: Dict[str, Any] = field(default_factory=dict)
    generated_at: str = ""
    disclaimer: str = "Educational only. Not financial advice."

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Score computation
# ---------------------------------------------------------------------------

def _technical_score(technical_readings: Any) -> float:
    """
    Convert technical readings summary to a score from -1 to +1.

    Score = (bullish_count - bearish_count) / total_directional_indicators
    """
    if technical_readings is None:
        return 0.0

    summary = technical_readings.summary
    bullish = summary.get("bullish_count", 0)
    bearish = summary.get("bearish_count", 0)
    total = bullish + bearish

    if total == 0:
        return 0.0

    return round((bullish - bearish) / total, 4)


def _chart_score(screenshot_ctx: Any, cross_check_result: Any) -> float:
    """
    Convert screenshot chart analysis to a score from -1 to +1,
    weighted by the cross-check trust score.
    """
    if screenshot_ctx is None:
        return 0.0

    trend = screenshot_ctx.trend
    confidence = screenshot_ctx.confidence
    trust = cross_check_result.trust_score if cross_check_result else 0.5

    trend_map = {
        "bullish": 1.0,
        "bearish": -1.0,
        "sideways": 0.0,
        "unknown": 0.0,
    }

    raw = trend_map.get(trend, 0.0) * confidence

    # Patterns boost
    patterns = screenshot_ctx.patterns_detected or []
    bullish_patterns = ["bullish_engulfing", "double_bottom", "morning_star",
                        "hammer", "inverse_head_and_shoulders", "bullish_harami"]
    bearish_patterns = ["bearish_engulfing", "double_top", "evening_star",
                        "shooting_star", "head_and_shoulders", "bearish_harami"]

    pattern_boost = 0.0
    for p in patterns:
        p_lower = p.lower().replace(" ", "_")
        if any(bp in p_lower for bp in bullish_patterns):
            pattern_boost += 0.1
        elif any(bp in p_lower for bp in bearish_patterns):
            pattern_boost -= 0.1

    raw += pattern_boost

    # Apply trust weighting
    return round(max(-1.0, min(1.0, raw * trust)), 4)


def _sentiment_score(sentiment_result: Any) -> float:
    """Extract sentiment score (already -1 to +1)."""
    if sentiment_result is None:
        return 0.0
    return sentiment_result.score


def _macd_confirms_short(technical_readings: Any) -> bool:
    """Shorts need MACD line below its signal line.

    Rationale: the composite can go negative on stale moving-average votes
    while momentum has already turned (MACD crossed up). Shorting into that
    turn is shorting a bottom. The 2026-10-05 morning scan showed exactly
    this on HDFCBANK (composite +0.27, macd bullish) and KOTAKBANK
    (11 bullish, macd bullish) — both printed SELL without this check.
    """
    if technical_readings is None:
        return False
    macd = technical_readings.indicators.get("macd")
    if macd is None or macd.value is None:
        return False
    sig = macd.extra.get("signal_line")
    if sig is None:
        return True  # no signal line available; fall back to composite only
    return macd.value < sig


# ---------------------------------------------------------------------------
# Confirmation logic
# ---------------------------------------------------------------------------

def _count_confirmations(technical_readings: Any, direction: str) -> int:
    """Count how many indicators confirm the given direction."""
    if technical_readings is None:
        return 0

    count = 0
    for name, reading in technical_readings.indicators.items():
        if reading.signal == direction:
            count += 1
    return count


def _build_reasoning(
    technical_readings: Any,
    sentiment_result: Any,
    screenshot_ctx: Any,
    cross_check_result: Any,
    tech_score: float,
    sent_score: float,
    chart_sc: float,
    composite: float,
) -> List[str]:
    """Build human-readable reasoning for the signal."""
    reasons: List[str] = []

    if technical_readings:
        s = technical_readings.summary
        reasons.append(
            f"{s.get('bullish_count', 0)}/{s.get('bullish_count', 0) + s.get('bearish_count', 0) + s.get('neutral_count', 0)} "
            f"technical indicators bullish (tech_score={tech_score:.2f})"
        )

        # Highlight key indicators
        for name, reading in technical_readings.indicators.items():
            if reading.signal in ("bullish", "bearish"):
                reasons.append(f"  {name}: {reading.signal} (value={reading.value})")

    if sentiment_result and sentiment_result.score != 0:
        reasons.append(
            f"Sentiment: {sentiment_result.label} (score={sentiment_result.score:.3f}, "
            f"{len(sentiment_result.sources)} sources)"
        )

    if screenshot_ctx and screenshot_ctx.trend != "unknown":
        reasons.append(f"Chart analysis: trend={screenshot_ctx.trend}, confidence={screenshot_ctx.confidence:.2f}")
        if screenshot_ctx.patterns_detected:
            reasons.append(f"  Patterns: {', '.join(screenshot_ctx.patterns_detected)}")

    if cross_check_result:
        reasons.append(f"Cross-check trust: {cross_check_result.trust_score:.2f}")
        for w in cross_check_result.warnings:
            reasons.append(f"  WARNING: {w}")

    reasons.append(f"Composite score: {composite:.4f}")

    return reasons


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_signal(
    symbol: str,
    latest_price: Optional[float],
    technical_readings: Any = None,
    sentiment_result: Any = None,
    screenshot_ctx: Any = None,
    cross_check_result: Any = None,
    config_override: Optional[Dict[str, Any]] = None,
) -> TradeSignal:
    """
    Generate a BUY / SELL / HOLD signal with confidence and reasoning.

    Args:
        symbol:              Ticker symbol.
        latest_price:        Current market price.
        technical_readings:  TechnicalReadings from indicators module.
        sentiment_result:    SentimentResult from sentiment module.
        screenshot_ctx:      ScreenshotContext from screenshot_analyzer.
        cross_check_result:  CrossCheckResult from cross_checker.
        config_override:     Override signal engine config.

    Returns:
        TradeSignal with action, confidence, and reasoning.
    """
    cfg = load_config()
    sig_cfg = config_override or cfg.get("signal_engine", {})
    weights = sig_cfg.get("weights", {})

    w_tech = weights.get("technical", 0.55)
    w_sent = weights.get("sentiment", 0.15)
    w_chart = weights.get("screenshot_chart", 0.30)

    min_confirm = sig_cfg.get("min_confirmations", 3)
    conflict_thresh = sig_cfg.get("conflict_threshold", 0.4)
    buy_thresh = sig_cfg.get("buy_threshold", 0.3)
    sell_thresh = sig_cfg.get("sell_threshold", -0.3)

    trend_filter_cfg = sig_cfg.get("trend_filter", {})
    if trend_filter_cfg.get("enabled", False) and technical_readings is not None:
        sma_period = trend_filter_cfg.get("sma_period", 200)
        sma_reading = technical_readings.indicators.get(f"sma_{sma_period}")
        if sma_reading is None:
            # Config asks for a window the indicator set does not carry
            # (e.g. trend SMA 200 on a 60-bar intraday slice). Fall back to
            # the pullback predicate's trend window so a 15m scan is judged
            # against its own trend instead of a missing number.
            pb = sig_cfg.get("pullback", {})
            sma_reading = technical_readings.indicators.get(
                f"sma_{pb.get('trend_sma', sma_period)}")
        if sma_reading and sma_reading.value is not None and latest_price is not None:
            if latest_price < sma_reading.value:
                buy_thresh = 999  # block longs below trend (was: block shorts)
            if latest_price > sma_reading.value:
                sell_thresh = -999  # block shorts above trend (was: allow longs)

    # --- Compute sub-scores ---
    tech_sc = _technical_score(technical_readings)
    sent_sc = _sentiment_score(sentiment_result)
    chart_sc = _chart_score(screenshot_ctx, cross_check_result)

    # --- Entry gate: pullback strategy ---
    # Strategy note
    # -------------
    # In "pullback" mode a long is only taken when the market is in an uptrend,
    # price has pulled back into it, and MACD has turned back up. This is a
    # gate, not a scoring bonus. Indicator-count scoring alone kept buying
    # breakouts at the top of moves and produced a 42% win rate with a 1.39
    # profit factor; gating on the pullback setup is what survived the
    # out-of-sample test (see scripts/validate_oos.py).
    strategy = sig_cfg.get("entry_strategy", "pullback")
    pullback_gate = False
    if technical_readings is not None:
        if strategy == "pullback":
            pullback_gate = bool(getattr(technical_readings, "pullback_detected", False))
        else:
            pullback_gate = True  # "signals" mode keeps the original behaviour

    # --- Regime gate: ADX trend-strength filter ---
    # Sep 10-11 style losses came from trading directionless chop. When ADX
    # is below threshold there is no trend to pull back INTO, so both
    # directions are refused regardless of what the composite says.
    # Default OFF (min_adx: 0) until measured; sweep script sets the value.
    regime_cfg = sig_cfg.get("regime_filter", {})
    min_adx = regime_cfg.get("min_adx", 0)
    adx_ok = True
    adx_val: Optional[float] = None
    if min_adx and technical_readings is not None:
        for key, ind in technical_readings.indicators.items():
            if key.startswith("adx_"):
                adx_val = ind.value
                break
        if adx_val is None:
            adx_ok = False  # cannot prove a trend; refuse
        else:
            adx_ok = adx_val >= min_adx

    # --- Weighted composite ---
    # Normalize weights if some inputs are missing
    actual_weights = {}
    if technical_readings is not None:
        actual_weights["tech"] = w_tech
    if sentiment_result is not None and sentiment_result.score != 0:
        actual_weights["sent"] = w_sent
    if screenshot_ctx is not None and screenshot_ctx.trend != "unknown":
        actual_weights["chart"] = w_chart

    if not actual_weights:
        # No data at all
        return TradeSignal(
            symbol=symbol,
            action="HOLD",
            entry_price=latest_price,
            confidence=0,
            reasoning=["No data available for analysis"],
            generated_at=utc_now(),
        )

    total_w = sum(actual_weights.values())
    composite = 0.0
    if "tech" in actual_weights:
        composite += tech_sc * actual_weights["tech"] / total_w
    if "sent" in actual_weights:
        composite += sent_sc * actual_weights["sent"] / total_w
    if "chart" in actual_weights:
        composite += chart_sc * actual_weights["chart"] / total_w

    composite = max(-1.0, min(1.0, composite))

    # --- Conflict detection ---
    conflict = False
    if technical_readings is not None and sentiment_result is not None:
        if abs(tech_sc - sent_sc) > conflict_thresh:
            if (tech_sc > 0 and sent_sc < -0.2) or (tech_sc < 0 and sent_sc > 0.2):
                conflict = True
                logger.warning(
                    "%s: tech/sentiment conflict (tech=%.2f, sent=%.2f)",
                    symbol, tech_sc, sent_sc,
                )

    # --- Confirmation count ---
    if composite > 0:
        confirmations = _count_confirmations(technical_readings, "bullish")
        direction = "bullish"
    elif composite < 0:
        confirmations = _count_confirmations(technical_readings, "bearish")
        direction = "bearish"
    else:
        confirmations = 0
        direction = "neutral"

    # --- Determine action ---
    # The pullback gate applies to long entries only. The out-of-sample
    # evidence covers longs (buying dips in Indian large-cap uptrends).
    # If shorts are enabled (via allow_shorts: true + entry_strategy="signals"),
    # they flow through the composite path when momentum confirms.
    # NOTE: the top-level `trading.allow_shorts` flag lives beside the
    # signal_engine section in config.yaml, so read it from the full config
    # (sig_cfg is only the signal_engine subsection and would miss it).
    try:
        _full_cfg = load_config()
        allow_shorts = bool(_full_cfg.get("trading", {}).get("allow_shorts", False))
    except Exception:
        allow_shorts = bool(sig_cfg.get("trading", {}).get("allow_shorts", False))

    action = "HOLD"
    if composite >= buy_thresh and confirmations >= min_confirm and not conflict:
        action = "BUY" if (pullback_gate and adx_ok) else "HOLD"
    elif composite <= sell_thresh and confirmations >= min_confirm and not conflict:
        if allow_shorts and adx_ok and _macd_confirms_short(technical_readings):
            action = "SELL"
        # else: long-only mode — bearish composite is HOLD.
        # The position, if any, exits via its stop/target/squareoff.

    # --- Confidence ---
    # Base confidence from composite strength (0 = 50%, 1 = 100%)
    base_conf = min(abs(composite), 1.0) * 50 + 50
    # Penalize for low confirmations
    if confirmations < min_confirm:
        base_conf *= 0.6
    # Penalize for conflict
    if conflict:
        base_conf *= 0.5
    # Boost for cross-check trust
    if cross_check_result:
        base_conf *= (0.5 + 0.5 * cross_check_result.trust_score)

    confidence = int(max(0, min(100, base_conf)))

    if action == "HOLD":
        confidence = min(confidence, 50)

    # --- Reasoning ---
    reasoning = _build_reasoning(
        technical_readings, sentiment_result, screenshot_ctx,
        cross_check_result, tech_sc, sent_sc, chart_sc, composite,
    )

    if conflict:
        reasoning.insert(0, "CONFLICT: technicals and sentiment strongly disagree — defaulting to HOLD")

    if confirmations < min_confirm and action != "HOLD":
        reasoning.insert(0, f"Low confirmations ({confirmations}/{min_confirm}) — signal weakened")

    if not pullback_gate and composite >= buy_thresh:
        reasoning.insert(0, (
            f"NO PULLBACK SETUP: price is not in an uptrend pullback with MACD turning up "
            f"(entry_strategy='{strategy}') — long entry skipped"
        ))

    _macd = technical_readings.indicators.get("macd") if technical_readings else None
    if action == "HOLD" and composite <= sell_thresh and _macd is not None and _macd.value is not None:
        _sig = _macd.extra.get("signal_line")
        if _sig is not None and _macd.value >= _sig:
            reasoning.insert(0, (
                "SHORT BLOCKED: MACD line is above its signal line — momentum has "
                "turned up, refusing to short into a possible bottom"
            ))

    if not adx_ok and action == "HOLD":
        if adx_val is None:
            reasoning.insert(0, "REGIME BLOCKED: ADX not computable — no proven trend, entry refused")
        else:
            reasoning.insert(0, (
                f"REGIME BLOCKED: ADX {adx_val:.1f} below {min_adx} — "
                f"chop regime, entry refused"
            ))

    signal = TradeSignal(
        symbol=symbol,
        action=action,
        entry_price=latest_price,
        confidence=confidence,
        composite_score=round(composite, 4),
        reasoning=reasoning,
        generated_at=utc_now(),
    )

    logger.info(
        "Signal %s: %s (confidence=%d%%, composite=%.4f, confirmations=%d)",
        symbol, action, confidence, composite, confirmations,
    )

    return signal
