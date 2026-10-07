"""
TRIO — Strategy experiment harness (diagnostic tool, not part of the app).

Purpose: stop guessing at parameters. Cache market data once, then evaluate
several strategy variants on identical data so results are comparable.

Run:  python scripts/experiment.py
Writes: scripts/experiment_results.json
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CACHE = ROOT / "scripts" / "_data_cache"

# Indian large/mid caps spanning sectors (NSE yfinance tickers)
BASKET = [
    "RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS",
    "SBIN.NS", "ITC.NS", "LT.NS", "AXISBANK.NS", "MARUTI.NS",
    "HINDUNILVR.NS", "SUNPHARMA.NS", "KOTAKBANK.NS", "ADANIENT.NS", "WIPRO.NS",
]

PERIOD = "5y"


# ---------------------------------------------------------------------------
# Data caching (network is slow and rate-limited; test logic on frozen data)
# ---------------------------------------------------------------------------

def load_prices(symbol: str) -> Optional[pd.DataFrame]:
    """Fetch OHLCV once and cache to disk as parquet-free CSV."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{symbol.replace('.', '_')}_{PERIOD}.csv"

    if path.exists():
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        return df

    try:
        import yfinance as yf
        raw = yf.Ticker(symbol).history(period=PERIOD, interval="1d")
        if raw.empty:
            return None
        raw.columns = [c.title() for c in raw.columns]
        df = raw[["Open", "High", "Low", "Close", "Volume"]].dropna()
        df.to_csv(path)
        print(f"  cached {symbol}: {len(df)} bars")
        return df
    except Exception as exc:
        print(f"  FAILED {symbol}: {exc}")
        return None


def load_all() -> Dict[str, pd.DataFrame]:
    data = {}
    print(f"Loading {PERIOD} daily data for {len(BASKET)} symbols...")
    for sym in BASKET:
        df = load_prices(sym)
        if df is not None and len(df) > 300:
            data[sym] = df
    print(f"Loaded {len(data)} symbols\n")
    return data


