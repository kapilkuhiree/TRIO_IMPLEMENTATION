"""Diagnose which gate kills signals: trend / pullback / macd / composite / sizing.
Author: Kapil Kuhire <kapilkuhire89@gmail.com>
"""
import sys
from pathlib import Path
import logging
logging.disable(logging.CRITICAL)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.data_fetcher import fetch_market_data
from src.indicators import compute_indicators
from src.signal_engine import generate_signal
from src.risk_manager import apply_risk_management

for sym in ["RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "SBIN.NS"]:
    md = fetch_market_data(sym, "1d", "2y")
    df = md.ohlcv
    n = len(df)
    c = {"bars": 0, "trend": 0, "pull": 0, "macd": 0, "composite_buy": 0,
         "sized": 0}
    for i in range(200, n):
        window = df.iloc[:i + 1]
        close = float(df["Close"].iloc[i])
        try:
            r = compute_indicators(window, sym, "1d")
        except Exception:
            continue
        c["bars"] += 1
        s50 = r.indicators.get("sma_50")
        s200 = r.indicators.get("sma_200")
        macd = r.indicators.get("macd")
        if s200 and s200.value and close > s200.value:
            c["trend"] += 1
        else:
            continue
        if s50 and s50.value and close < s50.value:
            c["pull"] += 1
        else:
            continue
        if macd and macd.value is not None and macd.value > (macd.extra.get("signal_line") or 0):
            c["macd"] += 1
        else:
            continue
        sig = generate_signal(sym, close, r)
        if sig.action == "BUY":
            c["composite_buy"] += 1
            atr = next((v.value for k, v in r.indicators.items() if k.startswith("atr_")), None)
            swing = r.swing_low
            sig2 = apply_risk_management(sig, atr, swing_level=swing)
            if sig2.action == "BUY" and sig2.position_size:
                c["sized"] += 1
    print(f"{sym}: bars={c['bars']} trend={c['trend']} "
          f"pull={c['pull']} macd={c['macd']} "
          f"buy={c['composite_buy']} sized={c['sized']}")
