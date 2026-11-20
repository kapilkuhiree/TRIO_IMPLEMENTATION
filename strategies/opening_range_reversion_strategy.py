"""TRIO - Opening Range Reversion Scalp (ORRS).

The complement of :class:`strategies.orb_strategy.OrbStrategy`.  ORB is paid
when the opening-range break *holds*; ORRS looks for the opposite event - a
break that fails and closes back inside the range - and fades it toward the
range.

Rule set (frozen from the 60-session NIFTY/BANKNIFTY study, poke 0.15):

1. Opening range = 09:15, 09:20 and 09:25 bars (high/low, no close term).
2. Range width ``W`` must be 0.08%-1.00% of the 09:25 close.
3. A later completed bar must poke ``0.15 * W`` beyond the range and then
   close back inside it:
       high > ORH + 0.15W  and  close < ORH   -> PE (fade the failed up-break)
       low  < ORL - 0.15W  and  close > ORL   -> CE (fade the failed down-break)
4. Entry is the next bar's open (ATM long option, handled by the planner).
5. Entries stop at 14:00.

Exits are NOT the underlying levels in this module.  The validated profile is a
22% premium target, a 32% premium stop, a 25% invalidation (close beyond the
range by ``0.25 * W``) and a 45-minute time stop.  Those live in
``ORRS_EXIT_PROFILE`` so the replay/execution layers share one definition.
``stop_loss``/``target`` below are structural *underlying* references only;
the options runtime prices its own premium levels from ``mode_cfg``.

This module is signal-only and stateless.  It never counts trades and never
enforces the 2-per-session cap: the live runtime's caps apply it, and shadow
replay must post-filter to the first two signals per index per session.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, List

IST = timezone(timedelta(hours=5, minutes=30))

# Opening range: the three completed 09:15/09:20/09:25 bars.
OPENING_TIMES = frozenset({time(9, 15), time(9, 20), time(9, 25)})
OR_CLOSE = time(9, 30)

# Signal geometry, frozen from the validated configuration.
POKE_FRACTION = 0.15          # penetration required beyond the range
RECLAIM_FRACTION = 0.0        # distance back inside the range on the close
INVALIDATION_FRACTION = 0.25  # thesis dead beyond range +/- this x W
WIDTH_MIN_FRACTION = 0.0008   # range width floor, as a fraction of spot
WIDTH_MAX_FRACTION = 0.0100   # range width ceiling
ENTRY_CUTOFF = time(14, 0)

# Validated exit profile.  Kept here so replay and any future execution layer
# read the same numbers rather than re-deriving them.
ORRS_EXIT_PROFILE = {
    "target_fraction": 0.22,
    "stop_fraction": 0.32,
    "invalidation_fraction": INVALIDATION_FRACTION,
    "time_stop_minutes": 45,
    "max_per_index_per_session": 2,
}


@dataclass
class OrrsSignal:
    """Result of one ORRS evaluation."""

    allowed: bool = False
    strategy: str = "opening_range_reversion"
    variant: str = "bullish"      # "bullish" | "bearish"
    option_type: str = "CE"       # "CE" fades a failed down-break, "PE" a failed up-break
    entry_price: float = 0.0      # underlying close on the signal bar
    stop_loss: float = 0.0        # invalidation level (underlying reference)
    target: float = 0.0           # structural range level (underlying reference)
    reasoning: str = ""
    bar_index: int = -1
    underlying_symbol: str = "NIFTY"
    or_high: float = 0.0
    or_low: float = 0.0
    or_width: float = 0.0
    poke_price: float = 0.0


class OpeningRangeReversionStrategy:
    """Failed opening-range break, faded back toward the range.

    The trigger is an event on a single completed bar: the bar must both
    penetrate the range and close back inside it.  A bar that merely sits
    inside the range never signals, so the condition cannot persist across
    scans the way a state-based trigger would.
    """

    def evaluate_signal(
        self,
        candles_5min: List[Dict[str, Any]],
        index: int,
        contract_specs: Dict[str, Any] | None = None,
        underlying_symbol: str = "NIFTY",
    ) -> OrrsSignal:
        # ``contract_specs`` is accepted for parity with OrbStrategy so the
        # shared pipeline can call every strategy the same way.  ORRS needs no
        # contract parameters: the planner picks the ATM strike.
        del contract_specs

        result = OrrsSignal()
        result.underlying_symbol = underlying_symbol

        try:
            if index < 3 or index >= len(candles_5min):
                return result

            current_candle = candles_5min[index]
            instrument_type = current_candle.get("instrument_type")
            if (
                not underlying_symbol
                or current_candle.get("underlying_symbol") != underlying_symbol
                or current_candle.get("exchange") != "NSE"
                or instrument_type not in {"index", "stock"}
            ):
                return result

            current_ts = _ist_timestamp(current_candle.get("timestamp", ""))
            if current_ts is None:
                return result
            # The range needs all three opening bars to be complete.
            if current_ts.time() < OR_CLOSE:
                return result
            if current_ts.time() > ENTRY_CUTOFF:
                result.reasoning = "entry past 14:00 IST cutoff"
                return result

            or_high, or_low, or_close, or_bars = _opening_range(
                candles_5min[:index], current_ts, underlying_symbol, instrument_type
            )
            if or_bars < 3:
                return result

            width = or_high - or_low
            if width <= 0 or or_close <= 0:
                return result
            width_fraction = width / or_close
            if not (WIDTH_MIN_FRACTION <= width_fraction <= WIDTH_MAX_FRACTION):
                return result

            try:
                high = float(current_candle["high"])
                low = float(current_candle["low"])
                close = float(current_candle["close"])
            except (KeyError, TypeError, ValueError):
                return result
            if close <= 0:
                return result

            result.or_high = or_high
            result.or_low = or_low
            result.or_width = width
            result.bar_index = index
            result.entry_price = close

            # Failed upside break: poke above the range, close back inside.
            if high > or_high + POKE_FRACTION * width and close < or_high - RECLAIM_FRACTION * width:
                _populate(
                    result,
                    variant="bearish",
                    option_type="PE",
                    structural_target=_fade_target(or_low, or_high, close, bearing=-1),
                    stop_loss=or_high + INVALIDATION_FRACTION * width,
                    poke_price=or_high + POKE_FRACTION * width,
                    side="above",
                    width=width,
                    width_fraction=width_fraction,
                )
                return result

            # Failed downside break: poke below the range, close back inside.
            if low < or_low - POKE_FRACTION * width and close > or_low + RECLAIM_FRACTION * width:
                _populate(
                    result,
                    variant="bullish",
                    option_type="CE",
                    structural_target=_fade_target(or_low, or_high, close, bearing=1),
                    stop_loss=or_low - INVALIDATION_FRACTION * width,
                    poke_price=or_low - POKE_FRACTION * width,
                    side="below",
                    width=width,
                    width_fraction=width_fraction,
                )
                return result
        except (TypeError, ValueError, AttributeError):
            return OrrsSignal(underlying_symbol=underlying_symbol)

        return result


def _ist_timestamp(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=IST)
    return stamp.astimezone(IST)


def _opening_range(
    prior: List[Dict[str, Any]],
    current_ts: datetime,
    underlying_symbol: str,
    instrument_type: Any,
) -> tuple[float, float, float, int]:
    """Return (high, low, 09:25 close, bar count) for the session's OR."""
    or_high = float("-inf")
    or_low = float("inf")
    or_close = 0.0
    bars = 0
    for candle in prior:
        if candle.get("underlying_symbol") != underlying_symbol:
            continue
        if (
            candle.get("exchange") != "NSE"
            or candle.get("instrument_type") != instrument_type
        ):
            continue
        stamp = _ist_timestamp(candle.get("timestamp", ""))
        if stamp is None or stamp.date() != current_ts.date():
            continue
        if stamp.time() not in OPENING_TIMES:
            continue
        try:
            high = float(candle["high"])
            low = float(candle["low"])
        except (KeyError, TypeError, ValueError):
            continue
        or_high = max(or_high, high)
        or_low = min(or_low, low)
        bars += 1
        if stamp.time() == time(9, 25):
            try:
                or_close = float(candle["close"])
            except (KeyError, TypeError, ValueError):
                or_close = 0.0
    if bars < 3:
        return 0.0, 0.0, 0.0, bars
    return or_high, or_low, or_close, bars


