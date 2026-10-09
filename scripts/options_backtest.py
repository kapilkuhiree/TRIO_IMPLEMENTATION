"""
TRIO — Options Strategy Backtest Harness (offline, NSE-chain-free)
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

Walks NIFTY 50 (^NSEI) daily bars and replays the options overlay the way
the live session runs it:

  signal (daily pullback engine) -> regime (ADX) -> strategy pick ->
  entry (next-open BS premium) -> intraday path (high/low BS repricing) ->
  exit (stop/target/EOD) -> P&L in rupees per lot.

Pricing is Black-Scholes with a fixed vol proxy (default 18% — close to
typical NIFTY ATM IV), so no NSE chain or network is needed. That makes
longs/spreads comparable on identical assumptions; absolute rupees are
model estimates, RELATIVE rankings across variants are the decision input.

Variants (style, long-delta, width, exits):
  A  long-ITM-0.65   (user proposal: naked BUY, slightly ITM)
  B  spread-ATM      (current default: bull/bear debit spread)
  C  spread-ITM-0.65 (ITM long + OTM short)
  D  long-ATM-0.50   (naked ATM — theta-bleed baseline)

Exits: spread/long stop -50% of debit, target +35% of debit, else EOD.
Hold: entry next open -> same-day EOD only (intraday, like the session).

Usage:
    python scripts/options_backtest.py [--days 60] [--vol 0.18]
        [--variants A B C D] [--no-fetch]

DISCLAIMER: Educational purposes only. Not financial advice.
Past performance does not indicate future results.
"""

import argparse
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.utils import load_config, get_logger  # noqa: E402
from src.options_pricing import bs_price  # noqa: E402

logger = get_logger("options_backtest")

IST = timezone(timedelta(hours=5, minutes=30))
NIFTY = "^NSEI"
LOT = 75  # NSE NIFTY lot (2025+)

STOP_PCT = -0.50   # exit spread/long at -50% of debit
TARGET_PCT = 0.35  # exit at +35% of debit


@dataclass
class OptTrade:
    date: str = ""
    action: str = ""
    variant: str = ""
    strategy: str = ""
    expiry_days: int = 7
    entry_debit: float = 0.0
    exit_value: float = 0.0
    pnl: float = 0.0
    exit_reason: str = ""
    entry_spot: float = 0.0
    exit_spot: float = 0.0


def _fetch_nifty(days: int = 400):
    """Daily NIFTY bars via yfinance (cached in-memory per run)."""
    import yfinance as yf
    df = yf.download(NIFTY, period=f"{days}d", interval="1d",
                     auto_adjust=False, progress=False)
    if df is None or len(df) == 0:
        raise RuntimeError("no NIFTY data")
    if isinstance(df.columns, object) and hasattr(df.columns, "levels"):
        try:
            df.columns = df.columns.get_level_values(0)
        except Exception:
            pass
    df = df.dropna(subset=["Close"])
    return df


def _sma(s: "np.ndarray", n: int) -> "np.ndarray":
    out = np.full(len(s), np.nan)
    if len(s) >= n:
        c = np.cumsum(np.insert(s, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def _rsi(close: np.ndarray, n: int = 14) -> np.ndarray:
    out = np.full(len(close), np.nan)
    if len(close) <= n:
        return out
    d = np.diff(close)
    gain = np.where(d > 0, d, 0.0)
    loss = np.where(d < 0, -d, 0.0)
    ag = np.convolve(gain, np.ones(n) / n, mode="valid")
    al = np.convolve(loss, np.ones(n) / n, mode="valid")
    rs = np.divide(ag, al, out=np.full_like(ag, np.inf), where=al != 0)
    out[n:] = 100.0 - 100.0 / (1.0 + rs)
    return out


def _macd(close: np.ndarray, fast=12, slow=26, sig=9):
    def ema(x, n):
        k = 2.0 / (n + 1.0)
        e = np.full(len(x), np.nan)
        e[0] = x[0]
        for i in range(1, len(x)):
            e[i] = x[i] * k + e[i - 1] * (1 - k)
        return e
    m = ema(close, fast) - ema(close, slow)
    s = ema(np.nan_to_num(m, nan=0.0), sig)
    return m, s


def _adx(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int = 14) -> np.ndarray:
    out = np.full(len(c), np.nan)
    if len(c) <= 2 * n:
        return out
    up = np.diff(h)
    dn = -np.diff(l)
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = np.maximum(h[1:] - l[1:], np.maximum(
        abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])))
    atr = np.convolve(tr, np.ones(n) / n, mode="valid")
    pdm = np.convolve(plus_dm, np.ones(n) / n, mode="valid")
    mdm = np.convolve(minus_dm, np.ones(n) / n, mode="valid")
    with np.errstate(invalid="ignore", divide="ignore"):
        pdi = 100.0 * pdm / atr
        mdi = 100.0 * mdm / atr
        dx = 100.0 * np.abs(pdi - mdi) / (pdi + mdi + 1e-12)
    ax = np.convolve(dx, np.ones(n) / n, mode="valid")
    start = len(c) - len(ax)
    out[start:] = ax
    return out


