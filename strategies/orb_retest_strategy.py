"""TRIO - ORB-Retest v2 Strategy Module

The legacy ORB strategy enters on the breakout close.  The signal diagnostic
over 59 sessions showed that the *worst* bucket was a full-body breakout
thrust (win rate 34.6%, PF 0.70), while the best buckets were wide opening
ranges, a 09:45-10:30 entry window, and weak/indecisive breakout bars.

ORB-Retest v2 replaces "chase the breakout" with:

    breakout -> pullback/retest of the opening-range level -> resumption

A failed breakout never produces a resumption, so the false-break is filtered
structurally instead of by a blanket re-entry timer.

The implementation is deliberately *stateless*: every call reconstructs the
session state machine from the candle history up to ``index``.  This matters
because the production planner constructs a fresh strategy instance for each
completed bar (``CandidatePipeline.evaluate_strategy_signals_pure``); any
instance-level counters would silently reset and never confirm.  A stateless
replay makes backtest and production behaviour identical.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

IST = timezone(timedelta(hours=5, minutes=30))


def _parse_ts(value: Any) -> Optional[datetime]:
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=IST)
    return ts.astimezone(IST)


@dataclass
class OrbRetestSignal:
    """Result from ORB-Retest v2 signal evaluation."""

    allowed: bool = False
    strategy: str = "orb_retest"
    variant: str = "bullish"  # "bullish" | "bearish"
    signal_type: str = "none"  # "retest_resumption" | "none"
    entry_price: float = 0.0
    trigger_price: float = 0.0
    trigger_time: str = ""
    expiry: str = "weekly"
    option_type: str = "CE"
    target: float = 0.0
    stop_loss: float = 0.0
    reasoning: str = ""
    bar_index: int = -1
    underlying_symbol: str = "NIFTY"
    breakout_bar_index: int = -1
    retest_bar_index: int = -1


class OrbRetestStrategy:
    """Breakout -> retest -> resumption strategy.

    Gates (all measured on data available at the signal bar):
      * opening-range width >= ``or_min_width_atr`` x ATR14
      * ATR14 as % of spot inside ``[atr_pct_min, atr_pct_max]``
      * entries only inside the configured session windows (default
        09:45-11:00 and 13:45-14:30 IST)
      * optional VWAP alignment
      * the initial breakout bar must not be an exhaustion thrust
        (``body <= max_breakout_body``)

    Setup:
      * a completed 5m close beyond the opening-range level arms the setup
      * price must trade back within ``retest_tolerance_atr`` x ATR14 of the
        level (the retest)
      * a later completed close back beyond the level is the resumption
        entry; a retest bar that itself closes back beyond the level also
        qualifies

    Re-entry:
      * after a setup is consumed, the strategy stays consumed until price
        closes back inside the opening range.  A fresh breakout then has to
        arm a new setup.  There is no blanket time cooldown in the strategy;
        the runtime's loss-only guard remains the operational backstop.
    """

    # Opening range 09:15-09:30 IST (3 completed 5m bars)
    OR_OPEN = dtime(9, 15)
    OR_CLOSE = dtime(9, 30)
    ENTRY_CUTOFF = dtime(14, 30)
    ATR_PERIOD = 14
    MINUTE_OFFSET = 9 * 60 + 15  # 09:15 in minutes

    # Default session windows expressed as minutes since 09:15.
    # 09:45-11:00 -> (30, 105); 13:45-14:30 -> (270, 315)
    DEFAULT_WINDOWS: Tuple[Tuple[int, int], ...] = ((30, 105), (270, 315))

    def __init__(
        self,
        lot_size: int = 65,
        fill_model: str = "bidask",
        or_min_width_atr: float = 3.0,
        or_max_width_atr: float = 14.0,
        atr_pct_min: float = 0.07,
        atr_pct_max: float = 0.30,
        retest_tolerance_atr: float = 0.20,
        retest_window_bars: int = 6,
        max_breakout_body: float = 0.90,
        resumption_min_body: float = 0.15,
        resumption_buffer_atr: float = 0.20,
        stop_atr: float = 1.0,
        target_r: float = 2.0,
        require_vwap: bool = False,
        session_windows: Optional[Tuple[Tuple[int, int], ...]] = None,
        earliest_minute: int = 30,
    ) -> None:
        self.lot_size = lot_size
        self.fill_model = fill_model
        self.or_min_width_atr = max(0.0, float(or_min_width_atr))
        self.or_max_width_atr = max(self.or_min_width_atr, float(or_max_width_atr))
        self.atr_pct_min = max(0.0, float(atr_pct_min))
        self.atr_pct_max = max(self.atr_pct_min, float(atr_pct_max))
        self.retest_tolerance_atr = max(0.0, float(retest_tolerance_atr))
        self.retest_window_bars = max(1, int(retest_window_bars))
        self.max_breakout_body = min(1.0, max(0.0, float(max_breakout_body)))
        self.resumption_min_body = min(1.0, max(0.0, float(resumption_min_body)))
        self.resumption_buffer_atr = max(0.0, float(resumption_buffer_atr))
        self.stop_atr = max(0.0, float(stop_atr))
        self.target_r = max(0.5, float(target_r))
        self.require_vwap = bool(require_vwap)
        self.session_windows = tuple(session_windows or self.DEFAULT_WINDOWS)
        self.earliest_minute = max(0, int(earliest_minute))

    # ------------------------------------------------------------------
    # Public signal boundary (same shape as OrbStrategy)
    # ------------------------------------------------------------------

    def evaluate_signal(
        self,
        candles_5min: List[Dict[str, Any]],
        index: int,
        contract_specs: Dict[str, Any],
        underlying_symbol: str = "NIFTY",
    ) -> OrbRetestSignal:
        result = OrbRetestSignal()
        result.strategy = "orb_retest"
        result.underlying_symbol = underlying_symbol

        if not isinstance(candles_5min, (list, tuple)):
            return result
        if index < 3 or index >= len(candles_5min):
            return result

        current = candles_5min[index] or {}
        if (
            not underlying_symbol
            or current.get("underlying_symbol") != underlying_symbol
            or current.get("exchange") != "NSE"
            or current.get("instrument_type") not in {"index", "stock"}
        ):
            return result

        current_ts = _parse_ts(current.get("timestamp"))
        if current_ts is None or current_ts.time() < self.OR_CLOSE:
            return result

        minute = self._minute_of_day(current_ts)
        if minute < self.earliest_minute or not self._in_window(minute):
            return result
        if current_ts.time() > self.ENTRY_CUTOFF:
            return result

        session = self._session_indices(candles_5min, index, underlying_symbol)
        if len(session) < 4:
            return result

        or_bar_indices = [
            j for j in session
            if (_parse_ts(candles_5min[j].get("timestamp")) or current_ts).time()
            in {dtime(9, 15), dtime(9, 20), dtime(9, 25)}
        ]
        if len(or_bar_indices) < 3:
            return result

        or_high = max(float(candles_5min[j]["high"]) for j in or_bar_indices)
        or_low = min(float(candles_5min[j]["low"]) for j in or_bar_indices)
        try:
            spot = float(current["close"])
        except (KeyError, TypeError, ValueError):
            return result
        if spot <= 0 or or_high <= or_low:
            return result

        atr = self._atr(candles_5min, index)
        if atr <= 0:
            return result

        or_width_atr = (or_high - or_low) / atr
        atr_pct = atr / spot * 100.0
        if or_width_atr < self.or_min_width_atr or or_width_atr > self.or_max_width_atr:
            return result
        if atr_pct < self.atr_pct_min or atr_pct > self.atr_pct_max:
            return result

        # The current bar itself must be a completed, directional resumption.
        body = self._body_ratio(current)
        close = spot
        bar_open = float(current.get("open") or 0.0)

        setup = self._replay(
            candles_5min,
            session,
            or_high,
            or_low,
            atr,
            index,
        )
        if setup is None:
            return result

        direction, breakout_idx, retest_idx = setup
        if index <= breakout_idx:
            return result

        buffer = self.resumption_buffer_atr * atr
        if direction == "bullish":
            if close <= or_high + buffer:
                return result
            if body < self.resumption_min_body or close <= bar_open:
                return result
        else:
            if close >= or_low - buffer:
                return result
            if body < self.resumption_min_body or close >= bar_open:
                return result

        if self.require_vwap:
            vwap = self._session_vwap(candles_5min, session)
            if vwap is not None:
                if direction == "bullish" and close < vwap:
                    return result
                if direction == "bearish" and close > vwap:
                    return result

        # Stop beyond the retest extreme, target a fixed R multiple.
        if direction == "bullish":
            retest_low = min(
                float(candles_5min[j]["low"])
                for j in range(breakout_idx, retest_idx + 1)
            )
            stop = retest_low - self.stop_atr * atr
            risk = close - stop
            if risk <= 0:
                return result
            target = close + self.target_r * risk
            result.option_type = "CE"
            result.variant = "bullish"
            result.signal_type = "retest_resumption"
            result.trigger_price = or_high
        else:
            retest_high = max(
                float(candles_5min[j]["high"])
                for j in range(breakout_idx, retest_idx + 1)
            )
            stop = retest_high + self.stop_atr * atr
            risk = stop - close
            if risk <= 0:
                return result
            target = close - self.target_r * risk
            result.option_type = "PE"
            result.variant = "bearish"
            result.signal_type = "retest_resumption"
            result.trigger_price = or_low

        result.allowed = True
        result.entry_price = close
        result.stop_loss = stop
        result.target = target
        result.trigger_time = str(current.get("timestamp") or "")
        result.expiry = contract_specs.get("expiry_preference", "weekly")
        result.bar_index = index
        result.breakout_bar_index = breakout_idx
        result.retest_bar_index = retest_idx
        result.reasoning = (
            f"ORB-Retest v2 {direction}: OR {or_low:.2f}-{or_high:.2f} "
            f"({or_width_atr:.2f}xATR, ATR {atr_pct:.2f}%), "
            f"breakout bar {breakout_idx}, retest bar {retest_idx}, "
            f"resumption close {close:.2f} (body {body:.2f}), "
            f"stop {stop:.2f}, target {target:.2f}"
        )
        return result

    # ------------------------------------------------------------------
    # Stateless session replay
    # ------------------------------------------------------------------

    def _replay(
        self,
        candles: List[Dict[str, Any]],
        session: List[int],
        or_high: float,
        or_low: float,
        atr: float,
        index: int,
    ) -> Optional[Tuple[str, int, int]]:
        """Replay the session and return (direction, breakout, retest).

        Returns a setup only when the *current* bar (``index``) is the
        resumption bar.  The replay never looks past ``index``.
        """
        tolerance = self.retest_tolerance_atr * atr
        buffer = self.resumption_buffer_atr * atr

        direction: Optional[str] = None
        breakout_idx = -1
        retest_idx = -1
        consumed = False

        for j in session:
            if j > index:
                break
            bar = candles[j]
            try:
                high = float(bar["high"])
                low = float(bar["low"])
                close = float(bar["close"])
            except (KeyError, TypeError, ValueError):
                continue
            body = self._body_ratio(bar)

            if consumed:
                # A consumed setup re-arms only after price closes back
                # inside the opening range.
                if direction == "bullish" and close < or_high:
                    consumed = False
                    direction = None
                elif direction == "bearish" and close > or_low:
                    consumed = False
                    direction = None
                else:
                    continue

            if direction is None:
                if close > or_high and body <= self.max_breakout_body:
                    direction = "bullish"
                    breakout_idx = j
                    retest_idx = -1
                elif close < or_low and body <= self.max_breakout_body:
                    direction = "bearish"
                    breakout_idx = j
                    retest_idx = -1
                continue

            if j <= breakout_idx:
                continue

            if direction == "bullish":
                # Full invalidation: price breaks the opposite side.
                if close < or_low:
                    direction = None
                    breakout_idx = -1
                    retest_idx = -1
                    continue
                touched = low <= or_high + tolerance
                if touched:
                    retest_idx = j
                if retest_idx < 0:
                    continue
                if (j - retest_idx) > self.retest_window_bars:
                    # Retest went stale; keep waiting for a fresh one.
                    retest_idx = -1
                    continue
                resumption = close > or_high + buffer
                if resumption and body >= self.resumption_min_body and close > float(bar.get("open") or close):
                    if j == index:
                        return direction, breakout_idx, max(retest_idx, j)
                    consumed = True
            else:
                if close > or_high:
                    direction = None
                    breakout_idx = -1
                    retest_idx = -1
                    continue
                touched = high >= or_low - tolerance
                if touched:
                    retest_idx = j
                if retest_idx < 0:
                    continue
                if (j - retest_idx) > self.retest_window_bars:
                    retest_idx = -1
                    continue
                resumption = close < or_low - buffer
                if resumption and body >= self.resumption_min_body and close < float(bar.get("open") or close):
                    if j == index:
                        return direction, breakout_idx, max(retest_idx, j)
                    consumed = True

        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _session_indices(
        candles: List[Dict[str, Any]], index: int, underlying_symbol: str
    ) -> List[int]:
        current = candles[index] or {}
        current_ts = _parse_ts(current.get("timestamp"))
        if current_ts is None:
            return []
        day = current_ts.date()
        session: List[int] = []
        for j in range(index, -1, -1):
            bar = candles[j] or {}
            ts = _parse_ts(bar.get("timestamp"))
            if ts is None or ts.date() != day:
                break
            if (
                bar.get("underlying_symbol") == underlying_symbol
                and bar.get("exchange") == "NSE"
                and bar.get("instrument_type") in {"index", "stock"}
            ):
                session.append(j)
        session.reverse()
        return session

    def _atr(self, candles: List[Dict[str, Any]], index: int) -> float:
        start = max(1, index - self.ATR_PERIOD + 1)
        ranges: List[float] = []
        for j in range(start, index + 1):
            try:
                high = float(candles[j]["high"])
                low = float(candles[j]["low"])
                prev_close = float(candles[j - 1]["close"])
            except (KeyError, TypeError, ValueError, IndexError):
                continue
            ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        return sum(ranges) / len(ranges) if ranges else 0.0

    @staticmethod
    def _session_vwap(
        candles: List[Dict[str, Any]], session: List[int]
    ) -> Optional[float]:
        total = 0.0
        count = 0
        for j in session:
            try:
                high = float(candles[j]["high"])
                low = float(candles[j]["low"])
                close = float(candles[j]["close"])
            except (KeyError, TypeError, ValueError):
                continue
            total += (high + low + close) / 3.0
            count += 1
        return total / count if count else None

    @staticmethod
    def _body_ratio(bar: Dict[str, Any]) -> float:
        try:
            high = float(bar["high"])
            low = float(bar["low"])
            opn = float(bar["open"])
            close = float(bar["close"])
        except (KeyError, TypeError, ValueError):
            return 0.0
        if high <= low:
            return 0.0
        return abs(close - opn) / (high - low)

    def _minute_of_day(self, ts: datetime) -> int:
        return ts.hour * 60 + ts.minute - self.MINUTE_OFFSET

    def _in_window(self, minute: int) -> bool:
        return any(lo <= minute <= hi for lo, hi in self.session_windows)


__all__ = ["OrbRetestSignal", "OrbRetestStrategy"]