def _fade_target(or_low: float, or_high: float, entry: float, *, bearing: int) -> float:
    """Structural target: the range midpoint, kept on the profitable side.

    The validated exits are premium-fraction based, so this underlying level is
    a reference rather than the real exit.  Clamping keeps it directionally
    sane when a failed break has already travelled most of the range.
    """
    midpoint = (or_low + or_high) / 2.0
    if bearing < 0:                       # PE: target must sit below entry
        return min(midpoint, entry * 0.999)
    return max(midpoint, entry * 1.001)   # CE: target must sit above entry


def _populate(
    result: OrrsSignal,
    *,
    variant: str,
    option_type: str,
    structural_target: float,
    stop_loss: float,
    poke_price: float,
    side: str,
    width: float,
    width_fraction: float,
) -> None:
    result.allowed = True
    result.variant = variant
    result.option_type = option_type
    result.target = structural_target
    result.stop_loss = stop_loss
    result.poke_price = poke_price
    result.reasoning = (
        f"Failed opening-range break {side} {POKE_FRACTION:.0%}W "
        f"(W {width:.1f} pts, {width_fraction:.2%} of spot); "
        f"fade with {option_type}, invalidation {INVALIDATION_FRACTION:.0%}W away, "
        f"{ORRS_EXIT_PROFILE['time_stop_minutes']}-minute time stop"
    )


__all__ = [
    "OpeningRangeReversionStrategy",
    "OrrsSignal",
    "ORRS_EXIT_PROFILE",
    "POKE_FRACTION",
    "INVALIDATION_FRACTION",
    "WIDTH_MIN_FRACTION",
    "WIDTH_MAX_FRACTION",
    "ENTRY_CUTOFF",
]