def _strikes_around(spot: float, step: int = 50, n: int = 12):
    base = round(spot / step) * step
    return [base + step * i for i in range(-n, n + 1)]


def _delta_proxy(spot: float, strike: float, kind: str) -> float:
    """Crude delta mirroring the selector's moneyness proxy (for targeting)."""
    dist = abs(strike - spot) / spot if spot else 1.0
    d = max(0.5 - dist * 17.5, 0.05)
    return d if kind == "CE" else -d


def _pick_strike(spot: float, kind: str, target_delta: float,
                 side: str = "OTM") -> float:
    best, best_d = None, 1e9
    for k in _strikes_around(spot):
        if side == "OTM":
            if kind == "CE" and k < spot:
                continue
            if kind == "PE" and k > spot:
                continue
        elif side == "ITM":
            if kind == "CE" and k > spot:
                continue
            if kind == "PE" and k < spot:
                continue
        d = abs(abs(_delta_proxy(spot, k, kind)) - target_delta)
        if d < best_d:
            best_d, best = d, k
    return best if best is not None else round(spot / 50) * 50


def _bs(spot: float, strike: float, dte_days: int, vol: float,
        kind: str) -> float:
    return bs_price(spot, strike, max(dte_days, 1) / 365.0, vol, kind)


def _enter(variant: str, action: str, spot: float, dte: int,
           vol: float) -> Tuple[str, List[Tuple[float, str, str]],
                                 float, float]:
    """Returns (strategy, [(strike, kind, side)], debit, width)."""
    kind = "CE" if action == "BUY" else "PE"
    if variant in ("A", "D"):
        tgt = 0.65 if variant == "A" else 0.50
        k = _pick_strike(spot, kind, tgt, side="ITM" if variant == "A"
                         else "ATM" if False else "OTM")
        # ATM naked: nearest strike (may be OTM side); ITM: ITM side
        if variant == "D":
            k = min(_strikes_around(spot), key=lambda x: abs(x - spot))
        px = _bs(spot, k, dte, vol, kind)
        strat = "long-call" if kind == "CE" else "long-put"
        return strat, [(k, kind, "BUY")], px, 0.0
    # spreads
    if variant == "B":
        long_k = min(_strikes_around(spot), key=lambda x: abs(x - spot))
        short_k = _pick_strike(spot, kind, 0.30, side="OTM")
    else:  # C: ITM long
        long_k = _pick_strike(spot, kind, 0.65, side="ITM")
        short_k = _pick_strike(spot, kind, 0.30, side="OTM")
    if short_k == long_k:
        step = 50
        short_k = long_k + step if kind == "CE" else long_k - step
    lp = _bs(spot, long_k, dte, vol, kind)
    sp = _bs(spot, short_k, dte, vol, kind)
    if sp >= lp:
        sp = round(lp * 0.35, 2)
    debit = round(max(lp - sp, 0.01), 2)
    width = abs(long_k - short_k)
    strat = "bull-call-spread" if kind == "CE" else "bear-put-spread"
    return strat, [(long_k, kind, "BUY"), (short_k, kind, "SELL")], debit, width


def _value(legs, spot: float, dte: int, vol: float) -> float:
    v = 0.0
    for k, kind, side in legs:
        px = _bs(spot, k, dte, vol, kind)
        v += px if side == "BUY" else -px
    return max(round(v, 2), 0.0)


