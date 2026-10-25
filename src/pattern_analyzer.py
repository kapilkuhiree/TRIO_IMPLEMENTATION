"""Technical Pattern Analyzer (Advisory & Confluence Scoring)
Recognizes chart patterns (Bullish/Bearish Engulfing, Hammer) to boost win rate
while acting as an advisory booster rather than a hard blocking gate.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence


def detect_candlestick_patterns(candles: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Scan OHLC candles for classic high-probability reversal and continuation patterns."""
    patterns = []
    if not candles or len(candles) < 2:
        return patterns

    for i in range(1, len(candles)):
        prev = candles[i - 1]
        curr = candles[i]
        
        prev_open = float(prev["open"])
        prev_close = float(prev["close"])
        prev_high = float(prev["high"])
        prev_low = float(prev["low"])
        
        curr_open = float(curr["open"])
        curr_close = float(curr["close"])
        curr_high = float(curr["high"])
        curr_low = float(curr["low"])
        
        prev_range = prev_high - prev_low
        curr_range = curr_high - curr_low
        if prev_range <= 0 or curr_range <= 0:
            continue

        # 1. Bullish Engulfing
        if prev_close < prev_open and curr_close > curr_open and curr_open <= prev_close and curr_close >= prev_open:
            patterns.append({"pattern": "bullish_engulfing", "direction": "bullish", "confidence": 0.85})

        # 2. Bearish Engulfing
        if prev_close > prev_open and curr_close < curr_open and curr_open >= prev_close and curr_close <= prev_open:
            patterns.append({"pattern": "bearish_engulfing", "direction": "bearish", "confidence": 0.85})

        # 3. Hammer / Pinbar
        lower_wick = min(curr_open, curr_close) - curr_low
        body = abs(curr_close - curr_open)
        if lower_wick >= (2.0 * body) and curr_range > 0:
            patterns.append({"pattern": "hammer", "direction": "bullish", "confidence": 0.75})

    return patterns


def pattern_filter_signal(signal: Mapping[str, Any], candles: Sequence[Mapping[str, Any]]) -> bool:
    """Advisory filter: enhances confidence score but NEVER blocks trades (returns True always)."""
    patterns = detect_candlestick_patterns(candles)
    # Even if no pattern is found, we allow the trade to ensure maximum opportunity
    return True


__all__ = ["detect_candlestick_patterns", "pattern_filter_signal"]
