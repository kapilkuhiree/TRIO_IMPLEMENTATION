"""Shadow-only ORB + true-VWAP sleeve, measured on NIFTY futures.

Why this exists
---------------
The live ``orb`` sleeve generates signals on the NIFTY **spot index**, whose
candles report ``Volume = 0``.  The scan-level "VWAP" therefore fell back to an
equal-weighted average of ``(H+L+C)/3`` — not a VWAP.  A 21-session replay on
NIFTY futures (which do carry volume) showed two things:

* a genuine VWAP never blocked a single close-confirmed ORB entry, and
* the day's only losing trade was a *repeat* entry on the same breakdown.

This module records what a corrected rule set WOULD have done, every session,
without touching the live book.  It is the forward evidence required before
any production change.

Hard rules
----------
* **Shadow only.**  Nothing here places, sizes, blocks or alters an order, and
  nothing in the live runtime imports it.
* **Same instrument.**  ORB and VWAP are both computed from the futures series.
  Spot is never mixed in.
* **Fail closed.**  If futures volume is unavailable or session coverage is
  below ``MIN_VOLUME_COVERAGE``, the record says so and no signal is emitted.
  The equal-weight fallback is deliberately *not* reproduced here.
* **Reproducible.**  Pure functions take candle lists; the broker fetch is a
  thin adapter so tests never need network access.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent

IST = timezone(timedelta(hours=5, minutes=30))

# Traded geometry.  A NIFTY lot is 65; premium P&L is approximated with an ATM
# delta of 0.5, so one underlying point is worth 0.5 x 65 = Rs 32.5.
LOT_SIZE = 65
ATM_DELTA = 0.5
POINT_VALUE_INR = ATM_DELTA * LOT_SIZE

# Round-trip cost in underlying points.  Derived from Rs 66.5 per trade
# (brokerage 2 x Rs 20, STT 0.0625% on sell premium, exchange 0.03503% both
# sides, SEBI, stamp, 18% GST) divided by Rs 32.5 per point.
DEFAULT_COST_POINTS = 2.05

# Live execution stop is -20% of premium.  At a Rs 200 ATM premium and delta
# 0.5 that is 0.20 * 200 / 0.5 = 80 underlying points.  Using a fixed
# premium-equivalent stop keeps the shadow comparable to the live sleeve.
DEFAULT_STOP_POINTS = 80.0

OR_OPEN = time(9, 15)
OR_CLOSE = time(9, 30)
ENTRY_FLOOR = time(10, 0)
ENTRY_CUTOFF = time(14, 30)
HARD_FLAT = time(15, 10)
LAST_BAR = time(15, 25)

MIN_VOLUME_COVERAGE = 0.90
DEFAULT_CONFIRM_CLOSES = 2
DEFAULT_RETEST_TOLERANCE_ATR = 0.20
DEFAULT_MAX_RETEST_BARS = 12

# Variant E allows one entry per leg, so the absence of a daily cap would be a
# gap: a choppy session can re-arm repeatedly.  Two entries per session is the
# upper bound from the original risk requirement (1-2 quality trades/day).  On
# the 4 audited sessions it never bound - the busiest day produced 2 legs.
DEFAULT_MAX_TRADES = 2

VARIANTS = ("A", "B", "C", "D", "E")
VARIANT_LABELS = {
    "A": "ORB only",
    "B": "ORB + true VWAP",
    "C": "ORB + true VWAP + retest",
    "D": "ORB + true VWAP + retest + EMA20",
    "E": "ORB + true VWAP, one entry per leg, re-arm allowed",
}


class FuturesUnavailable(RuntimeError):
    """Raised when a futures series cannot be trusted for VWAP."""


# ---------------------------------------------------------------------------
# True VWAP - fail closed
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionVwap:
    """Result of a true session VWAP calculation.

    ``valid`` is False whenever the volume series cannot support a genuine
    VWAP.  Callers must reject the signal in that case; there is no fallback.
    """

    value: Optional[float]
    valid: bool
    reason: str
    volume_sum: float
    bars_with_volume: int
    bars_total: int

    @property
    def coverage(self) -> float:
        return (self.bars_with_volume / self.bars_total) if self.bars_total else 0.0


def compute_session_vwap(
    bars: Sequence[Mapping[str, Any]],
    *,
    min_coverage: float = MIN_VOLUME_COVERAGE,
) -> SessionVwap:
    """Return ``Sum(typical * volume) / Sum(volume)`` for one session.

    Fails closed (``valid=False`` with a machine-readable ``reason``) when
    there is no volume, no usable bars, or the volume coverage is too sparse.
    An equal-weighted average is never substituted.
    """
    priced_value = 0.0
    volume_sum = 0.0
    with_volume = 0
    total = 0
    for bar in bars:
        try:
            high = float(bar["high"])
            low = float(bar["low"])
            close = float(bar["close"])
            volume = float(bar.get("volume") or 0.0)
        except (KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(x) for x in (high, low, close, volume)):
            continue
        if close <= 0 or high < low:
            continue
        total += 1
        if volume > 0:
            typical = (high + low + close) / 3.0
            priced_value += typical * volume
            volume_sum += volume
            with_volume += 1

    if total == 0:
        return SessionVwap(None, False, "no_session_bars", 0.0, 0, 0)
    if volume_sum <= 0:
        return SessionVwap(None, False, "no_volume", 0.0, 0, total)
    coverage = with_volume / total
    if coverage < float(min_coverage):
        return SessionVwap(
            None, False, "low_volume_coverage", volume_sum, with_volume, total
        )
    return SessionVwap(
        priced_value / volume_sum, True, "", volume_sum, with_volume, total
    )


# ---------------------------------------------------------------------------
# Candle normalisation and indicators
# ---------------------------------------------------------------------------


def session_clock(stamp: datetime) -> time:
    return stamp.astimezone(IST).time()


def _as_ist(value: Any) -> Optional[datetime]:
    try:
        if isinstance(value, datetime):
            stamp = value
        else:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=IST)
    return stamp.astimezone(IST)


def normalise_bars(rows: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Normalise broker/yfinance rows into sorted IST candle dicts."""
    bars: List[Dict[str, Any]] = []
    for row in rows:
        stamp = _as_ist(row.get("date") or row.get("timestamp"))
        if stamp is None:
            continue
        try:
            bar = {
                "timestamp": stamp,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume") or 0.0),
            }
        except (KeyError, TypeError, ValueError):
            continue
        if bar["close"] <= 0 or bar["high"] < bar["low"]:
            continue
        bars.append(bar)
    bars.sort(key=lambda item: item["timestamp"])
    return bars


