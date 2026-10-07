"""
TRIO — Technical Indicators
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

Calculates trend, momentum, volatility, and volume indicators on OHLCV data.
All parameters are configurable via config.yaml.

Each indicator returns a value and a signal: bullish / bearish / neutral / info.

DISCLAIMER: Educational purposes only. Not financial advice.
"""

import logging
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from src.utils import get_logger, load_config, utc_now

logger = get_logger("indicators")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class IndicatorReading:
    """Single indicator value and its signal.
    Author: Kapil Kuhire
    """
    value: Any = None
    signal: str = "neutral"  # bullish / bearish / neutral / info
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = {"value": self.value, "signal": self.signal}
        d.update(self.extra)
        return d

@dataclass
class TechnicalReadings:
    """Complete set of technical indicator readings for a symbol.
    Author: Kapil Kuhire
    """
    symbol: str = ""
    timeframe: str = ""
    computed_at: str = ""
    indicators: Dict[str, IndicatorReading] = field(default_factory=dict)
    support: List[float] = field(default_factory=list)
    resistance: List[float] = field(default_factory=list)
    swing_low: Optional[float] = None
    swing_high: Optional[float] = None
    summary: Dict[str, Any] = field(default_factory=dict)
    pullback_detected: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "computed_at": self.computed_at,
            "indicators": {k: v.to_dict() for k, v in self.indicators.items()},
            "support": self.support,
            "resistance": self.resistance,
            "swing_low": self.swing_low,
            "swing_high": self.swing_high,
            "summary": self.summary,
            "pullback_detected": self.pullback_detected,
        }


# ---------------------------------------------------------------------------
# Individual indicator calculations
# ---------------------------------------------------------------------------

def calc_sma(df: pd.DataFrame, period: int) -> IndicatorReading:
    """Simple Moving Average."""
    sma = df["Close"].rolling(window=period).mean()
    val = float(sma.iloc[-1]) if not sma.empty and pd.notna(sma.iloc[-1]) else None
    close = float(df["Close"].iloc[-1])

    if val is None:
        signal = "neutral"
    elif close > val:
        signal = "bullish"
    elif close < val:
        signal = "bearish"
    else:
        signal = "neutral"

    return IndicatorReading(value=val, signal=signal)


def calc_ema(df: pd.DataFrame, period: int) -> IndicatorReading:
    """Exponential Moving Average."""
    ema = df["Close"].ewm(span=period, adjust=False).mean()
    val = float(ema.iloc[-1]) if not ema.empty and pd.notna(ema.iloc[-1]) else None
    close = float(df["Close"].iloc[-1])

    if val is None:
        signal = "neutral"
    elif close > val:
        signal = "bullish"
    elif close < val:
        signal = "bearish"
    else:
        signal = "neutral"

    return IndicatorReading(value=val, signal=signal)