def run_variant(df, variant: str, vol: float = 0.18,
                dte_entry: int = 3) -> List[OptTrade]:
    """One variant over the frame. Signal: daily pullback clone.

    BUY: close > sma200, pullback (low <= sma50*1.01 and close > low),
    MACD line > signal. SELL mirror. ADX>=25 required (else skip — buy-only
    book sits out chop in long mode; spreads skip condor entirely).
    Entry next open, exit stop/target/EOD on high/low BS path.
    """
    o = df["Open"].to_numpy(float)
    h = df["High"].to_numpy(float)
    l = df["Low"].to_numpy(float)
    c = df["Close"].to_numpy(float)
    idx = df.index
    sma50, sma200 = _sma(c, 50), _sma(c, 200)
    rsi = _rsi(c)
    mline, msig = _macd(c)
    adx = _adx(h, l, c)
    trades: List[OptTrade] = []
    for i in range(200, len(df) - 1):
        if any(math.isnan(x) for x in
               (sma50[i], sma200[i], rsi[i], mline[i], msig[i])):
            continue
        ax = adx[i] if not math.isnan(adx[i]) else 30.0
        if ax < 25.0:
            continue  # chop: buy-only book sits out
        pullback = l[i] <= sma50[i] * 1.01 and c[i] > l[i]
        bull = (c[i] > sma200[i] and pullback and mline[i] > msig[i])
        bear = (c[i] < sma200[i] and h[i] >= sma50[i] * 0.99
                and c[i] < h[i] and mline[i] < msig[i])
        if bull == bear:
            continue
        action = "BUY" if bull else "SELL"
        entry_spot = float(o[i + 1])
        strat, legs, debit, width = _enter(variant, action, entry_spot,
                                           dte_entry, vol)
        if debit <= 0:
            continue
        stop_v = debit * (1.0 + STOP_PCT)
        tgt_v = debit * (1.0 + TARGET_PCT)
        # NOTE: BS on HIGH/LOW overstates intraday excursion (spot H/L with
        # full-day T but the option would reprice continuously). Use the
        # EOD close for exits: conservative and honest for daily bars.
        eod_v = _value(legs, float(c[i + 1]), dte_entry, vol)
        if eod_v <= stop_v:
            exit_v, reason = eod_v, "spread-stop"
        elif eod_v >= tgt_v:
            exit_v, reason = eod_v, "spread-target"
        else:
            exit_v, reason = eod_v, "eod"
        pnl = round((exit_v - debit) * LOT, 2)
        trades.append(OptTrade(
            date=str(idx[i + 1].date()) if hasattr(idx[i + 1], "date")
            else str(idx[i + 1]),
            action=action, variant=variant, strategy=strat,
            expiry_days=dte_entry, entry_debit=debit, exit_value=exit_v,
            pnl=pnl, exit_reason=reason, entry_spot=entry_spot,
            exit_spot=float(c[i + 1])))
    return trades


def summarize(trades: List[OptTrade]) -> Dict[str, Any]:
    n = len(trades)
    wins = sum(1 for t in trades if t.pnl > 0)
    pnl = round(sum(t.pnl for t in trades), 2)
    gp = sum(t.pnl for t in trades if t.pnl > 0)
    gl = -sum(t.pnl for t in trades if t.pnl < 0)
    pf = round(gp / gl, 2) if gl > 0 else float("inf") if gp > 0 else 0.0
    avg = round(pnl / n, 2) if n else 0.0
    wr = round(wins / n * 100, 1) if n else 0.0
    reasons: Dict[str, int] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    worst = round(min((t.pnl for t in trades), default=0.0), 2)
    best = round(max((t.pnl for t in trades), default=0.0), 2)
    return {"trades": n, "wins": wins, "win_rate": wr, "pnl": pnl,
            "profit_factor": pf, "avg": avg, "best": best, "worst": worst,
            "exits": reasons}


def main() -> None:
    ap = argparse.ArgumentParser(description="TRIO options backtest")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--vol", type=float, default=0.18)
    ap.add_argument("--variants", nargs="*", default=["A", "B", "C", "D"])
    ap.add_argument("--dte", type=int, default=3)
    args = ap.parse_args()

    print(f"Fetching {args.days + 260} NIFTY daily bars ...")
    df = _fetch_nifty(args.days + 260)
    df = df.iloc[-(args.days + 220):]
    print(f"Bars: {len(df)} ({df.index[0].date()} -> {df.index[-1].date()})")

    results = {}
    for v in args.variants:
        tr = run_variant(df, v, vol=args.vol, dte_entry=args.dte)
        results[v] = (tr, summarize(tr))
        s = results[v][1]
        print(f"\n[{v}] trades={s['trades']} win={s['win_rate']}% "
              f"pnl={s['pnl']:+.0f} PF={s['profit_factor']} "
              f"avg={s['avg']:+.0f} best={s['best']:+.0f} "
              f"worst={s['worst']:+.0f} exits={s['exits']}")

    names = {"A": "long-ITM-0.65", "B": "spread-ATM",
             "C": "spread-ITM-0.65", "D": "long-ATM-0.50"}
    print("\n==== RANKED (by total P&L) ====")
    for v, (_, s) in sorted(results.items(), key=lambda kv: kv[1][1]["pnl"],
                             reverse=True):
        print(f"  {v} {names.get(v, v)}: pnl={s['pnl']:+.0f} "
              f"win={s['win_rate']}% PF={s['profit_factor']} "
              f"n={s['trades']}")


if __name__ == "__main__":
    main()