def add_indicators(bars: Sequence[Mapping[str, Any]],
                   ema_period: int = 20,
                   atr_period: int = 14) -> List[Dict[str, Any]]:
    """Return copies with causal ``ema20``, ``ema20_5ago`` and ``atr``."""
    enriched: List[Dict[str, Any]] = [dict(bar) for bar in bars]
    if not enriched:
        return enriched
    alpha = 2.0 / (ema_period + 1.0)
    ema: Optional[float] = None
    emas: List[float] = []
    for bar in enriched:
        close = float(bar["close"])
        ema = close if ema is None else close * alpha + ema * (1.0 - alpha)
        emas.append(ema)
    true_ranges: List[float] = []
    for index, bar in enumerate(enriched):
        if index == 0:
            span = float(bar["high"]) - float(bar["low"])
        else:
            previous = float(enriched[index - 1]["close"])
            span = max(
                float(bar["high"]) - float(bar["low"]),
                abs(float(bar["high"]) - previous),
                abs(float(bar["low"]) - previous),
            )
        true_ranges.append(span)
    for index, bar in enumerate(enriched):
        bar["ema20"] = emas[index]
        bar["ema20_5ago"] = emas[index - 5] if index >= 5 else None
        window = true_ranges[max(0, index - atr_period + 1): index + 1]
        bar["atr"] = sum(window) / len(window) if window else 0.0
    return enriched