def calc_macd(
    df: pd.DataFrame, fast: int = 12, slow: int = 26, signal_period: int = 9
) -> IndicatorReading:
    """MACD (Moving Average Convergence Divergence)."""
    ema_fast = df["Close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["Close"].ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()
    histogram = macd_line - signal_line

    macd_val = float(macd_line.iloc[-1]) if pd.notna(macd_line.iloc[-1]) else None
    sig_val = float(signal_line.iloc[-1]) if pd.notna(signal_line.iloc[-1]) else None
    hist_val = float(histogram.iloc[-1]) if pd.notna(histogram.iloc[-1]) else None

    # Signal: bullish if MACD above signal line and histogram positive
    if macd_val is not None and sig_val is not None:
        if macd_val > sig_val and hist_val and hist_val > 0:
            signal = "bullish"
        elif macd_val < sig_val and hist_val and hist_val < 0:
            signal = "bearish"
        else:
            signal = "neutral"
    else:
        signal = "neutral"

    return IndicatorReading(
        value=macd_val,
        signal=signal,
        extra={"signal_line": sig_val, "histogram": hist_val},
    )


def calc_rsi(df: pd.DataFrame, period: int = 14, overbought: float = 70, oversold: float = 30) -> IndicatorReading:
    """Relative Strength Index."""
    delta = df["Close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)

    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()

    # A window with no down bars (avg_loss == 0) is RSI 100, not missing data.
    # A window with no up bars (avg_gain == 0) is RSI 0.
    rsi = pd.Series(np.nan, index=df.index, dtype=float)
    both_zero = (avg_loss == 0) & (avg_gain == 0)
    no_loss = (avg_loss == 0) & (avg_gain > 0)
    no_gain = (avg_gain == 0) & (avg_loss > 0)
    normal = (avg_loss > 0) & avg_gain.notna() & avg_loss.notna()

    rsi = rsi.mask(both_zero, 50.0)
    rsi = rsi.mask(no_loss, 100.0)
    rsi = rsi.mask(no_gain, 0.0)
    rs = avg_gain / avg_loss
    rsi = rsi.mask(normal, 100 - (100 / (1 + rs)))

    val = float(rsi.iloc[-1]) if not rsi.empty and pd.notna(rsi.iloc[-1]) else None

    if val is None:
        signal = "neutral"
    elif val >= overbought:
        signal = "bearish"  # overbought -> potential reversal down
    elif val <= oversold:
        signal = "bullish"  # oversold -> potential reversal up
    else:
        signal = "neutral"

    return IndicatorReading(value=val, signal=signal)


def calc_stochastic(
    df: pd.DataFrame, k_period: int = 14, d_period: int = 3,
    overbought: float = 80, oversold: float = 20,
) -> IndicatorReading:
    """Stochastic Oscillator (%K and %D)."""
    low_min = df["Low"].rolling(window=k_period).min()
    high_max = df["High"].rolling(window=k_period).max()

    denom = high_max - low_min
    denom = denom.replace(0, np.nan)
    k = 100 * (df["Close"] - low_min) / denom
    d = k.rolling(window=d_period).mean()

    k_val = float(k.iloc[-1]) if pd.notna(k.iloc[-1]) else None
    d_val = float(d.iloc[-1]) if pd.notna(d.iloc[-1]) else None

    if k_val is None:
        signal = "neutral"
    elif k_val >= overbought:
        signal = "bearish"
    elif k_val <= oversold:
        signal = "bullish"
    else:
        signal = "neutral"

    return IndicatorReading(value=k_val, signal=signal, extra={"k": k_val, "d": d_val})


def calc_bollinger(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> IndicatorReading:
    """Bollinger Bands."""
    middle = df["Close"].rolling(window=period).mean()
    std = df["Close"].rolling(window=period).std()
    upper = middle + std_dev * std
    lower = middle - std_dev * std

    upper_val = float(upper.iloc[-1]) if pd.notna(upper.iloc[-1]) else None
    middle_val = float(middle.iloc[-1]) if pd.notna(middle.iloc[-1]) else None
    lower_val = float(lower.iloc[-1]) if pd.notna(lower.iloc[-1]) else None
    close = float(df["Close"].iloc[-1])

    if upper_val is not None and lower_val is not None:
        if close >= upper_val:
            signal = "bearish"  # at upper band -> potential reversal
        elif close <= lower_val:
            signal = "bullish"  # at lower band -> potential bounce
        else:
            signal = "neutral"
    else:
        signal = "neutral"

    return IndicatorReading(
        value=middle_val,
        signal=signal,
        extra={"upper": upper_val, "middle": middle_val, "lower": lower_val},
    )


def calc_atr(df: pd.DataFrame, period: int = 14) -> IndicatorReading:
    """Average True Range (volatility, no directional signal)."""
    high = df["High"]
    low = df["Low"]
    prev_close = df["Close"].shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr = tr.rolling(window=period).mean()
    val = float(atr.iloc[-1]) if pd.notna(atr.iloc[-1]) else None

    return IndicatorReading(value=val, signal="info")


def calc_vwap(df: pd.DataFrame) -> IndicatorReading:
    """Volume Weighted Average Price."""
    typical = (df["High"] + df["Low"] + df["Close"]) / 3
    cum_tp_vol = (typical * df["Volume"]).cumsum()
    cum_vol = df["Volume"].cumsum().replace(0, np.nan)
    vwap = cum_tp_vol / cum_vol

    val = float(vwap.iloc[-1]) if pd.notna(vwap.iloc[-1]) else None
    close = float(df["Close"].iloc[-1])

    if val is None:
        signal = "neutral"
    elif close > val:
        signal = "bullish"
    elif close < val:
        signal = "bearish"
    else:
        signal = "neutral"

    return IndicatorReading(value=val, signal=signal)


def calc_adx(df: pd.DataFrame, period: int = 14) -> IndicatorReading:
    """Average Directional Index — trend STRENGTH (not direction).

    Returns 0-100. Conventional reading: below ~20-25 = chop/range,
    above ~25 = a real trend is present. Used as a regime filter: skip
    entries when ADX is below threshold, because Sep 10-11 style losses
    came from trading directionless chop.

    Signal convention: "bullish" when ADX >= 25 (trending regime),
    "bearish" when ADX < 15 (dead chop), "neutral" between.
    """
    high = df["High"]
    low = df["Low"]
    close = df["Close"]

    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)

    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr = tr.rolling(window=period).mean()
    plus_di = 100 * plus_dm.rolling(window=period).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.rolling(window=period).mean() / atr.replace(0, np.nan)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.rolling(window=period).mean()

    val = float(adx.iloc[-1]) if not adx.empty and pd.notna(adx.iloc[-1]) else None
    pdi = float(plus_di.iloc[-1]) if pd.notna(plus_di.iloc[-1]) else None
    mdi = float(minus_di.iloc[-1]) if pd.notna(minus_di.iloc[-1]) else None

    if val is None:
        signal = "neutral"
    elif val >= 25:
        signal = "bullish"   # trending regime
    elif val < 15:
        signal = "bearish"   # dead chop
    else:
        signal = "neutral"

    return IndicatorReading(value=val, signal=signal,
                            extra={"plus_di": pdi, "minus_di": mdi})


def calc_obv(df: pd.DataFrame) -> IndicatorReading:
    """On-Balance Volume."""
    obv = pd.Series(0, index=df.index, dtype=float)
    for i in range(1, len(df)):
        if df["Close"].iloc[i] > df["Close"].iloc[i - 1]:
            obv.iloc[i] = obv.iloc[i - 1] + df["Volume"].iloc[i]
        elif df["Close"].iloc[i] < df["Close"].iloc[i - 1]:
            obv.iloc[i] = obv.iloc[i - 1] - df["Volume"].iloc[i]
        else:
            obv.iloc[i] = obv.iloc[i - 1]

    val = float(obv.iloc[-1]) if pd.notna(obv.iloc[-1]) else None

    # Trend: compare OBV now to 5 bars ago
    if len(obv) >= 6:
        recent = obv.iloc[-1]
        older = obv.iloc[-6]
        if recent > older:
            trend = "rising"
            signal = "bullish"
        elif recent < older:
            trend = "falling"
            signal = "bearish"
        else:
            trend = "flat"
            signal = "neutral"
    else:
        trend = "unknown"
        signal = "neutral"

    return IndicatorReading(value=val, signal=signal, extra={"trend": trend})


# ---------------------------------------------------------------------------
# Support & Resistance detection (simple pivot-based)
# ---------------------------------------------------------------------------

def detect_swing_levels(
    df: pd.DataFrame, lookback: int = 10
) -> Tuple[Optional[float], Optional[float]]:
    """
    Return the most recent swing low and swing high for stop placement.

    The swing low is the lowest Low over the trailing `lookback` bars, shifted
    by one bar so the value at bar i only uses data up to bar i-1. That shift
    matters: without it the current bar's own low participates in the level,
    which is look-ahead bias and inflates backtest results.

    Why this exists: an out-of-sample test over 15 NSE large caps (5y, daily)
    found a stop placed below this swing low gave a better profit factor than
    any fixed ATR multiple tried.

    Args:
        df:       OHLCV DataFrame.
        lookback: Number of trailing bars to inspect.

    Returns:
        (swing_low, swing_high). Either may be None if history is too short.
    """
    if len(df) < lookback + 1:
        return None, None

    low = float(df["Low"].rolling(lookback).min().shift(1).iloc[-1])
    high = float(df["High"].rolling(lookback).max().shift(1).iloc[-1])

    if pd.isna(low):
        low = None
    if pd.isna(high):
        high = None

    return low, high


def detect_support_resistance(
    df: pd.DataFrame, lookback: int = 20, num_levels: int = 3
) -> Tuple[List[float], List[float]]:
    """
    Detect support and resistance levels using local min/max pivots.

    Args:
        df:       OHLCV DataFrame.
        lookback: Window size for detecting pivots.
        num_levels: Number of levels to return.

    Returns:
        Tuple of (support_levels, resistance_levels).
    """
    if len(df) < lookback * 2:
        return [], []

    supports = []
    resistances = []

    for i in range(lookback, len(df) - lookback):
        low_window = df["Low"].iloc[i - lookback:i + lookback + 1]
        high_window = df["High"].iloc[i - lookback:i + lookback + 1]

        if df["Low"].iloc[i] == low_window.min():
            supports.append(float(df["Low"].iloc[i]))
        if df["High"].iloc[i] == high_window.max():
            resistances.append(float(df["High"].iloc[i]))

    # Cluster nearby levels (within 0.5%) and take the strongest
    supports = _cluster_levels(supports, tolerance=0.005)[:num_levels]
    resistances = _cluster_levels(resistances, tolerance=0.005)[:num_levels]

    return sorted(supports), sorted(resistances, reverse=True)


def _cluster_levels(levels: List[float], tolerance: float = 0.005) -> List[float]:
    """Cluster nearby price levels and return averaged representatives."""
    if not levels:
        return []

    sorted_levels = sorted(levels)
    clusters: List[List[float]] = [[sorted_levels[0]]]

    for lvl in sorted_levels[1:]:
        if abs(lvl - clusters[-1][-1]) / clusters[-1][-1] <= tolerance:
            clusters[-1].append(lvl)
        else:
            clusters.append([lvl])

    # Sort by cluster size (most touches first), return averages
    clusters.sort(key=len, reverse=True)
    return [round(sum(c) / len(c), 2) for c in clusters]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_indicators(
    df: pd.DataFrame,
    symbol: str = "",
    timeframe: str = "",
    config_override: Optional[Dict[str, Any]] = None,
) -> TechnicalReadings:
    """
    Compute all configured technical indicators on an OHLCV DataFrame.

    Args:
        df:              OHLCV DataFrame with columns Open, High, Low, Close, Volume.
        symbol:          Ticker symbol for labeling.
        timeframe:       Timeframe for labeling.
        config_override: Optional dict to override indicator params from config.

    Returns:
        TechnicalReadings with all indicator values and signals.
    """
    if len(df) < 5:
        raise ValueError(f"Need at least 5 rows of data, got {len(df)}")

    cfg = load_config()
    ind_cfg = config_override or cfg.get("indicators", {})

    indicators: Dict[str, IndicatorReading] = {}

    # --- Trend ---
    # The entry gate needs its own SMA windows regardless of the display list:
    # intraday (15m) scans carry ~76 bars, so the 50/20 defaults exist, and we
    # also compute whatever the pullback predicate asks for so the gate can
    # never report "windows unavailable" on its own config.
    trend_cfg = ind_cfg.get("trend", {})
    entry_periods = [9, 21, 50, 200]
    try:
        pb_cfg_early = cfg.get("signal_engine", {}).get("pullback", {})
        entry_periods += [pb_cfg_early.get("trend_sma", 200), pb_cfg_early.get("pullback_sma", 50)]
    except Exception:
        pass
    for period in dict.fromkeys(list(trend_cfg.get("sma_periods", [9, 21, 50, 200])) + entry_periods):
        if period and len(df) >= period:
            indicators[f"sma_{period}"] = calc_sma(df, period)

    for period in trend_cfg.get("ema_periods", [9, 21, 50, 200]):
        if len(df) >= period:
            indicators[f"ema_{period}"] = calc_ema(df, period)

    macd_cfg = trend_cfg.get("macd", {})
    if len(df) >= macd_cfg.get("slow", 26):
        indicators["macd"] = calc_macd(
            df,
            fast=macd_cfg.get("fast", 12),
            slow=macd_cfg.get("slow", 26),
            signal_period=macd_cfg.get("signal", 9),
        )

    # --- Momentum ---
    mom_cfg = ind_cfg.get("momentum", {})
    rsi_cfg = mom_cfg.get("rsi", {})
    rsi_period = rsi_cfg.get("period", 14)
    if len(df) >= rsi_period:
        indicators[f"rsi_{rsi_period}"] = calc_rsi(
            df, rsi_period,
            overbought=rsi_cfg.get("overbought", 70),
            oversold=rsi_cfg.get("oversold", 30),
        )

    stoch_cfg = mom_cfg.get("stochastic", {})
    k_period = stoch_cfg.get("k_period", 14)
    if len(df) >= k_period:
        indicators["stoch"] = calc_stochastic(
            df,
            k_period=k_period,
            d_period=stoch_cfg.get("d_period", 3),
            overbought=stoch_cfg.get("overbought", 80),
            oversold=stoch_cfg.get("oversold", 20),
        )

    adx_cfg = mom_cfg.get("adx", {})
    adx_period = adx_cfg.get("period", 14)
    if len(df) >= adx_period * 2 + 1:
        indicators[f"adx_{adx_period}"] = calc_adx(df, adx_period)

    # --- Volatility ---
    vol_cfg = ind_cfg.get("volatility", {})
    bb_cfg = vol_cfg.get("bollinger", {})
    bb_period = bb_cfg.get("period", 20)
    if len(df) >= bb_period:
        indicators["bbands"] = calc_bollinger(
            df, period=bb_period, std_dev=bb_cfg.get("std_dev", 2.0),
        )

    atr_cfg = vol_cfg.get("atr", {})
    atr_period = atr_cfg.get("period", 14)
    if len(df) >= atr_period:
        indicators[f"atr_{atr_period}"] = calc_atr(df, atr_period)

    # --- Volume ---
    vol_ind_cfg = ind_cfg.get("volume", {})
    if vol_ind_cfg.get("vwap", True):
        indicators["vwap"] = calc_vwap(df)
    if vol_ind_cfg.get("obv", True):
        indicators["obv"] = calc_obv(df)

    # --- Support / Resistance ---
    support, resistance = detect_support_resistance(df)

    # --- Swing levels (stop placement) ---
    swing_lookback = 10
    swing_low, swing_high = detect_swing_levels(df, lookback=swing_lookback)

    # --- Summary (macro-trend only) ---
    # The count below covers the full bar window and only feeds the display
    # label "overall" plus the diagnostic composite score. The entry gate is
    # the pullback predicate defined just below; composite alone never takes
    # a trade, so the dead-indicator-counting concern does not apply.
    bullish = sum(1 for v in indicators.values() if v.signal == "bullish")
    bearish = sum(1 for v in indicators.values() if v.signal == "bearish")
    neutral = sum(1 for v in indicators.values() if v.signal in ("neutral", "info"))

    if bullish > bearish + 2:
        overall = "bullish"
    elif bearish > bullish + 2:
        overall = "bearish"
    else:
        overall = "neutral"

    # --- Pullback entry setup ---
    # Definition validated out-of-sample on DAILY bars across 15 NSE large
    # caps (5y): buy dips inside an established uptrend, never chase.
    #   1. close above the 200 SMA   -> we are in an uptrend
    #   2. close below the 50 SMA    -> price has pulled back into the trend
    #   3. MACD line above its signal-> momentum has turned back up
    # An RSI filter was tested on daily bars and reduced expectancy, so it is
    # off by default (config: pullback.require_rsi_below = 0).
    #
    # INTRADAY STATUS (verified 2026-10-05): a 50/20 variant for 15m bars was
    # tested on 60d of 15m history across 15 NSE names with a train/test
    # split. Every 15m config failed (test profit factor 0.04-0.55). The
    # intraday gate is therefore not trusted. If config points the pullback
    # predicate at short windows anyway, the gate still evaluates the pattern
    # mechanically, but nothing about its profitability has been shown.
    close = float(df["Close"].iloc[-1])
    pb_cfg = cfg.get("signal_engine", {}).get("pullback", {})
    trend_key = f"sma_{pb_cfg.get('trend_sma', 200)}"
    pull_key = f"sma_{pb_cfg.get('pullback_sma', 50)}"
    trend_sma = indicators.get(trend_key)
    pull_sma = indicators.get(pull_key)
    macd_reading = indicators.get("macd")
    rsi_reading = indicators.get(f"rsi_{cfg.get('indicators', {}).get('momentum', {}).get('rsi', {}).get('period', 14)}")
    rsi_cap = pb_cfg.get("require_rsi_below", 0)

    pullback = False
    pullback_reason = "setup incomplete"
    if trend_sma is None or pull_sma is None:
        pullback_reason = (
            f"windows unavailable on this bar count: need {trend_key} and "
            f"{pull_key} (not enough bars for one or both)"
        )
    elif macd_reading is None:
        pullback_reason = "macd not computable on this bar count"
    elif trend_sma.value is None or pull_sma.value is None:
        pullback_reason = f"{trend_key} or {pull_key} value is NaN (history too short)"
    else:
        in_uptrend = close > trend_sma.value
        pulled_back = close < pull_sma.value
        macd_turning_up = (macd_reading.value is not None
                           and macd_reading.value > (macd_reading.extra.get("signal_line") or 0))
        rsi_ok = True
        if rsi_cap and rsi_reading and rsi_reading.value is not None:
            rsi_ok = rsi_reading.value < rsi_cap
        pullback = bool(in_uptrend and pulled_back and macd_turning_up and rsi_ok)
        if not pullback:
            missing = []
            if not in_uptrend:
                missing.append(f"close {close:.2f} not above {trend_key} {trend_sma.value:.2f}")
            if not pulled_back:
                missing.append(f"close {close:.2f} not below {pull_key} {pull_sma.value:.2f}")
            if not macd_turning_up:
                missing.append("macd not above its signal line")
            if not rsi_ok:
                missing.append(f"rsi {rsi_reading.value:.1f} not below {rsi_cap}")
            pullback_reason = "; ".join(missing) if missing else "unknown"

    readings = TechnicalReadings(
        symbol=symbol,
        timeframe=timeframe,
        computed_at=utc_now(),
        indicators=indicators,
        support=support,
        resistance=resistance,
        swing_low=swing_low,
        swing_high=swing_high,
        summary={
            "bullish_count": bullish,
            "bearish_count": bearish,
            "neutral_count": neutral,
            "overall": overall,
        },
        pullback_detected=pullback,
    )

    # Log one line that states WHY the gate fired or stayed shut, so a scan
    # of live 15m bars is diagnosable without digging through numbers.
    if pullback:
        logger.info("%s pullback SETUP: close %.2f inside uptrend (%s), dipped below %s, macd turned up",
                    symbol, close, trend_key, pull_key)
    else:
        logger.info("%s no pullback: %s", symbol, pullback_reason)

    logger.info(
        "%s indicators computed: %d bullish, %d bearish, %d neutral -> %s",
        symbol, bullish, bearish, neutral, overall,
    )

    return readings
