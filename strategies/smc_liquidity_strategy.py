"""TRIO - SMC Liquidity Sweep -> Displacement -> FVG retest strategy.

A deterministic, price-action-only subset of the SMC playbook:

    liquidity sweep  ->  displacement  ->  market-structure shift
                     ->  fair value gap  ->  first retrace into the gap

Design notes
------------
* **No order flow.**  NSE index candles carry no volume (verified: 0 of 4,496
  bars for both NIFTY and BANKNIFTY) and the repo has no bid/ask or tick
  history, so delta / CVD / absorption cannot be computed.  Rather than
  fabricate them, the strategy uses displacement (body and range versus ATR)
  as the participation proxy.  This is labelled as such, not as footprint
  data.
* **AMD is folded into the sweep precondition.**  A separate
  accumulation/manipulation/distribution classifier is subjective and invites
  look-ahead.  Instead the setup requires a real liquidity level (previous-day
  high/low, a confirmed swing, or an equal-high/equal-low cluster) to be swept
  and reclaimed, which is the only part of AMD that is measurable.
* **Stateless.**  Every call replays the candle prefix up to ``index``.  The
  live planner constructs a fresh strategy instance per scan, so any
  instance-level counter would silently reset and never confirm.  Replaying
  makes backtest and production identical, and guarantees no future bar is
  read.

Gates, in order (each one is counted in the funnel diagnostics):

1. 15m bias: EMA9 vs EMA21 on completed 15-minute bars.
2. Sweep: a 5m bar wicks beyond a level and closes back through it within
   ``reclaim_bars`` bars.
3. Displacement: the reclaim bar (or the next) has body >= 0.6 x ATR14 and
   range >= 1.2 x ATR14 in the trade direction.
4. MSS: that displacement closes beyond the most recent confirmed swing
   high/low that existed *before* the sweep.
5. FVG: the displacement triple leaves a real 3-candle imbalance.
6. Entry: first retrace to the 50% level of the gap, inside 09:45-14:30,
   before the gap is fully filled or the setup expires.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

IST = timezone(timedelta(hours=5, minutes=30))

# Detector parameters (all overridable on the instance).
ATR_PERIOD = 14
SWING_K = 2                     # fractal half-width for confirmed swings
RECLAIM_BARS = 2                # sweep must be reclaimed within this many bars
EQUAL_LEVEL_ATR = 0.10          # two swings closer than this are "equal"
DISPLACEMENT_BODY_ATR = 0.60
DISPLACEMENT_RANGE_ATR = 1.20
MIN_FVG_ATR = 0.10              # ignore micro gaps
STOP_BUFFER_ATR = 0.25
RR_TARGET = 2.0
FVG_ENTRY_FRACTION = 0.50       # consequent encroachment
SETUP_TTL_BARS = 12             # 60 minutes on 5m
ENTRY_START = dtime(9, 45)
ENTRY_END = dtime(14, 30)
BIAS_FAST = 9
BIAS_SLOW = 21
BIAS_TF_BARS = 3                # 5m -> 15m


def _parse_ts(value: Any) -> Optional[datetime]:
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return ts.replace(tzinfo=IST) if ts.tzinfo is None else ts.astimezone(IST)


def _f(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # reject NaN


def _ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1.0 - alpha) * out[-1])
    return out


@dataclass
class SmcSignal:
    """Result from one SMC evaluation (matches the planner's signal shape)."""

    allowed: bool = False
    strategy: str = "smc_liquidity"
    variant: str = "bullish"          # "bullish" | "bearish"
    signal_type: str = "fvg_retest"
    entry_price: float = 0.0
    trigger_price: float = 0.0        # the swept level
    trigger_time: str = ""
    expiry: str = "weekly"
    option_type: str = "CE"
    target: float = 0.0
    stop_loss: float = 0.0
    reasoning: str = ""
    bar_index: int = -1
    underlying_symbol: str = "NIFTY"
    sweep_bar_index: int = -1
    displacement_bar_index: int = -1
    fvg_low: float = 0.0
    fvg_high: float = 0.0
    mss_level: float = 0.0
    risk_reward: float = 0.0


class SmcLiquidityStrategy:
    """Liquidity sweep + displacement + MSS + FVG retest.

    Parameters are exposed so the research harness can run sensitivity sweeps
    without editing the module.
    """

    def __init__(
        self,
        *,
        atr_period: int = ATR_PERIOD,
        swing_k: int = SWING_K,
        reclaim_bars: int = RECLAIM_BARS,
        displacement_body_atr: float = DISPLACEMENT_BODY_ATR,
        displacement_range_atr: float = DISPLACEMENT_RANGE_ATR,
        min_fvg_atr: float = MIN_FVG_ATR,
        stop_buffer_atr: float = STOP_BUFFER_ATR,
        rr_target: float = RR_TARGET,
        fvg_entry_fraction: float = FVG_ENTRY_FRACTION,
        setup_ttl_bars: int = SETUP_TTL_BARS,
        entry_start: dtime = ENTRY_START,
        entry_end: dtime = ENTRY_END,
        require_bias: bool = True,
        bias_fast: int = BIAS_FAST,
        bias_slow: int = BIAS_SLOW,
        entry_mode: str = "fvg",
    ) -> None:
        self.atr_period = max(2, int(atr_period))
        self.swing_k = max(1, int(swing_k))
        self.reclaim_bars = max(1, int(reclaim_bars))
        self.displacement_body_atr = float(displacement_body_atr)
        self.displacement_range_atr = float(displacement_range_atr)
        self.min_fvg_atr = float(min_fvg_atr)
        self.stop_buffer_atr = float(stop_buffer_atr)
        self.rr_target = float(rr_target)
        self.fvg_entry_fraction = min(1.0, max(0.0, float(fvg_entry_fraction)))
        self.setup_ttl_bars = max(1, int(setup_ttl_bars))
        self.entry_start = entry_start
        self.entry_end = entry_end
        self.require_bias = bool(require_bias)
        self.bias_fast = max(2, int(bias_fast))
        self.bias_slow = max(self.bias_fast + 1, int(bias_slow))
        self.entry_mode = str(entry_mode or "fvg").lower()

    # ------------------------------------------------------------------
    # Public boundary used by CandidatePipeline / the live planner
    # ------------------------------------------------------------------

    def evaluate_signal(
        self,
        candles: List[Dict[str, Any]],
        index: int,
        contract_specs: Optional[Dict[str, Any]] = None,
        underlying_symbol: str = "NIFTY",
    ) -> SmcSignal:
        result = SmcSignal(underlying_symbol=underlying_symbol)
        if not isinstance(candles, (list, tuple)) or index < 0 or index >= len(candles):
            return result
        signals, _funnel = self.scan_signals(candles[:index + 1], underlying_symbol)
        payload = signals.get(index)
        if not payload:
            return result
        result.allowed = True
        result.variant = payload["variant"]
        result.option_type = payload["option_type"]
        result.entry_price = payload["entry_price"]
        result.trigger_price = payload["trigger_price"]
        result.trigger_time = payload["trigger_time"]
        result.expiry = (contract_specs or {}).get("expiry_preference", "weekly")
        result.stop_loss = payload["stop_loss"]
        result.target = payload["target"]
        result.bar_index = index
        result.sweep_bar_index = payload["sweep_bar_index"]
        result.displacement_bar_index = payload["displacement_bar_index"]
        result.fvg_low = payload["fvg_low"]
        result.fvg_high = payload["fvg_high"]
        result.mss_level = payload["mss_level"]
        result.risk_reward = payload["risk_reward"]
        result.reasoning = payload["reasoning"]
        return result

    # ------------------------------------------------------------------
    # Scanner
    # ------------------------------------------------------------------

    def scan_signals(
        self,
        candles: List[Dict[str, Any]],
        underlying_symbol: str = "NIFTY",
    ) -> Tuple[Dict[int, Dict[str, Any]], Dict[str, int]]:
        """Replay the candle list once and return trigger bars + funnel counts.

        Returns ``(signals, funnel)`` where ``signals`` maps the entry-trigger
        bar index to the setup payload.
        """
        funnel = {
            "bars": 0,
            "sweeps": 0,
            "displacements": 0,
            "mss": 0,
            "fvg": 0,
            "bias_blocked": 0,
            "entries": 0,
        }
        signals: Dict[int, Dict[str, Any]] = {}
        if not candles:
            return signals, funnel

        # Normalise to plain floats; bail out on malformed history.
        highs: List[float] = []
        lows: List[float] = []
        closes: List[float] = []
        opens: List[float] = []
        stamps: List[Optional[datetime]] = []
        for candle in candles:
            high, low = _f(candle.get("high")), _f(candle.get("low"))
            close, opn = _f(candle.get("close")), _f(candle.get("open"))
            if None in (high, low, close, opn):
                return signals, funnel
            highs.append(high)
            lows.append(low)
            closes.append(close)
            opens.append(opn)
            stamps.append(_parse_ts(candle.get("timestamp")))
        n = len(candles)
        funnel["bars"] = n

        # --- ATR(14) aligned to bars (simple average of true ranges) --
        atr: List[Optional[float]] = [None] * n
        trs: List[float] = [highs[0] - lows[0]]
        for i in range(1, n):
            trs.append(max(highs[i] - lows[i],
                           abs(highs[i] - closes[i - 1]),
                           abs(lows[i] - closes[i - 1])))
        tr_sum = 0.0
        for i in range(n):
            tr_sum += trs[i]
            if i >= self.atr_period:
                tr_sum -= trs[i - self.atr_period]
            if i >= self.atr_period - 1:
                atr[i] = tr_sum / self.atr_period

        # --- session map and previous-day levels ---------------------
        session_of: List[Optional[str]] = [None] * n
        for i, ts in enumerate(stamps):
            if ts is not None:
                session_of[i] = ts.date().isoformat()
        day_high: Dict[str, float] = {}
        day_low: Dict[str, float] = {}
        for i in range(n):
            day = session_of[i]
            if day is None:
                continue
            day_high[day] = max(day_high.get(day, highs[i]), highs[i])
            day_low[day] = min(day_low.get(day, lows[i]), lows[i])
        ordered_days = sorted(day_high)
        prev_day = {day: (ordered_days[pos - 1] if pos > 0 else None)
                    for pos, day in enumerate(ordered_days)}

        # --- 15m bias ------------------------------------------------
        bias_up = [False] * n
        bias_dn = [False] * n
        m15: List[float] = []
        fast_series: List[float] = []
        slow_series: List[float] = []
        for i in range(n):
            ts = stamps[i]
            if ts is not None and (ts.minute - 25) % 15 == 0:
                m15.append(closes[i])
                fast_series = _ema(m15, self.bias_fast)
                slow_series = _ema(m15, self.bias_slow)
            if (len(fast_series) >= self.bias_slow and fast_series and slow_series):
                bias_up[i] = fast_series[-1] > slow_series[-1]
                bias_dn[i] = fast_series[-1] < slow_series[-1]

        # --- replay ---------------------------------------------------
        swing_highs: List[Tuple[int, float]] = []
        swing_lows: List[Tuple[int, float]] = []
        sweep_watch: List[Tuple[float, int, float]] = []   # level, idx, extreme
        pending: Optional[Dict[str, Any]] = None
        last_reclaim: Optional[Dict[str, Any]] = None

        for i in range(n):
            ts = stamps[i]
            if ts is None or atr[i] is None:
                continue
            a = float(atr[i])
            day = session_of[i]
            entered_this_bar = False

            # 1. confirm fractal swings (bars up to i only)
            s = i - self.swing_k
            if s - self.swing_k >= 0:
                window = range(s - self.swing_k, s + self.swing_k + 1)
                if highs[s] == max(highs[j] for j in window):
                    swing_highs.append((s, highs[s]))
                if lows[s] == min(lows[j] for j in window):
                    swing_lows.append((s, lows[s]))
                swing_highs = swing_highs[-40:]
                swing_lows = swing_lows[-40:]

            # 2. manage an armed setup
            if pending is not None:
                long_side = pending["variant"] == "bullish"
                expired = i - pending["displacement_idx"] > self.setup_ttl_bars
                if expired:
                    pending = None
                elif long_side:
                    if lows[i] <= pending["fvg_low"]:
                        pending = None                      # gap fully filled
                    elif lows[i] <= pending["entry_level"]:
                        if self._window_ok(ts) and (not self.require_bias or bias_up[i]):
                            signals[i] = self._payload(pending, i, ts, "bullish")
                            funnel["entries"] += 1
                            entered_this_bar = True
                            pending = None
                        elif self.require_bias and not bias_up[i]:
                            funnel["bias_blocked"] += 1
                else:
                    if highs[i] >= pending["fvg_high"]:
                        pending = None
                    elif highs[i] >= pending["entry_level"]:
                        if self._window_ok(ts) and (not self.require_bias or bias_dn[i]):
                            signals[i] = self._payload(pending, i, ts, "bearish")
                            funnel["entries"] += 1
                            entered_this_bar = True
                            pending = None
                        elif self.require_bias and not bias_dn[i]:
                            funnel["bias_blocked"] += 1

            # 3. register sweeps of real levels
            if entered_this_bar:
                continue
            pday = prev_day.get(day) if day else None
            levels_long: List[float] = []
            levels_short: List[float] = []
            if pday:
                levels_long.append(day_low[pday])
                levels_short.append(day_high[pday])
            for idx, price in swing_lows[-20:]:
                if idx < i:
                    levels_long.append(price)
            for idx, price in swing_highs[-20:]:
                if idx < i:
                    levels_short.append(price)
            # equal highs/lows: a second swing within tolerance confirms a pool
            for idx, price in swing_lows[-20:]:
                for idx2, price2 in swing_lows[-20:]:
                    if idx != idx2 and abs(price - price2) <= EQUAL_LEVEL_ATR * a:
                        levels_long.append(min(price, price2))
            for idx, price in swing_highs[-20:]:
                for idx2, price2 in swing_highs[-20:]:
                    if idx != idx2 and abs(price - price2) <= EQUAL_LEVEL_ATR * a:
                        levels_short.append(max(price, price2))

            for level in {round(x, 4) for x in levels_long}:
                if lows[i] < level:
                    sweep_watch.append((level, i, lows[i]))
                    funnel["sweeps"] += 1
            for level in {round(x, 4) for x in levels_short}:
                if highs[i] > level:
                    sweep_watch.append((level, i, highs[i]))
                    funnel["sweeps"] += 1
            sweep_watch = [w for w in sweep_watch if i - w[1] <= self.reclaim_bars]

            # 4. reclaim -> (next) displacement -> MSS -> FVG
            body = abs(closes[i] - opens[i])
            rng = highs[i] - lows[i]
            disp_up = (closes[i] > opens[i]
                       and body >= self.displacement_body_atr * a
                       and rng >= self.displacement_range_atr * a)
            disp_dn = (closes[i] < opens[i]
                       and body >= self.displacement_body_atr * a
                       and rng >= self.displacement_range_atr * a)

            reclaims: List[Dict[str, Any]] = []
            for level, idx, extreme in sweep_watch:
                if closes[i] > level and highs[i] > level:
                    reclaims.append({"variant": "bullish", "level": level,
                                     "sweep_idx": idx, "extreme": extreme})
                elif closes[i] < level and lows[i] < level:
                    reclaims.append({"variant": "bearish", "level": level,
                                     "sweep_idx": idx, "extreme": extreme})
            if reclaims:
                last_reclaim = reclaims[0]
            elif last_reclaim is not None and i - last_reclaim["sweep_idx"] > self.reclaim_bars:
                last_reclaim = None

            candidates = list(reclaims)
            if last_reclaim is not None and last_reclaim not in candidates:
                candidates.append(last_reclaim)

            for cand in candidates:
                if pending is not None:
                    break
                long_side = cand["variant"] == "bullish"
                if long_side and not disp_up:
                    continue
                if not long_side and not disp_dn:
                    continue
                funnel["displacements"] += 1

                # MSS: the last confirmed swing before the sweep must break
                if long_side:
                    prior = [(idx, price) for idx, price in swing_highs
                             if idx < cand["sweep_idx"]]
                    if not prior:
                        continue
                    mss_level = prior[-1][1]
                    if closes[i] <= mss_level:
                        continue
                else:
                    prior = [(idx, price) for idx, price in swing_lows
                             if idx < cand["sweep_idx"]]
                    if not prior:
                        continue
                    mss_level = prior[-1][1]
                    if closes[i] >= mss_level:
                        continue
                funnel["mss"] += 1

                # Diagnostic mode: enter on the MSS displacement close instead
                # of waiting for an FVG retrace.  Used to separate "structure
                # has no edge" from "the FVG gate is starving the sleeve".
                if self.entry_mode == "mss":
                    entry_level = closes[i]
                    stop = (cand["extreme"] - self.stop_buffer_atr * a
                            if long_side else cand["extreme"] + self.stop_buffer_atr * a)
                    risk = (entry_level - stop) if long_side else (stop - entry_level)
                    if risk <= 0:
                        continue
                    target = (entry_level + self.rr_target * risk if long_side
                              else entry_level - self.rr_target * risk)
                    setup = {
                        "variant": cand["variant"],
                        "option_type": "CE" if long_side else "PE",
                        "trigger_price": cand["level"],
                        "sweep_idx": cand["sweep_idx"],
                        "displacement_idx": i,
                        "fvg_low": 0.0, "fvg_high": 0.0,
                        "entry_level": entry_level,
                        "stop": stop, "target": target,
                        "mss_level": mss_level, "risk": risk,
                    }
                    signals[i] = self._payload(setup, i, ts, cand["variant"])
                    funnel["entries"] += 1
                    entered_this_bar = True
                    pending = None
                    continue

                # FVG in the displacement triple (i-2, i-1, i)
                if i < 2:
                    continue
                if long_side:
                    fvg_low, fvg_high = highs[i - 2], lows[i]
                    if not (fvg_low < fvg_high
                            and (fvg_high - fvg_low) >= self.min_fvg_atr * a):
                        continue
                else:
                    fvg_low, fvg_high = highs[i], lows[i - 2]
                    if not (fvg_low < fvg_high
                            and (fvg_high - fvg_low) >= self.min_fvg_atr * a):
                        continue
                funnel["fvg"] += 1

                entry_level = (fvg_low + self.fvg_entry_fraction
                               * (fvg_high - fvg_low))
                stop = (cand["extreme"] - self.stop_buffer_atr * a
                        if long_side else cand["extreme"] + self.stop_buffer_atr * a)
                risk = (entry_level - stop) if long_side else (stop - entry_level)
                if risk <= 0:
                    continue
                target = (entry_level + self.rr_target * risk if long_side
                          else entry_level - self.rr_target * risk)
                pending = {
                    "variant": cand["variant"],
                    "option_type": "CE" if long_side else "PE",
                    "trigger_price": cand["level"],
                    "sweep_idx": cand["sweep_idx"],
                    "displacement_idx": i,
                    "fvg_low": fvg_low,
                    "fvg_high": fvg_high,
                    "entry_level": entry_level,
                    "stop": stop,
                    "target": target,
                    "mss_level": mss_level,
                    "risk": risk,
                }

        return signals, funnel

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _window_ok(self, ts: datetime) -> bool:
        return self.entry_start <= ts.time() <= self.entry_end

    def _payload(self, pending: Dict[str, Any], index: int,
                 ts: datetime, variant: str) -> Dict[str, Any]:
        entry = pending["entry_level"]
        return {
            "variant": variant,
            "option_type": pending["option_type"],
            "entry_price": entry,
            "trigger_price": pending["trigger_price"],
            "trigger_time": ts.isoformat(),
            "stop_loss": pending["stop"],
            "target": pending["target"],
            "sweep_bar_index": pending["sweep_idx"],
            "displacement_bar_index": pending["displacement_idx"],
            "fvg_low": pending["fvg_low"],
            "fvg_high": pending["fvg_high"],
            "mss_level": pending["mss_level"],
            "risk_reward": self.rr_target,
            "reasoning": (
                f"SMC {variant}: swept {pending['trigger_price']:.2f}, "
                f"displacement closed {pending['mss_level']:.2f} MSS, "
                f"FVG {pending['fvg_low']:.2f}-{pending['fvg_high']:.2f}, "
                f"entry {entry:.2f}, stop {pending['stop']:.2f}, "
                f"target {pending['target']:.2f}"
            ),
        }
