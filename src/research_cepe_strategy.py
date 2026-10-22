"""Research-only trend/pullback/breakout candidates for CE and PE studies.

The module deliberately stops at an underlying-level research candidate.  It
does not select an option contract, estimate a premium, place an order, or
connect to a runtime.  Every evaluation is made from the completed candle
prefix ending at the requested signal index.

Input candles are normalized mappings with these fields::

    timestamp, open, high, low, close, volume, exchange,
    underlying_symbol, instrument_type

``stock`` and ``index`` are the only accepted underlying instrument types.
The default exchange is NSE.  A caller that has a wall-clock boundary can
also pass ``as_of``; a 5-minute candle is accepted only when its whole bar has
closed by that boundary.  The optional ``completed``/``is_complete`` flags
are respected when present.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Any, Optional


STRATEGY_NAME = "trend_pullback_breakout_research"
DEFAULT_EXCHANGE = "NSE"
SUPPORTED_INSTRUMENT_TYPES = frozenset({"stock", "index"})


@dataclass(frozen=True)
class ResearchCEPEConfig:
    """Conservative, fixed research thresholds.

    These values describe the candidate definition; they are not an
    optimization surface.  In particular, the breakout must be confirmed by
    the current completed candle and the previous completed candle must have
    interacted with EMA20.
    """

    ema_fast: int = 20
    ema_slow: int = 50
    atr_period: int = 14
    min_history: int = 55
    pullback_tolerance: float = 0.0125
    min_body_to_range: float = 0.60
    min_close_location: float = 0.70
    breakout_buffer: float = 0.0000
    min_signal_range_atr: float = 0.80
    volume_multiplier: float = 1.20
    volume_lookback: int = 20
    range_expansion_multiplier: float = 1.10
    stop_atr_buffer: float = 0.15
    target_r_multiple: float = 2.0

    def __post_init__(self) -> None:
        if self.ema_fast <= 0 or self.ema_slow <= 0 or self.atr_period <= 0:
            raise ValueError("indicator periods must be positive")
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast must be smaller than ema_slow")
        if self.min_history < self.ema_slow + 1:
            raise ValueError("min_history must include the slow EMA and a signal bar")
        if not 0 < self.pullback_tolerance < 0.10:
            raise ValueError("pullback_tolerance must be between 0 and 0.10")
        if not 0 < self.min_body_to_range <= 1:
            raise ValueError("min_body_to_range must be in (0, 1]")
        if not 0.5 <= self.min_close_location <= 1:
            raise ValueError("min_close_location must be in [0.5, 1]")
        if self.breakout_buffer < 0:
            raise ValueError("breakout_buffer cannot be negative")
        if self.min_signal_range_atr <= 0:
            raise ValueError("min_signal_range_atr must be positive")
        if self.volume_multiplier <= 0 or self.volume_lookback <= 0:
            raise ValueError("volume thresholds must be positive")
        if self.range_expansion_multiplier <= 0:
            raise ValueError("range_expansion_multiplier must be positive")
        if self.stop_atr_buffer < 0 or self.target_r_multiple <= 0:
            raise ValueError("stop/target thresholds are invalid")


DEFAULT_CONFIG = ResearchCEPEConfig()


@dataclass(frozen=True)
class _Candle:
    """Validated candle with the original timestamp retained for output."""

    timestamp: Any
    parsed_timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float]
    underlying: str
    instrument_type: str

    @property
    def candle_range(self) -> float:
        return self.high - self.low


def _finite_number(value: Any, *, strictly_positive: bool = False) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    if strictly_positive and number <= 0:
        return None
    return number


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _flag_is_incomplete(row: Mapping[str, Any]) -> bool:
    """Return true for any explicit false completion marker."""

    for key in ("completed", "is_complete", "complete", "closed"):
        if key not in row:
            continue
        value = row[key]
        if isinstance(value, bool):
            if not value:
                return True
        elif value is None or str(value).strip().lower() in {
            "0", "false", "no", "open", "forming", "incomplete",
        }:
            return True
    return False


def _normalise_expected_underlying(
    underlying_symbol: Optional[str],
    expected_underlying: Optional[str],
) -> Optional[str]:
    if underlying_symbol is not None and expected_underlying is not None:
        if underlying_symbol != expected_underlying:
            return None
    return expected_underlying if expected_underlying is not None else underlying_symbol


def _validated_prefix(
    rows: Sequence[Mapping[str, Any]],
    index: int,
    *,
    expected_underlying: Optional[str],
    expected_exchange: str,
    expected_instrument_type: Optional[str],
    as_of: Any,
) -> Optional[tuple[list[_Candle], str, str]]:
    """Validate only rows through ``index`` and return their causal prefix."""

    if index < 0 or index >= len(rows):
        return None
    if not isinstance(expected_exchange, str) or not expected_exchange.strip():
        return None
    expected_exchange_key = expected_exchange.strip().upper()
    if expected_instrument_type is not None:
        if not isinstance(expected_instrument_type, str):
            return None
        if expected_instrument_type.strip().lower() not in SUPPORTED_INSTRUMENT_TYPES:
            return None
        expected_type_key = expected_instrument_type.strip().lower()
    else:
        expected_type_key = None

    as_of_dt = _parse_timestamp(as_of) if as_of is not None else None
    if as_of is not None and as_of_dt is None:
        return None

    prefix: list[_Candle] = []
    actual_underlying: Optional[str] = None
    actual_type: Optional[str] = None
    previous_timestamp: Optional[datetime] = None

    for raw in rows[: index + 1]:
        if not isinstance(raw, Mapping) or "timestamp" not in raw:
            return None
        if _flag_is_incomplete(raw):
            return None

        parsed_timestamp = _parse_timestamp(raw.get("timestamp"))
        if parsed_timestamp is None:
            return None
        if (
            parsed_timestamp.minute % 5 != 0
            or parsed_timestamp.second != 0
            or parsed_timestamp.microsecond != 0
        ):
            return None
        if previous_timestamp is not None and parsed_timestamp <= previous_timestamp:
            return None
        previous_timestamp = parsed_timestamp

        # A timestamp identifies the start of this 5-minute bar.  If a caller
        # supplies an observation boundary, require the complete bar to have
        # elapsed, which rejects both forming and future rows without using a
        # wall clock inside this deterministic module.
        if as_of_dt is not None and parsed_timestamp + timedelta(minutes=5) > as_of_dt:
            return None

        symbol = raw.get("underlying_symbol")
        exchange = raw.get("exchange")
        instrument_type = raw.get("instrument_type")
        if not isinstance(symbol, str) or not symbol.strip():
            return None
        if not isinstance(exchange, str) or exchange.strip().upper() != expected_exchange_key:
            return None
        if not isinstance(instrument_type, str):
            return None
        type_key = instrument_type.strip().lower()
        if type_key not in SUPPORTED_INSTRUMENT_TYPES:
            return None
        if expected_underlying is not None and symbol != expected_underlying:
            return None
        if expected_type_key is not None and type_key != expected_type_key:
            return None

        if actual_underlying is None:
            actual_underlying = symbol
        elif symbol != actual_underlying:
            return None
        if actual_type is None:
            actual_type = type_key
        elif type_key != actual_type:
            return None

        values = {
            name: _finite_number(raw.get(name), strictly_positive=True)
            for name in ("open", "high", "low", "close")
        }
        if any(value is None for value in values.values()):
            return None
        open_price = values["open"]
        high = values["high"]
        low = values["low"]
        close = values["close"]
        assert open_price is not None and high is not None
        assert low is not None and close is not None
        if high < low or open_price < low or open_price > high or close < low or close > high:
            return None

        volume = _finite_number(raw.get("volume"))
        if volume is not None and volume < 0:
            return None
        prefix.append(
            _Candle(
                timestamp=raw.get("timestamp"),
                parsed_timestamp=parsed_timestamp,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
                underlying=symbol,
                instrument_type=type_key,
            )
        )

    if actual_underlying is None or actual_type is None:
        return None
    return prefix, actual_underlying, actual_type


def _ema(values: Sequence[float], period: int) -> list[float]:
    alpha = 2.0 / (period + 1.0)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(alpha * float(value) + (1.0 - alpha) * result[-1])
    return result


def _atr(candles: Sequence[_Candle], period: int) -> list[Optional[float]]:
    true_ranges: list[float] = []
    for position, candle in enumerate(candles):
        if position == 0:
            true_ranges.append(candle.high - candle.low)
            continue
        previous_close = candles[position - 1].close
        true_ranges.append(
            max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        )

    result: list[Optional[float]] = [None] * len(candles)
    for position in range(period - 1, len(candles)):
        result[position] = sum(true_ranges[position - period + 1 : position + 1]) / period
    return result


def _round_level(value: float) -> float:
    # Fixed decimal formatting avoids tiny platform-dependent repr artifacts
    # while retaining substantially more precision than a normal price tick.
    return round(float(value), 8)


def _volume_confirmation(
    candles: Sequence[_Candle],
    index: int,
    atr_value: float,
    config: ResearchCEPEConfig,
) -> tuple[bool, str]:
    current = candles[index]
    previous = candles[index - 1]
    prior_volumes = [
        candle.volume
        for candle in candles[max(0, index - config.volume_lookback) : index]
        if candle.volume is not None and candle.volume > 0
    ]
    current_volume_ok = (
        current.volume is not None
        and current.volume > 0
        and bool(prior_volumes)
        and current.volume >= (sum(prior_volumes) / len(prior_volumes)) * config.volume_multiplier
    )
    if current_volume_ok:
        return True, "volume confirmation"

    # Missing/zero volume is common in normalized index candles.  It is not a
    # failure by itself: the completed signal bar must instead expand beyond
    # the prior candle and be large relative to causal ATR14.
    range_expansion_ok = (
        current.candle_range >= previous.candle_range * config.range_expansion_multiplier
        and current.candle_range >= atr_value
    )
    if range_expansion_ok:
        return True, "range-expansion fallback"
    return False, "volume/range confirmation absent"


def _signal_from_prefix(
    candles: Sequence[_Candle],
    index: int,
    *,
    underlying: str,
    config: ResearchCEPEConfig,
) -> Optional[dict[str, Any]]:
    if len(candles) < config.min_history or index < 1:
        return None

    closes = [candle.close for candle in candles]
    ema_fast = _ema(closes, config.ema_fast)
    ema_slow = _ema(closes, config.ema_slow)
    atr_values = _atr(candles, config.atr_period)
    atr_value = atr_values[index]
    if atr_value is None or atr_value <= 0:
        return None

    current = candles[index]
    previous = candles[index - 1]
    previous_ema_fast = ema_fast[index - 1]
    previous_ema_slow = ema_slow[index - 1]
    current_ema_fast = ema_fast[index]
    current_ema_slow = ema_slow[index]
    previous_range = previous.candle_range
    current_range = current.candle_range
    if previous_range <= 0 or current_range <= 0:
        return None

    current_body_ratio = abs(current.close - current.open) / current_range
    close_location = (current.close - current.low) / current_range
    if current_body_ratio < config.min_body_to_range:
        return None
    if current_range < atr_value * config.min_signal_range_atr:
        return None

    volume_ok, volume_reason = _volume_confirmation(candles, index, atr_value, config)
    if not volume_ok:
        return None

    tolerance = config.pullback_tolerance
    bullish_pullback = (
        previous.low <= previous_ema_fast * (1.0 + tolerance)
        and previous.high >= previous_ema_fast * (1.0 - tolerance)
        and previous.close <= previous_ema_fast * (1.0 + tolerance)
        and previous.close >= previous_ema_fast * (1.0 - 2.0 * tolerance)
    )
    bearish_pullback = (
        previous.high >= previous_ema_fast * (1.0 - tolerance)
        and previous.low <= previous_ema_fast * (1.0 + tolerance)
        and previous.close >= previous_ema_fast * (1.0 - tolerance)
        and previous.close <= previous_ema_fast * (1.0 + 2.0 * tolerance)
    )

    bullish = (
        current_ema_fast > current_ema_slow
        and previous_ema_fast > previous_ema_slow
        and previous.close >= previous_ema_slow
        and current.close > current_ema_fast
        and bullish_pullback
        and close_location >= config.min_close_location
        and current.close > previous.high * (1.0 + config.breakout_buffer)
    )
    bearish = (
        current_ema_fast < current_ema_slow
        and previous_ema_fast < previous_ema_slow
        and previous.close <= previous_ema_slow
        and current.close < current_ema_fast
        and bearish_pullback
        and close_location <= 1.0 - config.min_close_location
        and current.close < previous.low * (1.0 - config.breakout_buffer)
    )
    if not bullish and not bearish:
        return None

    if bullish:
        direction = "bullish"
        option_type = "CE"
        entry = current.close
        stop = min(previous.low, current.low) - atr_value * config.stop_atr_buffer
        risk = entry - stop
        target = entry + risk * config.target_r_multiple
        breakout_level = "previous high"
    else:
        direction = "bearish"
        option_type = "PE"
        entry = current.close
        stop = max(previous.high, current.high) + atr_value * config.stop_atr_buffer
        risk = stop - entry
        target = entry - risk * config.target_r_multiple
        breakout_level = "previous low"

    if risk <= 0 or target <= 0:
        return None

    return {
        "signal_timestamp": current.timestamp,
        "underlying": underlying,
        "direction": direction,
        "option_type": option_type,
        "action": "BUY",
        "entry": _round_level(entry),
        "stop": _round_level(stop),
        "target": _round_level(target),
        # Standard signal-capture aliases; these remain underlying levels,
        # never option premiums or guessed contract fields.
        "entry_price": _round_level(entry),
        "stop_loss": _round_level(stop),
        "target_price": _round_level(target),
        "reason": (
            f"{direction} EMA20/EMA50 trend, prior completed candle pulled back "
            f"to EMA20, current completed candle broke {breakout_level} with "
            f"strong body/range and {volume_reason}"
        ),
        "strategy_name": STRATEGY_NAME,
    }


def _as_rows(candles: Iterable[Mapping[str, Any]]) -> Optional[list[Mapping[str, Any]]]:
    try:
        rows = list(candles)
    except TypeError:
        return None
    return rows


def generate_candidates(
    candles: Iterable[Mapping[str, Any]],
    *,
    underlying_symbol: Optional[str] = None,
    expected_underlying: Optional[str] = None,
    expected_exchange: str = DEFAULT_EXCHANGE,
    expected_instrument_type: Optional[str] = None,
    index: Optional[int] = None,
    as_of: Any = None,
    config: ResearchCEPEConfig = DEFAULT_CONFIG,
) -> list[dict[str, Any]]:
    """Return deterministic underlying-level BUY candidates.

    If ``index`` is supplied, only that completed candle and its prefix are
    evaluated.  Rows after the index are intentionally ignored, so appending
    future data cannot change a signal already available at the index.  With
    no index, each row is evaluated in chronological order and each candidate
    still sees only its own prefix.
    """

    if not isinstance(config, ResearchCEPEConfig):
        return []
    rows = _as_rows(candles)
    if rows is None or not rows:
        return []
    requested_underlying = _normalise_expected_underlying(
        underlying_symbol, expected_underlying,
    )
    if underlying_symbol is not None and expected_underlying is not None and requested_underlying is None:
        return []

    if index is not None:
        indices = [index]
    else:
        indices = range(len(rows))

    candidates: list[dict[str, Any]] = []
    for signal_index in indices:
        if not isinstance(signal_index, int):
            continue
        validated = _validated_prefix(
            rows,
            signal_index,
            expected_underlying=requested_underlying,
            expected_exchange=expected_exchange,
            expected_instrument_type=expected_instrument_type,
            as_of=as_of,
        )
        if validated is None:
            continue
        prefix, actual_underlying, _ = validated
        candidate = _signal_from_prefix(
            prefix,
            signal_index,
            underlying=actual_underlying,
            config=config,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def evaluate_signal(
    candles: Iterable[Mapping[str, Any]],
    index: Optional[int] = None,
    **kwargs: Any,
) -> Optional[dict[str, Any]]:
    """Evaluate one signal index and return a candidate dict or ``None``."""

    rows = _as_rows(candles)
    if rows is None or not rows:
        return None
    signal_index = len(rows) - 1 if index is None else index
    candidates = generate_candidates(rows, index=signal_index, **kwargs)
    return candidates[0] if candidates else None


def trend_pullback_breakout_research(
    candles: Iterable[Mapping[str, Any]],
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Named strategy entry point for research callers."""

    return generate_candidates(candles, **kwargs)


__all__ = [
    "DEFAULT_CONFIG",
    "DEFAULT_EXCHANGE",
    "ResearchCEPEConfig",
    "STRATEGY_NAME",
    "SUPPORTED_INSTRUMENT_TYPES",
    "evaluate_signal",
    "generate_candidates",
    "trend_pullback_breakout_research",
]
