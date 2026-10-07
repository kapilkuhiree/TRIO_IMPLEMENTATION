"""
TRIO — Intraday (15m) variant validation.
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

Problem: the daily pullback setup (200/50 SMAs) was validated out-of-sample
on 5y of daily bars. The intraday variant (50/20 SMAs on 15m bars) was built
by analogy and has never been tested. Running untested parameters live is
exactly how accounts get hurt, so this script measures the intraday variant
on 15m data before trusting it.

yfinance serves ~60d of 15m history (~1400 bars/symbol for NSE). That is
enough for a train/test split: first 40d train, last 20d test.

Run: python scripts/validate_intraday.py
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from experiment import BASKET, metrics  # noqa: E402

CACHE = ROOT / "scripts" / "_data_cache_15m"
PERIOD_15M = "60d"

# Variants to test on 15m bars. Same exit family as the daily validation
# (swing stop, fixed R target) so results are comparable.
CANDIDATES = {
    # (entry_fn_name, stop_kind, stop_mult, target_r)
    "intra_pullback_swing_t1.5": ("pullback_50_20", "swing", 2.5, 1.5),
    "intra_pullback_swing_t1.0": ("pullback_50_20", "swing", 2.5, 1.0),
    "intra_pullback_swing_t2.0": ("pullback_50_20", "swing", 2.5, 2.0),
    "intra_pullback_atr_t1.5": ("pullback_50_20", "atr", 2.5, 1.5),
    "intra_rsi_pullback_t1.5": ("pullback_50_20_rsi", "swing", 2.5, 1.5),
    "intra_trendonly_t1.5": ("trend_only", "swing", 2.5, 1.5),
}


def load_15m(symbol: str) -> Optional[pd.DataFrame]:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{symbol.replace('.', '_')}_15m.csv"
    if path.exists():
        return pd.read_csv(path, index_col=0, parse_dates=True)
    try:
        import yfinance as yf
        raw = yf.Ticker(symbol).history(period=PERIOD_15M, interval="15m")
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


def features_15m(df: pd.DataFrame) -> pd.DataFrame:
    """Same feature set as the daily harness, shorter windows."""
    f = pd.DataFrame(index=df.index)
    close = df["Close"]
    f["close"] = close
    f["high"] = df["High"]
    f["low"] = df["Low"]
    f["sma20"] = close.rolling(20).mean()
    f["sma50"] = close.rolling(50).mean()
    f["sma200"] = close.rolling(200).mean()

    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    f["rsi14"] = 100 - (100 / (1 + rs))

    prev = close.shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev).abs(),
        (df["Low"] - prev).abs(),
    ], axis=1).max(axis=1)
    f["atr14"] = tr.rolling(14).mean()

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    f["macd"] = macd
    f["macd_signal"] = macd.ewm(span=9, adjust=False).mean()
    f["swing_low"] = df["Low"].rolling(10).min().shift(1)
    return f


def entries(f: pd.DataFrame, name: str) -> pd.Series:
    if name == "pullback_50_20":
        return ((f["close"] > f["sma50"])
                & (f["close"] < f["sma20"])
                & (f["macd"] > f["macd_signal"])).astype(int)
    if name == "pullback_50_20_rsi":
        return ((f["close"] > f["sma50"])
                & (f["close"] < f["sma20"])
                & (f["macd"] > f["macd_signal"])
                & (f["rsi14"] < 50)).astype(int)
    if name == "trend_only":
        return ((f["close"] > f["sma50"])
                & (f["macd"] > f["macd_signal"])).astype(int)
    raise ValueError(name)


def simulate(f: pd.DataFrame, e: pd.Series, kind: str,
             sm: float, tr: float) -> List[Dict[str, Any]]:
    trades: List[Dict[str, Any]] = []
    in_trade = False
    stop = target = entry = 0.0
    rows = f.to_dict("records")
    n, i = len(rows), 0
    while i < n:
        row = rows[i]
        if in_trade:
            if row["low"] <= stop:
                trades.append({"pnl_pct": (stop - entry) / entry * 100})
                in_trade = False
                i += 1
                continue
            if row["high"] >= target:
                trades.append({"pnl_pct": (target - entry) / entry * 100})
                in_trade = False
                i += 1
                continue
            i += 1
            continue
        if e.iloc[i] == 1:
            entry = row["close"]
            a = row["atr14"]
            if kind == "swing":
                sl = row["swing_low"]
                stop = sl if sl == sl and sl > 0 else entry - sm * a
            else:
                stop = entry - sm * a
            risk = entry - stop
            if risk <= 0 or not (a == a):
                i += 1
                continue
            target = entry + risk * tr
            in_trade = True
        i += 1
    if in_trade:
        last = rows[-1]["close"]
        trades.append({"pnl_pct": (last - entry) / entry * 100})
    return trades


def main() -> None:
    import logging
    logging.disable(logging.CRITICAL)
    print("Loading 60d of 15m data...")
    data: Dict[str, pd.DataFrame] = {}
    for sym in BASKET:
        df = load_15m(sym)
        if df is not None and len(df) > 300:
            data[sym] = df
    print(f"Loaded {len(data)} symbols\n")

    print(f"{'config':<28} | {'TRAIN win%':>10} {'PF':>6} {'n':>5} "
          f"| {'TEST win%':>10} {'PF':>6} {'n':>5}")
    print("-" * 80)
    rows = []
    for label, (fn, kind, sm, tr) in CANDIDATES.items():
        train: List[Dict[str, Any]] = []
        test: List[Dict[str, Any]] = []
        for sym, df in data.items():
            f = features_15m(df).dropna()
            if len(f) < 200:
                continue
            cut = int(len(f) * 0.66)
            tr_f, te_f = f.iloc[:cut], f.iloc[cut:]
            if len(tr_f) > 60:
                train.extend(simulate(tr_f, entries(tr_f, fn), kind, sm, tr))
            if len(te_f) > 60:
                test.extend(simulate(te_f, entries(te_f, fn), kind, sm, tr))
        tr_m = metrics(train, label)
        te_m = metrics(test, label)
        rows.append({"config": label, "train": tr_m, "test": te_m})
        print(f"{label:<28} | {tr_m.get('win_rate',0):>10} "
              f"{tr_m.get('profit_factor',0):>6} {tr_m.get('trades',0):>5} "
              f"| {te_m.get('win_rate',0):>10} "
              f"{te_m.get('profit_factor',0):>6} {te_m.get('trades',0):>5}")

    out = ROOT / "scripts" / "validation_intraday.json"
    out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print()
    ok = [r for r in rows if r["test"].get("profit_factor", 0) >= 1.2
          and r["test"].get("trades", 0) >= 15]
    if ok:
        print("Survived 15m out-of-sample (test PF >= 1.2):")
        for r in ok:
            print(f"  {r['config']:<28} win%={r['test']['win_rate']} "
                  f"PF={r['test']['profit_factor']} n={r['test']['trades']}")
    else:
        print("No 15m config survived. The intraday variant is NOT validated — "
              "do not trust it live; use daily signals or stay flat.")
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