def group_sessions(bars: Sequence[Mapping[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Group bars by IST date, keeping only regular-session bars."""
    sessions: Dict[str, List[Dict[str, Any]]] = {}
    for bar in bars:
        clock = session_clock(bar["timestamp"])
        if clock < OR_OPEN or clock > LAST_BAR:
            continue
        day = bar["timestamp"].astimezone(IST).date().isoformat()
        sessions.setdefault(day, []).append(dict(bar))
    for day in sessions:
        sessions[day].sort(key=lambda item: item["timestamp"])
    return sessions


def session_is_complete(session: Sequence[Mapping[str, Any]]) -> bool:
    """True when the last bar is at or after the 15:10 hard flat.

    A forward record must never freeze a partial session: the stop it sees at
    11:00 is not the stop the completed day would have produced.
    """
    if not session:
        return False
    return session_clock(session[-1]["timestamp"]) >= HARD_FLAT


# ---------------------------------------------------------------------------
# Signal engine
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TradePlan:
    day: str
    variant: str
    direction: str
    entry_time: str
    entry: float
    stop: float
    target: float
    risk_points: float
    exit_time: str
    exit: float
    exit_reason: str
    gross_points: float
    net_points: float
    net_inr: float
    r_multiple: float


def _consecutive(session: Sequence[Mapping[str, Any]], index: int,
                 level: float, above: bool) -> int:
    count = 0
    for position in range(index, -1, -1):
        close = float(session[position]["close"])
        if (close > level) if above else (close < level):
            count += 1
        else:
            break
    return count


def _clock_minutes(stamp: datetime) -> int:
    clock = session_clock(stamp)
    return clock.hour * 60 + clock.minute


def _opening_range(session: Sequence[Mapping[str, Any]]) -> Optional[Tuple[float, float]]:
    window = [
        bar for bar in session
        if session_clock(bar["timestamp"]) in (time(9, 15), time(9, 20), time(9, 25))
    ]
    if len(window) < 3:
        return None
    high = max(float(bar["high"]) for bar in window)
    low = min(float(bar["low"]) for bar in window)
    if high <= low:
        return None
    return high, low


def _vwap_path(session: Sequence[Mapping[str, Any]]) -> Tuple[List[Optional[float]], List[str]]:
    """Cumulative true VWAP per bar, plus the first failure reason."""
    values: List[Optional[float]] = []
    reason = ""
    for index in range(len(session)):
        result = compute_session_vwap(session[: index + 1])
        if result.valid:
            values.append(result.value)
        else:
            values.append(None)
            if not reason:
                reason = result.reason
    return values, reason


# Public aliases.  The live-entry audit reuses these exact functions so the
# audit and the replay can never disagree about what an opening range, a
# consecutive-close count, or a causal VWAP path is.
opening_range = _opening_range
consecutive_closes = _consecutive
vwap_path = _vwap_path


def _find_entry(
    session: Sequence[Mapping[str, Any]],
    vwap_path: Sequence[Optional[float]],
    *,
    variant: str,
    or_high: float,
    or_low: float,
    confirm_closes: int,
    retest_tolerance_atr: float,
    max_retest_bars: int,
) -> Optional[Tuple[int, str]]:
    if variant == "E":
        raise ValueError("variant E is multi-entry; use _leg_trades")
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant: {variant}")
    use_vwap = variant != "A"
    use_retest = variant in ("C", "D")
    use_ema = variant == "D"

    if not use_retest:
        for index, bar in enumerate(session):
            clock = _clock_minutes(bar["timestamp"])
            if clock < ENTRY_FLOOR.minute + ENTRY_FLOOR.hour * 60:
                continue
            if clock > ENTRY_CUTOFF.hour * 60 + ENTRY_CUTOFF.minute:
                return None
            close = float(bar["close"])
            vwap = vwap_path[index]
            for direction, above, level in (("long", True, or_high),
                                            ("short", False, or_low)):
                if _consecutive(session, index, level, above) < confirm_closes:
                    continue
                if use_vwap:
                    if vwap is None:
                        return None
                    if above and not close > vwap:
                        continue
                    if not above and not close < vwap:
                        continue
                return index, direction
        return None

    armed: Optional[Tuple[str, int]] = None
    for index, bar in enumerate(session):
        clock = _clock_minutes(bar["timestamp"])
        if clock < ENTRY_FLOOR.hour * 60 + ENTRY_FLOOR.minute:
            continue
        if clock > ENTRY_CUTOFF.hour * 60 + ENTRY_CUTOFF.minute:
            return None
        close = float(bar["close"])
        vwap = vwap_path[index]
        if armed is None:
            if _consecutive(session, index, or_high, True) >= confirm_closes:
                armed = ("long", index)
            elif _consecutive(session, index, or_low, False) >= confirm_closes:
                armed = ("short", index)
            continue
        direction, armed_at = armed
        if index - armed_at > max_retest_bars:
            armed = None
            continue
        if use_vwap and vwap is None:
            return None
        atr = float(bar.get("atr") or 0.0)
        tolerance = retest_tolerance_atr * atr
        if direction == "long":
            if close < or_low:
                armed = None
                continue
            touched = float(bar["low"]) <= or_high + tolerance
            resumed = (
                close > or_high
                and close > float(bar["open"])
                and (not use_vwap or (vwap is not None and close > vwap))
            )
            if not (touched and resumed):
                continue
            if use_ema:
                ema = bar.get("ema20")
                previous = bar.get("ema20_5ago")
                if not (ema is not None and previous is not None
                        and close > ema and ema > previous):
                    continue
            return index, direction
        if close > or_high:
            armed = None
            continue
        touched = float(bar["high"]) >= or_low - tolerance
        resumed = (
            close < or_low
            and close < float(bar["open"])
            and (not use_vwap or (vwap is not None and close < vwap))
        )
        if not (touched and resumed):
            continue
        if use_ema:
            ema = bar.get("ema20")
            previous = bar.get("ema20_5ago")
            if not (ema is not None and previous is not None
                    and close < ema and ema < previous):
                continue
        return index, direction
    return None


def _simulate_position(
    session: Sequence[Mapping[str, Any]],
    entry_index: int,
    direction: str,
    *,
    stop_points: float,
    reward_risk: float,
    cost_points: float,
) -> Optional[Tuple[Dict[str, Any], int]]:
    """Simulate one position and return ``(outcome, exit_bar_index)``.

    The exit index lets the per-leg variant know where the next leg may start,
    so it never opens a second position while the first is still live.
    """
    entry = float(session[entry_index]["close"])
    if stop_points <= 0:
        return None
    if direction == "long":
        stop = entry - stop_points
        target = entry + reward_risk * stop_points
    else:
        stop = entry + stop_points
        target = entry - reward_risk * stop_points
    exit_price: Optional[float] = None
    exit_reason = ""
    exit_time = ""
    exit_index = len(session) - 1
    for position in range(entry_index + 1, len(session)):
        bar = session[position]
        clock = _clock_minutes(bar["timestamp"])
        if clock > HARD_FLAT.hour * 60 + HARD_FLAT.minute:
            previous = session[position - 1]
            exit_price = float(previous["close"])
            exit_reason = "hard_flat"
            exit_time = previous["timestamp"].astimezone(IST).strftime("%H:%M")
            exit_index = position - 1
            break
        if direction == "long":
            if float(bar["low"]) <= stop:
                exit_price, exit_reason = stop, "stop"
            elif float(bar["high"]) >= target:
                exit_price, exit_reason = target, "target"
        else:
            if float(bar["high"]) >= stop:
                exit_price, exit_reason = stop, "stop"
            elif float(bar["low"]) <= target:
                exit_price, exit_reason = target, "target"
        if exit_price is not None:
            exit_time = bar["timestamp"].astimezone(IST).strftime("%H:%M")
            exit_index = position
            break
    if exit_price is None:
        last = session[-1]
        exit_price = float(last["close"])
        exit_reason = "session_end"
        exit_time = last["timestamp"].astimezone(IST).strftime("%H:%M")
        exit_index = len(session) - 1

    gross = (exit_price - entry) if direction == "long" else (entry - exit_price)
    net = gross - cost_points
    return {
        "direction": direction,
        "entry_time": session[entry_index]["timestamp"].astimezone(IST).strftime("%H:%M"),
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "target": round(target, 2),
        "exit_time": exit_time,
        "exit": round(exit_price, 2),
        "exit_reason": exit_reason,
        "gross_points": round(gross, 2),
        "net_points": round(net, 2),
        "net_inr": round(net * POINT_VALUE_INR, 2),
        "r_multiple": round(net / stop_points, 3),
    }, exit_index


def _simulate(
    session: Sequence[Mapping[str, Any]],
    entry_index: int,
    direction: str,
    *,
    stop_points: float,
    reward_risk: float,
    cost_points: float,
) -> Optional[Dict[str, Any]]:
    """Single-position wrapper kept for the A-D variants and their tests."""
    result = _simulate_position(
        session, entry_index, direction,
        stop_points=stop_points,
        reward_risk=reward_risk,
        cost_points=cost_points,
    )
    return None if result is None else result[0]


def _leg_trades(
    session: Sequence[Mapping[str, Any]],
    vwap_path: Sequence[Optional[float]],
    or_high: float,
    or_low: float,
    *,
    confirm_closes: int,
    stop_points: float,
    reward_risk: float,
    cost_points: float,
    max_trades: int,
) -> Tuple[List[Dict[str, Any]], str]:
    """One entry per leg, re-arm allowed, at most ``max_trades`` per session.

    A leg ends when price closes back inside the opening range; only then may a
    new leg start with its own confirmed breakout.  A second breakout without a
    re-arm is refused — that is exactly the repeat-entry defect the live book
    exhibits, and the one thing this variant is meant to measure.
    """
    trades: List[Dict[str, Any]] = []
    reason = ""
    need_rearm = False
    index = 0
    floor = ENTRY_FLOOR.hour * 60 + ENTRY_FLOOR.minute
    cutoff = ENTRY_CUTOFF.hour * 60 + ENTRY_CUTOFF.minute
    while index < len(session):
        if max_trades and len(trades) >= max_trades:
            break
        bar = session[index]
        clock = _clock_minutes(bar["timestamp"])
        if clock > cutoff:
            break
        if clock < floor:
            index += 1
            continue
        close = float(bar["close"])
        if need_rearm:
            if or_low < close < or_high:
                need_rearm = False
            else:
                index += 1
                continue
        direction = None
        if _consecutive(session, index, or_high, True) >= confirm_closes:
            direction = "long"
        elif _consecutive(session, index, or_low, False) >= confirm_closes:
            direction = "short"
        if direction is None:
            index += 1
            continue
        vwap = vwap_path[index]
        if vwap is None:
            reason = "vwap_unavailable"
            break
        if direction == "long" and not close > vwap:
            index += 1
            continue
        if direction == "short" and not close < vwap:
            index += 1
            continue
        outcome = _simulate_position(
            session, index, direction,
            stop_points=stop_points,
            reward_risk=reward_risk,
            cost_points=cost_points,
        )
        if outcome is None:
            break
        trade, exit_index = outcome
        trades.append(trade)
        index = exit_index + 1
        need_rearm = True
    return trades, reason


def evaluate_session(
    bars: Sequence[Mapping[str, Any]],
    *,
    day: str,
    variant: str,
    reward_risk: float = 2.0,
    stop_points: float = DEFAULT_STOP_POINTS,
    cost_points: float = DEFAULT_COST_POINTS,
    confirm_closes: int = DEFAULT_CONFIRM_CLOSES,
    retest_tolerance_atr: float = DEFAULT_RETEST_TOLERANCE_ATR,
    max_retest_bars: int = DEFAULT_MAX_RETEST_BARS,
    max_trades: int = DEFAULT_MAX_TRADES,
) -> Dict[str, Any]:
    """Evaluate one session for one variant.

    Returns a record with ``status`` of ``ok`` (a trade was planned),
    ``no_signal`` (no qualifying entry) or ``data_issue`` (VWAP could not be
    computed and the variant needs it).
    """
    enriched = add_indicators(group_sessions(bars).get(day, []))
    record: Dict[str, Any] = {
        "kind": "shadow_orb_vwap",
        "day": day,
        "variant": variant,
        "variant_label": VARIANT_LABELS[variant],
        "reward_risk": reward_risk,
        "stop_points": stop_points,
        "cost_points": cost_points,
        "status": "no_signal",
        "data_issue": "",
        "source": "forward",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    if not enriched:
        record["status"] = "data_issue"
        record["data_issue"] = "no_session_bars"
        return record

    opening = _opening_range(enriched)
    if opening is None:
        record["status"] = "data_issue"
        record["data_issue"] = "incomplete_opening_range"
        return record
    or_high, or_low = opening

    vwap_path, vwap_reason = _vwap_path(enriched)
    last_vwap = next((value for value in reversed(vwap_path) if value is not None), None)
    last_bar = enriched[-1]
    record.update({
        "or_high": round(or_high, 2),
        "or_low": round(or_low, 2),
        "or_width": round(or_high - or_low, 2),
        "vwap_close": None if last_vwap is None else round(last_vwap, 2),
        "ema20_close": round(float(last_bar["ema20"]), 2),
        "atr_close": round(float(last_bar["atr"]), 2),
        "volume_coverage": round(
            compute_session_vwap(enriched).coverage, 3
        ),
    })

    needs_vwap = variant != "A"
    if needs_vwap and last_vwap is None:
        record["status"] = "data_issue"
        record["data_issue"] = vwap_reason or "vwap_unavailable"
        return record

    record["vwap_binding"] = _vwap_would_block(
        enriched, vwap_path, or_high, or_low, confirm_closes
    )

    if variant == "E":
        trades, reason = _leg_trades(
            enriched, vwap_path, or_high, or_low,
            confirm_closes=confirm_closes,
            stop_points=stop_points,
            reward_risk=reward_risk,
            cost_points=cost_points,
            max_trades=max_trades,
        )
        record["max_trades"] = max_trades
        if not trades:
            if reason:
                record["status"] = "data_issue"
                record["data_issue"] = reason
            return record
        record["status"] = "ok"
        record["trade_count"] = len(trades)
        record["trades"] = [
            TradePlan(day=day, variant=variant, risk_points=stop_points,
                      **trade).__dict__
            for trade in trades
        ]
        record["net_points"] = round(
            sum(float(t["net_points"]) for t in trades), 2
        )
        record["net_inr"] = round(record["net_points"] * POINT_VALUE_INR, 2)
        return record

    entry = _find_entry(
        enriched, vwap_path,
        variant=variant,
        or_high=or_high,
        or_low=or_low,
        confirm_closes=confirm_closes,
        retest_tolerance_atr=retest_tolerance_atr,
        max_retest_bars=max_retest_bars,
    )
    if entry is None:
        return record

    index, direction = entry
    record["vwap_at_entry"] = (
        None if vwap_path[index] is None else round(float(vwap_path[index]), 2)
    )
    outcome = _simulate(
        enriched, index, direction,
        stop_points=stop_points,
        reward_risk=reward_risk,
        cost_points=cost_points,
    )
    if outcome is None:
        return record
    record["status"] = "ok"
    record["trade"] = TradePlan(
        day=day, variant=variant, risk_points=stop_points, **outcome
    ).__dict__
    return record


def _vwap_would_block(
    session: Sequence[Mapping[str, Any]],
    vwap_path: Sequence[Optional[float]],
    or_high: float,
    or_low: float,
    confirm_closes: int,
) -> bool:
    """True when the first VWAP-free ORB entry sits on the wrong side of VWAP."""
    for index, bar in enumerate(session):
        clock = _clock_minutes(bar["timestamp"])
        if clock < ENTRY_FLOOR.hour * 60 + ENTRY_FLOOR.minute:
            continue
        if clock > ENTRY_CUTOFF.hour * 60 + ENTRY_CUTOFF.minute:
            return False
        close = float(bar["close"])
        vwap = vwap_path[index]
        if _consecutive(session, index, or_high, True) >= confirm_closes:
            return vwap is None or close <= vwap
        if _consecutive(session, index, or_low, False) >= confirm_closes:
            return vwap is None or close >= vwap
    return False


# ---------------------------------------------------------------------------
# Futures data adapter
# ---------------------------------------------------------------------------


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:  # pragma: no cover - optional dependency
        return


def _interval(timeframe: str) -> str:
    mapping = {
        "1m": "minute", "5m": "5minute", "15m": "15minute",
        "30m": "30minute", "1h": "60minute", "1d": "day",
    }
    return mapping.get(str(timeframe).lower(), "5minute")


def load_futures_bars(
    underlying: str = "NIFTY",
    *,
    timeframe: str = "5m",
    period: str = "30d",
) -> Tuple[str, List[Dict[str, Any]]]:
    """Fetch the nearest futures series and normalise it.

    Raises ``FuturesUnavailable`` when there is no contract, no rows, or no
    volume.  There is no spot fallback: a zero-volume series cannot produce a
    true VWAP.
    """
    _load_env()
    try:
        from src.zerodha_market_data import ZerodhaMarketData
    except Exception as exc:  # pragma: no cover - import guard
        raise FuturesUnavailable(f"broker adapter unavailable: {exc}") from exc

    adapter = ZerodhaMarketData.from_env()
    try:
        rows = adapter.instruments("NFO")
    except Exception as exc:
        raise FuturesUnavailable(f"NFO instrument dump failed: {exc}") from exc
    contracts = [
        row for row in rows
        if str(row.get("name", "")).upper() == underlying.upper()
        and str(row.get("instrument_type", "")).upper() == "FUT"
    ]
    if not contracts:
        raise FuturesUnavailable(f"no {underlying} futures listed")
    nearest = sorted(contracts, key=lambda row: str(row.get("expiry")))[0]
    symbol = str(nearest.get("tradingsymbol") or "")
    try:
        raw = adapter.historical("NFO", symbol, _interval(timeframe), period)
    except Exception as exc:
        raise FuturesUnavailable(f"historical fetch failed for {symbol}: {exc}") from exc
    bars = normalise_bars(raw or [])
    if not bars:
        raise FuturesUnavailable(f"no candles returned for {symbol}")
    if not any(bar["volume"] > 0 for bar in bars):
        raise FuturesUnavailable(f"{symbol} candles carry zero volume")
    return symbol, bars


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def default_ledger_path() -> Path:
    return ROOT / "output" / "shadow_orb_vwap.jsonl"


def _key(record: Mapping[str, Any]) -> Tuple[str, str, str]:
    return (
        str(record.get("day")),
        str(record.get("variant")),
        str(record.get("source", "forward")),
    )


def read_records(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    path = Path(path or default_ledger_path())
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def append_record(record: Mapping[str, Any],
                  path: Optional[Path] = None) -> bool:
    """Append unless ``(day, variant, source)`` already exists."""
    path = Path(path or default_ledger_path())
    existing = {_key(item) for item in read_records(path)}
    if _key(record) in existing:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(record), default=str) + "\n")
    return True


def summarise(
    records: Iterable[Mapping[str, Any]],
    *,
    variant: Optional[str] = None,
    source: Optional[str] = None,
) -> Dict[str, Any]:
    rows = [
        row for row in records
        if (variant is None or row.get("variant") == variant)
        and (source is None or str(row.get("source", "forward")) == source)
    ]
    sessions = {str(row.get("day")) for row in rows if row.get("status") != "data_issue"}
    data_issues = sum(1 for row in rows if row.get("status") == "data_issue")
    trades = []
    for row in rows:
        if row.get("status") != "ok":
            continue
        if row.get("trade"):
            trades.append(row["trade"])
        trades.extend(row.get("trades") or [])
    if not trades:
        return {
            "variant": variant,
            "sessions": len(sessions),
            "data_issues": data_issues,
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_pct": 0.0,
            "gross_points": 0.0,
            "net_points": 0.0,
            "net_inr": 0.0,
            "max_drawdown_points": 0.0,
            "avg_win_points": 0.0,
            "avg_loss_points": 0.0,
            "profit_factor": None,
            "reward_risk_realised": None,
            "vwap_binding": 0,
        }
    net = [float(trade["net_points"]) for trade in trades]
    wins = [value for value in net if value > 0]
    losses = [value for value in net if value <= 0]
    equity = peak = drawdown = 0.0
    for value in net:
        equity += value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    return {
        "variant": variant,
        "sessions": len(sessions),
        "data_issues": data_issues,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_pct": round(100.0 * len(wins) / len(trades), 1),
        "gross_points": round(sum(float(t["gross_points"]) for t in trades), 2),
        "net_points": round(sum(net), 2),
        "net_inr": round(sum(net) * POINT_VALUE_INR, 2),
        "max_drawdown_points": round(drawdown, 2),
        "avg_win_points": round(avg_win, 2),
        "avg_loss_points": round(avg_loss, 2),
        "profit_factor": (round(sum(wins) / abs(sum(losses)), 2)
                          if losses and sum(losses) != 0 else None),
        "reward_risk_realised": (round(avg_win / abs(avg_loss), 2)
                                 if avg_loss else None),
        "vwap_binding": sum(1 for row in rows if row.get("vwap_binding")),
    }


PROMOTION_MIN_SESSIONS = 10
PROMOTION_MIN_TRADES = 8
PROMOTION_MAX_DRAWDOWN_POINTS = 150.0


def promotion_status(records: Iterable[Mapping[str, Any]],
                     *,
                     variant: str) -> Dict[str, Any]:
    """Check the pre-registered promotion gate for one variant.

    Only ``source='forward'`` records count.  Backfilled sessions seed the
    ledger for context but can never satisfy the gate.
    """
    stats = summarise(records, variant=variant, source="forward")
    checks = {
        "forward_sessions": (stats["sessions"] >= PROMOTION_MIN_SESSIONS,
                             f'{stats["sessions"]}/{PROMOTION_MIN_SESSIONS}'),
        "trades": (stats["trades"] >= PROMOTION_MIN_TRADES,
                   f'{stats["trades"]}/{PROMOTION_MIN_TRADES}'),
        "positive_net": (stats["net_points"] > 0,
                         f'{stats["net_points"]:+.2f} pts'),
        "drawdown_ok": (
            abs(stats["max_drawdown_points"]) <= PROMOTION_MAX_DRAWDOWN_POINTS,
            f'{stats["max_drawdown_points"]:.2f} pts '
            f'(limit {PROMOTION_MAX_DRAWDOWN_POINTS:.0f})',
        ),
    }
    return {
        "variant": variant,
        "eligible": all(ok for ok, _ in checks.values()),
        "checks": {name: {"met": ok, "detail": detail}
                   for name, (ok, detail) in checks.items()},
        "stats": stats,
    }


__all__ = [
    "ATM_DELTA", "DEFAULT_CONFIRM_CLOSES", "DEFAULT_COST_POINTS",
    "DEFAULT_MAX_RETEST_BARS", "DEFAULT_MAX_TRADES",
    "DEFAULT_RETEST_TOLERANCE_ATR",
    "DEFAULT_STOP_POINTS", "FuturesUnavailable", "LOT_SIZE",
    "MIN_VOLUME_COVERAGE", "POINT_VALUE_INR", "PROMOTION_MAX_DRAWDOWN_POINTS",
    "PROMOTION_MIN_SESSIONS", "PROMOTION_MIN_TRADES", "SessionVwap",
    "TradePlan", "VARIANTS", "VARIANT_LABELS", "add_indicators",
    "append_record", "compute_session_vwap", "consecutive_closes",
    "default_ledger_path", "evaluate_session", "group_sessions",
    "load_futures_bars", "normalise_bars", "opening_range",
    "promotion_status", "read_records", "session_clock", "session_is_complete",
    "summarise",
    "vwap_path",
]
