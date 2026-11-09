"""
Backtest the current ORB + Momentum + Pullback strategy on the last 5 trading
sessions of NIFTY and BANKNIFTY.  Uses yfinance 5m data and the same
signal logic as the live scanner (ORB, pullback, momentum).
"""

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import yfinance as yf

IST = timezone(timedelta(hours=5, minutes=30))
NIFTY = "^NSEI"
BANKNIFTY = "^NSEBANK"
LOT = 65  # NIFTY
LOT_BANK = 30  # BANKNIFTY

STOP_PCT = -0.20   # exit at -20% of premium
TARGET_PCT = 0.30  # exit at +30% of premium


def fetch_5m(symbol: str, days: int = 10) -> pd.DataFrame:
    df = yf.download(symbol, period=f"{days}d", interval="5m",
                     auto_adjust=False, progress=False)
    if isinstance(df.columns, object) and hasattr(df.columns, "levels"):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["Close"])
    df.index = pd.to_datetime(df.index, utc=True).tz_convert(IST)
    return df


def vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["High"] + df["Low"] + df["Close"]) / 3
    return (typical * df["Volume"]).cumsum() / df["Volume"].cumsum()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    gain = d.clip(lower=0)
    loss = -d.clip(upper=0)
    avg_gain = gain.rolling(n).mean()
    avg_loss = loss.rolling(n).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    hl = df["High"] - df["Low"]
    hc = (df["High"] - df["Close"].shift()).abs()
    lc = (df["Low"] - df["Close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def signal_5m(symbol: str, df: pd.DataFrame) -> Optional[Dict]:
    """Return a trade dict if a valid signal is found, else None."""
    if len(df) < 35:
        return None

    # Opening range = first 3 bars (09:15-09:30 IST)
    # Corrected: Need to calculate based on the current day only.
    # The signal_5m function is passed the WHOLE day's dataframe (df_day).
    # ORB high should be on df.iloc[:3] of the CURRENT day.
    # The current signal_5m is called with 'df' which is already the day's df.
    or_high = df.iloc[:3]["High"].max()
    or_low = df.iloc[:3]["Low"].min()
    vwap_s = vwap(df)

    # Momentum: 3 consecutive up/down candles above VWAP
    last = df.iloc[-1]
    last_vwap = vwap_s.iloc[-1]

    # 1. ORB breakout
    if last["Close"] > or_high:
        from src.options_live_scan import _session_vwap
        vwap_val = _session_vwap(df.to_dict('records'))
        if vwap_val and last["Close"] < vwap_val:
             return None
        return {"action": "CE", "entry": last["Close"], "signal": "ORB-CE",
                "time": df.index[-1]}
    if last["Close"] < or_low:
        from src.options_live_scan import _session_vwap
        vwap_val = _session_vwap(df.to_dict('records'))
        if vwap_val and last["Close"] > vwap_val:
             return None
        return {"action": "PE", "entry": last["Close"], "signal": "ORB-PE",
                "time": df.index[-1]}

    # 2. Momentum: 3 consecutive green candles above VWAP
    c = df["Close"]
    o = df["Open"]
    if (len(df) >= 4 and c.iloc[-1] > c.iloc[-2] > c.iloc[-3] > c.iloc[-4]
            and last_vwap < last["Close"]):
        return {"action": "CE", "entry": last["Close"], "signal": "MOM-CE",
                "time": df.index[-1]}
    if (len(df) >= 4 and c.iloc[-1] < c.iloc[-2] < c.iloc[-3] < c.iloc[-4]
            and last_vwap > last["Close"]):
        return {"action": "PE", "entry": last["Close"], "signal": "MOM-PE",
                "time": df.index[-1]}

    # 3. Pullback to VWAP
    rsi_s = rsi(df["Close"])
    if rsi_s.iloc[-1] < 30 and last["Close"] < last_vwap:
        return {"action": "PE", "entry": last["Close"], "signal": "PULL-PE",
                "time": df.index[-1]}
    if rsi_s.iloc[-1] > 70 and last["Close"] > last_vwap:
        return {"action": "CE", "entry": last["Close"], "signal": "PULL-CE",
                "time": df.index[-1]}

    return None


def price_option(spot: float, strike: float, dte: int, vol: float,
                 kind: str) -> float:
    from src.options_pricing import bs_price
    return bs_price(spot, strike, max(dte, 1) / 365.0, vol, kind)


def run_backtest() -> None:
    print("Fetching NIFTY & BANKNIFTY 5m data ...")
    df_nifty = fetch_5m(NIFTY)
    df_bank = fetch_5m(BANKNIFTY)

    # Group by day
    days_nifty = {d: g for d, g in df_nifty.groupby(df_nifty.index.date)}
    days_bank = {d: g for d, g in df_bank.groupby(df_bank.index.date)}

    all_days = sorted(set(list(days_nifty.keys()) + list(days_bank.keys())))
    last_5 = all_days[-5:]

    print(f"Sessions to backtest: {[str(d) for d in last_5]}\n")

    trades = []
    for day in last_5:
        for label, df_day, lot in (
            ("NIFTY", days_nifty.get(day), LOT),
            ("BANKNIFTY", days_bank.get(day), LOT_BANK),
        ):
            if df_day is None or len(df_day) < 10:
                continue
            sig = signal_5m(label, df_day)
            if not sig:
                continue

            spot = sig["entry"]
            strike = round(spot / 50) * 50
            kind = sig["action"]
            dte = 5
            vol = 0.18  # typical NIFTY ATM IV
            entry_premium = price_option(spot, strike, dte, vol, kind)
            if entry_premium <= 0:
                continue

            stop = entry_premium * (1 + STOP_PCT)
            target = entry_premium * (1 + TARGET_PCT)

            # Replay remaining bars of the day
            idx = df_day.index.get_loc(sig["time"])
            remainder = df_day.iloc[idx + 1:]
            exit_px, reason = entry_premium, "eod"

            print(f"DEBUG: ENTRY: spot={spot}, strike={strike}, premium={entry_premium}, stop={stop}, target={target}")
            for ts, bar in remainder.iterrows():
                # Reprice option using bar spot
                new_spot = bar["Close"]
                val = price_option(new_spot, strike, dte, vol, kind)
                # print(f"DEBUG: spot={new_spot}, premium={val}, stop={stop}, target={target}")
                if kind == "CE":
                    if val <= stop:
                        exit_px, reason = val, "stop"
                        break
                    if val >= target:
                        exit_px, reason = val, "target"
                        break
                else:
                    # For PE, price moves down when spot moves down
                    # BS handles direction correctly
                    if val <= stop:
                        exit_px, reason = val, "stop"
                        break
                    if val >= target:
                        exit_px, reason = val, "target"
                        break
                exit_px = val

            pnl = round((exit_px - entry_premium) * lot, 2)
            trades.append({
                "day": str(day),
                "symbol": label,
                "signal": sig["signal"],
                "action": kind,
                "strike": strike,
                "entry": round(entry_premium, 2),
                "exit": round(exit_px, 2),
                "exit_reason": reason,
                "pnl": pnl,
                "win": pnl > 0,
            })

    print(f"{'Day':<12} {'Symbol':<12} {'Signal':<10} {'Entry':>8} "
          f"{'Exit':>8} {'Exit Reason':<12} {'P&L':>10}")
    print("-" * 85)
    total_pnl = 0
    wins = 0
    for t in trades:
        print(f"{t['day']:<12} {t['symbol']:<12} {t['signal']:<10} "
              f"{t['entry']:>8.2f} {t['exit']:>8.2f} "
              f"{t['exit_reason']:<12} {t['pnl']:>+10,.2f}")
        total_pnl += t["pnl"]
        if t["win"]:
            wins += 1

    print("-" * 85)
    n = len(trades)
    wr = round(wins / n * 100, 1) if n else 0
    print(f"\nTotal trades: {n}   Wins: {wins}   Win rate: {wr}%")
    print(f"Net P&L: {total_pnl:,.2f} INR")


if __name__ == "__main__":
    run_backtest()