# ---------------------------------------------------------------------------
# Indicators (self-contained, vectorised — mirrors src/indicators.py semantics)
# ---------------------------------------------------------------------------

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def swing_low(df: pd.DataFrame, lookback: int = 10) -> pd.Series:
    """Lowest low over the trailing `lookback` bars, shifted 1 to avoid lookahead."""
    return df["Low"].rolling(lookback).min().shift(1)


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Attach every series a variant might need. All are backward-looking."""
    f = pd.DataFrame(index=df.index)
    close = df["Close"]

    f["close"] = close
    f["high"] = df["High"]
    f["low"] = df["Low"]
    f["sma20"] = close.rolling(20).mean()
    f["sma50"] = close.rolling(50).mean()
    f["sma200"] = close.rolling(200).mean()

    f["rsi14"] = _rsi(close, 14)
    f["atr14"] = atr(df, 14)

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    f["macd"] = macd
    f["macd_signal"] = macd.ewm(span=9, adjust=False).mean()
    f["macd_hist"] = macd - f["macd_signal"]

    mid = close.rolling(20).mean()
    std = close.rolling(20).std()
    f["bb_mid"] = mid
    f["bb_upper"] = mid + 2 * std
    f["bb_lower"] = mid - 2 * std

    f["swing_low"] = swing_low(df, 10)
    f["swing_high"] = df["High"].rolling(10).max().shift(1)

    # Volume surge: today's volume vs 20-bar average
    f["vol_ratio"] = df["Volume"] / df["Volume"].rolling(20).mean()

    return f


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    out = out.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    out = out.mask((avg_gain == 0) & (avg_loss > 0), 0.0)
    return out


# ---------------------------------------------------------------------------
# Backtest engine shared by all variants
# ---------------------------------------------------------------------------

def simulate(
    f: pd.DataFrame,
    entries: pd.Series,
    stop_kind: str,
    stop_atr_mult: float,
    target_r: Optional[float],
    trail_atr_mult: Optional[float],
    risk_pct: float = 1.0,
) -> List[Dict[str, Any]]:
    """
    Walk bars forward. `entries[i] == 1` means "may open a long at the close of bar i".

    Exit priority per bar: stop check first (conservative — assumes we don't know
    the intraday order), then target, then trail.

    Returns a list of trade dicts.
    """
    trades: List[Dict[str, Any]] = []
    in_trade = False
    stop = target = entry = 0.0
    entry_i = 0

    rows = f.to_dict("records")
    n = len(rows)
    i = 0

    while i < n:
        row = rows[i]

        if in_trade:
            low, high = row["low"], row["high"]

            # Stop first: if a bar touches stop and target, assume stop.
            if stop_kind == "atr":
                hit = low <= stop
            else:
                hit = low <= stop

            if hit:
                exit_p = stop
                trades.append(_mk(entry, exit_p, entry_i, i, "stop"))
                in_trade = False
                i += 1
                continue

            if target_r is not None and target > 0:
                if high >= target:
                    trades.append(_mk(entry, target, entry_i, i, "target"))
                    in_trade = False
                    i += 1
                    continue

            if trail_atr_mult is not None and row["atr14"] == row["atr14"]:
                new_stop = row["close"] - trail_atr_mult * row["atr14"]
                if new_stop > stop:
                    stop = new_stop

            i += 1
            continue

        # Flat: look for an entry signal
        if entries.iloc[i] == 1:
            entry = row["close"]
            entry_i = i
            a = row["atr14"]

            if stop_kind == "atr":
                stop = entry - stop_atr_mult * a
            else:
                sl = row["swing_low"]
                stop = sl if sl == sl and sl > 0 else entry - stop_atr_mult * a

            risk = entry - stop
            if risk <= 0:
                i += 1
                continue

            target = entry + risk * target_r if target_r else 0.0
            in_trade = True
            i += 1
            continue

        i += 1

    if in_trade:
        last = rows[-1]["close"]
        trades.append(_mk(entry, last, entry_i, n - 1, "open"))
    return trades


def _mk(entry: float, exit_p: float, ei: int, xi: int, reason: str) -> Dict[str, Any]:
    return {
        "entry": entry, "exit": exit_p, "pnl_pct": (exit_p - entry) / entry * 100,
        "entry_i": ei, "exit_i": xi, "reason": reason,
    }


def metrics(trades: List[Dict[str, Any]], label: str) -> Dict[str, Any]:
    if not trades:
        return {"variant": label, "trades": 0}
    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]
    gp = sum(t["pnl_pct"] for t in wins)
    gl = abs(sum(t["pnl_pct"] for t in losses))
    pf = (gp / gl) if gl > 0 else float("inf") if gp > 0 else 0.0
    return {
        "variant": label,
        "trades": len(trades),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "profit_factor": round(pf, 2) if pf != float("inf") else 999,
        "avg_win_pct": round(gp / len(wins), 2) if wins else 0,
        "avg_loss_pct": round(-gl / len(losses), 2) if losses else 0,
        "expectancy_pct": round(sum(t["pnl_pct"] for t in trades) / len(trades), 3),
        "total_pct": round(sum(t["pnl_pct"] for t in trades), 1),
    }


# ---------------------------------------------------------------------------
# Strategy variants
# ---------------------------------------------------------------------------

def v_signal_count(f: pd.DataFrame) -> pd.Series:
    """Approximates the current app: majority of indicators agree."""
    up = (
        (f["close"] > f["sma20"]).astype(int)
        + (f["sma20"] > f["sma50"]).astype(int)
        + (f["sma50"] > f["sma200"]).astype(int)
        + (f["macd"] > f["macd_signal"]).astype(int)
        + (f["close"] > f["sma200"]).astype(int)
    )
    return (up >= 4).astype(int)


def v_trend_pullback(f: pd.DataFrame) -> pd.Series:
    """Uptrend + pullback: price above 200SMA, dipped below 50SMA, MACD turning up."""
    trend = f["close"] > f["sma200"]
    pulled = f["close"] < f["sma50"]
    macd_up = f["macd"] > f["macd_signal"]
    return (trend & pulled & macd_up).astype(int)


def v_trend_pullback_rsi(f: pd.DataFrame) -> pd.Series:
    """Trend + pullback, but demand RSI is actually washed out (better entry)."""
    trend = f["close"] > f["sma200"]
    pulled = f["close"] < f["sma50"]
    macd_up = f["macd"] > f["macd_signal"]
    washed = f["rsi14"] < 50
    return (trend & pulled & macd_up & washed).astype(int)


def v_bb_bounce(f: pd.DataFrame) -> pd.Series:
    """Uptrend + close back inside the lower Bollinger band (mean reversion)."""
    trend = f["close"] > f["sma200"]
    bounce = (f["close"] > f["bb_lower"]) & (f["low"] <= f["bb_lower"] * 1.01)
    macd_up = f["macd"] > f["macd_signal"]
    return (trend & bounce & macd_up).astype(int)


def v_breakout_retest(f: pd.DataFrame) -> pd.Series:
    """New 20-bar high, then a close back above the 20SMA with volume support."""
    new_high = f["high"] >= f["high"].rolling(20).max().shift(1)
    above = f["close"] > f["sma20"]
    vol_ok = f["vol_ratio"] > 1.0
    trend = f["close"] > f["sma200"]
    return (trend & new_high & above & vol_ok).astype(int)


VARIANTS = {
    "A_signal_count_atr35_t2R": (
        v_signal_count, "atr", 3.5, 2.0, None),
    "B_pullback_atr2.5_t1.5R": (
        v_trend_pullback, "atr", 2.5, 1.5, None),
    "C_pullback_swingstop_t1.5R": (
        v_trend_pullback, "swing", 2.5, 1.5, None),
    "D_pullback_swing_trail": (
        v_trend_pullback, "swing", 2.5, None, 2.5),
    "E_pullback_rsi_swing_t1.5R": (
        v_trend_pullback_rsi, "swing", 2.5, 1.5, None),
    "F_pullback_rsi_swing_trail": (
        v_trend_pullback_rsi, "swing", 2.5, None, 2.5),
    "G_bb_bounce_swing_t1.5R": (
        v_bb_bounce, "swing", 3.0, 1.5, None),
    "H_bb_bounce_swing_trail": (
        v_bb_bounce, "swing", 3.0, None, 2.5),
    "I_breakout_swing_trail": (
        v_breakout_retest, "swing", 2.5, None, 2.5),
}

# Sweep: how does win rate move as we demand less reward per unit of risk?
# Used to quantify the win-rate vs profit-factor tradeoff honestly.
SWEEP = {
    "S_t0.6": (v_trend_pullback, "swing", 2.5, 0.6, None),
    "S_t0.8": (v_trend_pullback, "swing", 2.5, 0.8, None),
    "S_t1.0": (v_trend_pullback, "swing", 2.5, 1.0, None),
    "S_t1.2": (v_trend_pullback, "swing", 2.5, 1.2, None),
    "S_t1.5": (v_trend_pullback, "swing", 2.5, 1.5, None),
    "S_t2.0": (v_trend_pullback, "swing", 2.5, 2.0, None),
    "S_t3.0": (v_trend_pullback, "swing", 2.5, 3.0, None),
    # Wide stop + small target: the classic way to inflate win rate.
    "W_wide4_t0.5": (v_trend_pullback, "atr", 4.0, 0.5, None),
    "W_wide5_t0.4": (v_trend_pullback, "atr", 5.0, 0.4, None),
    "W_wide6_t0.3": (v_trend_pullback, "atr", 6.0, 0.3, None),
    # Trailing-only exits with very wide trails.
    "T_trail3.5": (v_trend_pullback, "swing", 3.5, None, 3.5),
    "T_trail4.5": (v_trend_pullback, "swing", 4.5, None, 4.5),
}


def main() -> None:
    import logging
    logging.disable(logging.CRITICAL)

    data = load_all()
    if not data:
        print("No data available.")
        return

    per_symbol: Dict[str, List[Dict[str, Any]]] = {}
    for label, (fn, stop_kind, sm, tr, trail) in VARIANTS.items():
        for sym, df in data.items():
            f = build_features(df).dropna()
            if len(f) < 60:
                continue
            e = fn(f)
            trades = simulate(f, e, stop_kind, sm, tr, trail)
            per_symbol.setdefault(label, []).extend(
                [{**t, "symbol": sym} for t in trades]
            )

    rows = [metrics(per_symbol.get(label, []), label) for label in VARIANTS]
    rows.sort(key=lambda r: (-r.get("win_rate", 0)))

    out = ROOT / "scripts" / "experiment_results.json"
    out.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    def show(rs, title):
        print(title)
        print(f"{'variant':<32} {'trades':>7} {'win%':>7} {'PF':>7} "
              f"{'avgW':>7} {'avgL':>7} {'exp%':>8} {'tot%':>9}")
        print("-" * 88)
        for r in rs:
            print(f"{r['variant']:<32} {r.get('trades',0):>7} {r.get('win_rate',0):>7} "
                  f"{r.get('profit_factor',0):>7} {r.get('avg_win_pct',0):>7} "
                  f"{r.get('avg_loss_pct',0):>7} {r.get('expectancy_pct',0):>8} "
                  f"{r.get('total_pct',0):>9}")
        print()

    show(rows, "=== ENTRY STRATEGY COMPARISON (sorted by win rate) ===")

    sweep_trades: Dict[str, List[Dict[str, Any]]] = {}
    for label, (fn, stop_kind, sm, tr, trail) in SWEEP.items():
        for sym, df in data.items():
            f = build_features(df).dropna()
            if len(f) < 60:
                continue
            e = fn(f)
            sweep_trades.setdefault(label, []).extend(simulate(f, e, stop_kind, sm, tr, trail))

    srows = [metrics(sweep_trades.get(label, []), label) for label in SWEEP]
    srows.sort(key=lambda r: (-r.get("win_rate", 0)))
    show(srows, "=== WIN-RATE vs PROFIT-FACTOR TRADEOFF (sorted by win rate) ===")

    out2 = ROOT / "scripts" / "experiment_sweep.json"
    out2.write_text(json.dumps(srows, indent=2), encoding="utf-8")
    print(f"Wrote {out} and {out2}")


if __name__ == "__main__":
    main()