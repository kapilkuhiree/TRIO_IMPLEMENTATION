"""Causal demand/supply confirmation for completed intraday candles.

This module is intentionally independent of the strategy classes.  It turns a
completed candle sequence into a small set of demand/supply zones formed by a
base candle followed by a directional displacement, then checks whether the
latest signal candle reacts near one of those zones.

The evaluator never looks beyond the requested signal index.  That makes it
safe for live use and for deterministic replay: candles added after a signal
cannot change the result for that signal.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
import math
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple


IST = timezone(timedelta(hours=5, minutes=30))
NSE_OPEN = time(9, 15)
NSE_CLOSE = time(15, 15)
_INTRADAY_RE = re.compile(r"^(?P<value>\d+)(?P<unit>[mh])$")

DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "lookback_bars": 40,
    "max_zone_age_bars": 24,
    "max_touches": 2,
    "atr_period": 14,
    "base_body_atr": 0.70,
    "base_range_atr": 1.50,
    "displacement_atr": 0.80,
    "proximity_atr": 0.75,
    "equity_timeframes": ["5m", "15m"],
    "equity_timeframe": "15m",
    "options_timeframe": "5m",
}


@dataclass(frozen=True)
class ZoneConfirmation:
    """Auditable result of one directional demand/supply check."""

    confirmed: bool
    direction: str
    zone_type: str = ""
    zone_low: Optional[float] = None
    zone_high: Optional[float] = None
    zone_timestamp: str = ""
    trigger_timestamp: str = ""
    touches: int = 0
    age_bars: int = 0
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _timestamp(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(IST)


def _candle(row: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(row, dict):
        return None
    timestamp = _timestamp(row.get("timestamp", row.get("datetime", "")))
    values = {
        "open": _finite(row.get("open", row.get("Open"))),
        "high": _finite(row.get("high", row.get("High"))),
        "low": _finite(row.get("low", row.get("Low"))),
        "close": _finite(row.get("close", row.get("Close"))),
    }
    if timestamp is None or any(value is None for value in values.values()):
        return None
    if values["low"] <= 0 or values["high"] < values["low"]:
        return None
    if values["open"] < values["low"] or values["open"] > values["high"]:
        return None
    if values["close"] < values["low"] or values["close"] > values["high"]:
        return None
    return {
        "timestamp": timestamp.isoformat(),
        **values,
        "volume": _finite(row.get("volume", row.get("Volume"))),
        "completed": row.get("completed", True) is not False,
    }


def _normalise(candles: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = []
    for row in candles or []:
        normalized = _candle(row)
        if normalized is not None and normalized["completed"]:
            result.append(normalized)
    result.sort(key=lambda item: item["timestamp"])
    return result


def _float_config(config: Dict[str, Any], key: str) -> float:
    value = _finite(config.get(key))
    return value if value is not None else float(DEFAULTS[key])


def demand_supply_config(cfg: Optional[Dict[str, Any]] = None,
                         *, context: str = "equity") -> Dict[str, Any]:
    """Return effective settings without mutating the caller's config.

    ``TRIO_DEMAND_SUPPLY`` is an explicit runtime switch.  The options overlay
    may also contain a nested ``demand_supply`` block; this is used for strict
    options while the equity screen uses the top-level block or the switch.
    """
    source = (cfg or {}).get("demand_supply", {}) or {}
    if context == "options":
        source = {
            **source,
            **(((cfg or {}).get("options_intraday", {}) or {}).get(
                "demand_supply", {}) or {}),
        }
    result = dict(DEFAULTS)
    if isinstance(source, dict):
        result.update(source)
    raw = os.environ.get("TRIO_DEMAND_SUPPLY")
    if raw is not None:
        result["enabled"] = str(raw).strip().lower() in {
            "1", "true", "yes", "on", "y"
        }
    return result


def demand_supply_enabled(cfg: Optional[Dict[str, Any]] = None,
                          *, context: str = "equity",
                          timeframe: Optional[str] = None) -> bool:
    settings = demand_supply_config(cfg, context=context)
    if not bool(settings.get("enabled", False)):
        return False
    if context == "options":
        wanted = str(settings.get("options_timeframe", "5m")).lower()
        return timeframe is None or str(timeframe).lower() == wanted
    timeframes = settings.get("equity_timeframes", DEFAULTS["equity_timeframes"])
    if isinstance(timeframes, str):
        timeframes = [item.strip() for item in timeframes.split(",")]
    return timeframe is None or str(timeframe).lower() in {
        str(item).lower() for item in (timeframes or [])
    }


def _atr(candles: List[Dict[str, Any]], index: int, period: int) -> float:
    start = max(0, index - max(1, period) + 1)
    ranges = []
    previous_close = None
    for row in candles[start:index + 1]:
        high, low, close = row["high"], row["low"], row["close"]
        true_range = high - low
        if previous_close is not None:
            true_range = max(true_range, abs(high - previous_close),
                             abs(low - previous_close))
        ranges.append(true_range)
        previous_close = close
    return sum(ranges) / len(ranges) if ranges else 0.0


def _zone_candidates(candles: List[Dict[str, Any]], index: int,
                     settings: Dict[str, Any]) -> List[Dict[str, Any]]:
    period = max(2, int(_float_config(settings, "atr_period")))
    lookback = max(4, int(_float_config(settings, "lookback_bars")))
    start = max(1, index - lookback)
    zones: List[Dict[str, Any]] = []
    for base_index in range(start, max(start, index - 1)):
        base = candles[base_index]
        base_atr = _atr(candles, base_index, period)
        base_range = base["high"] - base["low"]
        if base_atr <= 0 or base_range > _float_config(settings, "base_range_atr") * base_atr:
            continue
        if abs(base["close"] - base["open"]) > _float_config(settings, "base_body_atr") * base_atr:
            continue
        for displacement in range(base_index + 1, min(index, base_index + 4)):
            row = candles[displacement]
            row_atr = _atr(candles, displacement, period)
            body = abs(row["close"] - row["open"])
            if row_atr <= 0 or body < _float_config(settings, "displacement_atr") * row_atr:
                continue
            if row["close"] > row["open"] and row["close"] > base["high"]:
                zone_type = "demand"
                zone_low = base["low"]
                zone_high = max(base["open"], base["close"])
            elif row["close"] < row["open"] and row["close"] < base["low"]:
                zone_type = "supply"
                zone_low = min(base["open"], base["close"])
                zone_high = base["high"]
            else:
                continue
            invalidated = False
            touches = 0
            for later in candles[displacement + 1:index]:
                if zone_type == "demand" and later["close"] < zone_low:
                    invalidated = True
                    break
                if zone_type == "supply" and later["close"] > zone_high:
                    invalidated = True
                    break
                if later["low"] <= zone_high and later["high"] >= zone_low:
                    touches += 1
            if invalidated or touches > int(_float_config(settings, "max_touches")):
                continue
            age = index - displacement
            if age > int(_float_config(settings, "max_zone_age_bars")):
                continue
            zones.append({
                "zone_type": zone_type,
                "zone_low": zone_low,
                "zone_high": zone_high,
                "zone_index": displacement,
                "zone_timestamp": row["timestamp"],
                "touches": touches,
                "age": age,
                "strength": body / row_atr,
            })
            break
    return zones


def confirm_demand_supply(candles: Iterable[Dict[str, Any]], direction: str,
                          index: int = -1,
                          cfg: Optional[Dict[str, Any]] = None,
                          *, context: str = "equity") -> ZoneConfirmation:
    """Confirm a bullish demand or bearish supply reaction.

    ``index`` identifies the completed signal candle.  The zone itself is
    formed strictly from candles before that candle.
    """
    settings = demand_supply_config(cfg, context=context)
    normalized = _normalise(candles)
    wanted = str(direction or "").strip().lower()
    if wanted in {"buy", "long", "bullish", "ce"}:
        wanted, zone_type = "bullish", "demand"
    elif wanted in {"sell", "short", "bearish", "pe"}:
        wanted, zone_type = "bearish", "supply"
    else:
        return ZoneConfirmation(False, wanted, reason="invalid direction")
    if not bool(settings.get("enabled", False)):
        return ZoneConfirmation(True, wanted, reason="demand/supply disabled")
    if index < 0:
        index += len(normalized)
    if index < 1 or index >= len(normalized):
        return ZoneConfirmation(False, wanted, reason="no completed signal candle")
    current = normalized[index]
    if not current["completed"]:
        return ZoneConfirmation(False, wanted, trigger_timestamp=current["timestamp"],
                                reason="signal candle is incomplete")
    zones = [zone for zone in _zone_candidates(normalized, index, settings)
             if zone["zone_type"] == zone_type]
    if not zones:
        return ZoneConfirmation(False, wanted, trigger_timestamp=current["timestamp"],
                                reason=f"no fresh {zone_type} zone")
    atr = _atr(normalized, index, max(2, int(_float_config(settings, "atr_period"))))
    proximity = atr * _float_config(settings, "proximity_atr")
    matches = []
    for zone in zones:
        if wanted == "bullish":
            touched = current["low"] <= zone["zone_high"] + proximity
            reacted = current["close"] > current["open"] and current["close"] >= zone["zone_high"]
        else:
            touched = current["high"] >= zone["zone_low"] - proximity
            reacted = current["close"] < current["open"] and current["close"] <= zone["zone_low"]
        if touched and reacted:
            matches.append(zone)
    if not matches:
        return ZoneConfirmation(False, wanted, trigger_timestamp=current["timestamp"],
                                reason=f"signal did not react at fresh {zone_type} zone")
    chosen = sorted(matches, key=lambda item: (
        item["touches"], item["age"], -item["strength"]))[0]
    return ZoneConfirmation(
        True, wanted, zone_type=zone_type,
        zone_low=round(chosen["zone_low"], 8),
        zone_high=round(chosen["zone_high"], 8),
        zone_timestamp=chosen["zone_timestamp"],
        trigger_timestamp=current["timestamp"],
        touches=chosen["touches"], age_bars=chosen["age"],
        reason=f"{zone_type} reaction confirmed",
    )


def candles_from_frame(frame: Any, *, symbol: str = "") -> List[Dict[str, Any]]:
    """Convert a pandas-like OHLCV frame to the pure candle schema."""
    if frame is None or not hasattr(frame, "iterrows"):
        return []
    rows = []
    for index, series in frame.iterrows():
        row = dict(series)
        timestamp = row.get("timestamp", row.get("Timestamp", index))
        rows.append({
            "timestamp": timestamp.isoformat() if hasattr(timestamp, "isoformat") else timestamp,
            "open": row.get("Open", row.get("open")),
            "high": row.get("High", row.get("high")),
            "low": row.get("Low", row.get("low")),
            "close": row.get("Close", row.get("close")),
            "volume": row.get("Volume", row.get("volume")),
            "symbol": symbol,
        })
    return rows


def _interval_minutes(timeframe: str) -> Optional[int]:
    match = _INTRADAY_RE.match(str(timeframe or "").strip().lower())
    if not match:
        return None
    value = int(match.group("value"))
    return value if match.group("unit") == "m" else value * 60


def filter_completed_frame(frame: Any, timeframe: str,
                           now: Optional[datetime] = None) -> Tuple[Any, Dict[str, Any]]:
    """Drop forming and out-of-session intraday rows without poisoning history."""
    minutes = _interval_minutes(timeframe)
    if minutes is None or frame is None or not hasattr(frame, "iterrows"):
        return frame, {"valid": True, "filtered": False, "reason": "not_intraday"}
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(IST)
    keep = []
    dropped = {"outside_session": 0, "incomplete": 0, "invalid_timestamp": 0}
    for index, _series in frame.iterrows():
        timestamp = _timestamp(index)
        if timestamp is None:
            dropped["invalid_timestamp"] += 1
            continue
        if timestamp.time() < NSE_OPEN or timestamp.time() >= NSE_CLOSE:
            dropped["outside_session"] += 1
            continue
        if timestamp + timedelta(minutes=minutes) > current:
            dropped["incomplete"] += 1
            continue
        keep.append(index)
    filtered = frame.loc[keep].copy() if keep else frame.iloc[0:0].copy()
    return filtered, {
        "valid": bool(len(filtered)),
        "filtered": True,
        "rows_in": int(len(frame)),
        "rows_out": int(len(filtered)),
        "dropped": dropped,
        "timeframe": timeframe,
        "latest_timestamp": (str(filtered.index[-1]) if len(filtered) else None),
    }


__all__ = [
    "ZoneConfirmation", "candles_from_frame", "confirm_demand_supply",
    "demand_supply_config", "demand_supply_enabled", "filter_completed_frame",
]
