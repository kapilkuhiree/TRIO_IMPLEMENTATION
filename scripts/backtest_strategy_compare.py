"""
Head-to-head backtest: shipped strategy stack (ORB + Pullback + Momentum)
vs the new MeanReversion strategy.

Drives the REAL strategy classes on 5m NIFTY / BANKNIFTY data.
Option entries are priced with Black-Scholes (ATM, 5 DTE, 18% vol).
Exits use the config's premium fractions: stop -20%, target +25%
(plus a +30% sensitivity run), checked on completed bars like the live runtime.

One entry per strategy per symbol per day (first valid signal).

Usage:
    python scripts/backtest_strategy_compare.py [--days 60] [--target 0.25]
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.options_pricing import bs_price
from strategies.orb_strategy import OrbStrategy
from strategies.trend_pullback_strategy import TrendPullbackStrategy
from strategies.momentum_continuation_strategy import MomentumContinuationStrategy
from strategies.mean_reversion_strategy import MeanReversionStrategy
import src.options_live_scan as scan

IST = timezone(timedelta(hours=5, minutes=30))

SYMBOLS = {
    "^NSEI": {"name": "NIFTY", "lot": 65, "step": 50, "vol": 0.18},
    "^NSEBANK": {"name": "BANKNIFTY", "lot": 30, "step": 100, "vol": 0.18},
}

STOP_FRACTION = 0.20
DTE_DAYS = 5
ENTRY_CUTOFF = pd.Timestamp("14:30").time()
HARD_FLAT = pd.Timestamp("15:10").time()


def _vwap_with_fallback(candles) -> Optional[float]:
    """Volume-less VWAP fallback so index feeds (Volume=0) are usable."""
    from datetime import datetime as _dt
    try:
        dates = []
        for row in candles:
            try:
                dates.append(_dt.fromisoformat(
                    str(row.get("timestamp")).replace("Z", "+00:00")).date())
            except (TypeError, ValueError):
                dates.append(None)
        known = [d for d in dates if d is not None]
        if not known:
            return None
        last = max(known)
        pv = vol_sum = 0.0
        for row, d in zip(candles, dates):
            if d != last:
                continue
            try:
                h = float(row.get("high"))
                l = float(row.get("low"))
                c = float(row.get("close"))
                v = float(row.get("volume") or 0.0)
            except (TypeError, ValueError):
                continue
            if c <= 0:
                continue
            typical = (h + l + c) / 3.0
            if v <= 0:
                pv += typical
                vol_sum += 1.0
            else:
                pv += typical * v
                vol_sum += v
        return pv / vol_sum if vol_sum > 0 else None
    except Exception:
        return None


scan._session_vwap = _vwap_with_fallback


class FixedPullback(TrendPullbackStrategy):
    """Pullback with the volume-less VWAP fallback (diagnostic variant)."""

    @staticmethod
    def _calculate_vwap(candles_5min, index, session_candles=None) -> float:
        from datetime import datetime as _dt
        candles_to_use = candles_5min if session_candles is None else session_candles
        if index >= len(candles_to_use):
            return 0.0
        try:
            current = _dt.fromisoformat(
                str(candles_to_use[index].get("timestamp")).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return 0.0
        day = current.date()
        symbol = candles_to_use[index].get("underlying_symbol")
        pv = vol_sum = 0.0
        for i in range(index + 1):
            candle = candles_to_use[i]
            try:
                ts = _dt.fromisoformat(
                    str(candle.get("timestamp")).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if ts.date() != day:
                continue
            if symbol and candle.get("underlying_symbol") not in (None, symbol):
                continue
            try:
                h = float(candle.get("high") or 0)
                l = float(candle.get("low") or 0)
                c = float(candle.get("close") or 0)
                v = float(candle.get("volume") or 0)
            except (TypeError, ValueError):
                continue
            if c <= 0:
                continue
            typical = (h + l + c) / 3.0
            if v <= 0:
                pv += typical
                vol_sum += 1.0
            else:
                pv += typical * v
                vol_sum += v
        return pv / vol_sum if vol_sum > 0 else 0.0


def fetch_5m(symbol: str, days: int) -> Optional[pd.DataFrame]:
    df = yf.download(symbol, period=f"{days}d", interval="5m",
                     auto_adjust=False, progress=False)
    if df is None or df.empty:
        return None
    if hasattr(df.columns, "levels"):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["Close"])
    df.index = pd.to_datetime(df.index, utc=True).tz_convert(IST)
    df = df[(df.index.time >= pd.Timestamp("09:15").time()) &
            (df.index.time <= pd.Timestamp("15:30").time())]
    return df


def to_candles(df: pd.DataFrame, name: str) -> List[Dict[str, Any]]:
    return [{
        "timestamp": ts.isoformat(),
        "open": float(r["Open"]), "high": float(r["High"]),
        "low": float(r["Low"]), "close": float(r["Close"]),
        "volume": float(r.get("Volume") or 0.0),
        "underlying_symbol": name, "exchange": "NSE", "instrument_type": "index",
    } for ts, r in df.iterrows()]


def premium(spot: float, strike: float, kind: str, vol: float) -> float:
    return bs_price(spot, strike, max(DTE_DAYS, 1) / 365.0, vol, kind)


def run_trade(candles, entry_idx, *, kind, lot, step, vol, target_fraction):
    if entry_idx + 1 >= len(candles):
        return None
    entry_bar = candles[entry_idx + 1]
    entry_day = entry_bar["timestamp"][:10]
    spot = float(entry_bar["open"])
    if spot <= 0:
        return None
    strike = round(spot / step) * step
    entry_prem = premium(spot, strike, kind, vol)
    if entry_prem <= 0:
        return None
    stop = entry_prem * (1.0 - STOP_FRACTION)
    target = entry_prem * (1.0 + target_fraction)

    exit_prem, reason, exit_ts = entry_prem, "eod", entry_bar["timestamp"]
    for bar in candles[entry_idx + 1:]:
        if bar["timestamp"][:10] != entry_day:
            break
        ts = pd.Timestamp(bar["timestamp"])
        if ts.time() > HARD_FLAT:
            break
        px = premium(float(bar["close"]), strike, kind, vol)
        exit_prem, exit_ts = px, bar["timestamp"]
        if px <= stop:
            reason = "stop"
            break
        if px >= target:
            reason = "target"
            break

    return {
        "strike": strike, "kind": kind,
        "entry_premium": round(entry_prem, 2),
        "exit_premium": round(exit_prem, 2),
        "reason": reason,
        "pnl": round((exit_prem - entry_prem) * lot, 2),
        "entry_ts": entry_bar["timestamp"], "exit_ts": exit_ts,
    }


def evaluate_day(day_df, candles, day_indices, *, name, lot, step, vol,
                 target_fraction, strategies) -> List[Dict[str, Any]]:
    taken: Dict[str, Dict[str, Any]] = {}
    signals = defaultdict(int)
    for i in day_indices:
        if i >= len(candles):
            break
        ts = pd.Timestamp(candles[i]["timestamp"])
        if ts.time() < pd.Timestamp("09:30").time() or ts.time() > ENTRY_CUTOFF:
            continue
        for sname, strat in strategies.items():
            if sname in taken:
                continue
            try:
                sig = (strat.evaluate_signal(candles, i) if sname == "mean_reversion"
                       else strat.evaluate_signal(
                           candles, i, {"lot_size": lot, "strike_step": step}, name))
            except Exception:
                continue
            if not getattr(sig, "allowed", False):
                continue
            kind = str(getattr(sig, "option_type", "") or "").upper()
            if kind not in {"CE", "PE"}:
                continue
            signals[sname] += 1
            trade = run_trade(candles, i, kind=kind, lot=lot, step=step,
                              vol=vol, target_fraction=target_fraction)
            if trade is None:
                continue
            trade.update(day=ts.date().isoformat(), symbol=name, strategy=sname)
            taken[sname] = trade
    return list(taken.values()) + [{"_signals": dict(signals)}]


def stats(trades: List[Dict[str, Any]], label: str) -> Dict[str, Any]:
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    ordered = sorted(trades, key=lambda t: t["exit_ts"])
    cum = peak = dd = 0.0
    for t in ordered:
        cum += t["pnl"]
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    pnls = [t["pnl"] for t in trades]
    mean = sum(pnls) / n if n else 0.0
    var = (sum((p - mean) ** 2 for p in pnls) / (n - 1)) if n > 1 else 0.0
    std = var ** 0.5
    t_stat = (mean / (std / (n ** 0.5))) if n > 1 and std > 0 else 0.0
    return {
        "label": label, "trades": n,
        "wins": len(wins), "losses": len(losses),
        "win_rate": (len(wins) / n * 100) if n else 0.0,
        "net": sum(pnls),
        "avg": mean,
        "avg_win": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss": (gross_loss / len(losses)) if losses else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss else float("inf"),
        "max_dd": dd,
        "t_stat": t_stat,
    }


def print_table(rows: List[Dict[str, Any]], title: str) -> None:
    print(f"\n{title}")
    print("-" * 96)
    print(f"{'Strategy':<22}{'Trades':>7}{'Win%':>7}{'Net P&L':>13}"
          f"{'AvgWin':>11}{'AvgLoss':>11}{'PF':>7}{'MaxDD':>12}{'t':>7}")
    print("-" * 103)
    for r in rows:
        pf = "inf" if r["profit_factor"] == float("inf") else f"{r['profit_factor']:.2f}"
        print(f"{r['label']:<22}{r['trades']:>7}{r['win_rate']:>6.1f}%"
              f"{r['net']:>13,.0f}{r['avg_win']:>11,.0f}{-r['avg_loss']:>11,.0f}"
              f"{pf:>7}{r['max_dd']:>12,.0f}{r['t_stat']:>7.2f}")
    print("-" * 103)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--target", type=float, default=0.25)
    args = ap.parse_args()

    data = {}
    for key in SYMBOLS:
        df = fetch_5m(key, args.days)
        if df is not None and len(df) > 200:
            data[key] = df

    if not data:
        print("No data fetched.")
        return 1

    today = pd.Timestamp.now(tz=IST).date()
    all_days = sorted({d for df in data.values() for d in df.index.date
                       if d < today})  # exclude the unfinished session
    last_week = set(all_days[-5:])
    print(f"Data: {all_days[0]} .. {all_days[-1]}  ({len(all_days)} completed sessions)")
    print(f"Last-week window: {sorted(str(d) for d in last_week)}")
    print(f"Stop: -{STOP_FRACTION*100:.0f}%  Target: +{args.target*100:.0f}%  "
          f"(completed-bar exits, ATM BS, 5 DTE, 18% vol)")

    # Build per-symbol candle series and run every day once.
    period_trades = {"last_week": [], "full": []}
    signal_counts = defaultdict(lambda: defaultdict(int))

    for key, df in data.items():
        meta = SYMBOLS[key]
        candles = to_candles(df, meta["name"])

        strategies = {
            "orb": OrbStrategy(lot_size=meta["lot"]),
            "pullback": TrendPullbackStrategy(lot_size=meta["lot"]),
            "pullback_fixed": FixedPullback(lot_size=meta["lot"]),
            "momentum": MomentumContinuationStrategy(lot_size=meta["lot"]),
            "mean_reversion": MeanReversionStrategy(),
        }

        by_date = defaultdict(list)
        for i, c in enumerate(candles):
            by_date[c["timestamp"][:10]].append(i)

        for day, indices in by_date.items():
            if pd.Timestamp(day).date() >= today:
                continue
            day_df = df[df.index.date == pd.Timestamp(day).date()]
            if len(day_df) < 10:
                continue
            result = evaluate_day(
                day_df, candles, indices, name=meta["name"],
                lot=meta["lot"], step=meta["step"], vol=meta["vol"],
                target_fraction=args.target, strategies=strategies)
            for item in result:
                if "_signals" in item:
                    for s, n in item["_signals"].items():
                        signal_counts[day][s] += n
                else:
                    period_trades["full"].append(item)
                    if pd.Timestamp(day).date() in last_week:
                        period_trades["last_week"].append(item)

    labels = ["orb", "pullback", "pullback_fixed", "momentum", "mean_reversion"]

    def rows_for(trades, include_stacks=True):
        rows = []
        for s in labels:
            sub = [t for t in trades if t["strategy"] == s]
            rows.append(stats(sub, s))
        if include_stacks:
            shipped = [t for t in trades if t["strategy"] in {"orb", "pullback", "momentum"}]
            rows.append(stats(shipped, "shipped stack (all)"))
            # one trade per day+symbol: earliest shipped entry
            best = {}
            for t in shipped:
                k = (t["day"], t["symbol"])
                if k not in best or t["entry_ts"] < best[k]["entry_ts"]:
                    best[k] = t
            rows.append(stats(list(best.values()), "shipped (1st signal)"))
            # shipped + pullback VWAP fix + mean reversion (one per bucket/day)
            fixed = {}
            for t in [x for x in trades if x["strategy"] in
                      {"orb", "pullback", "pullback_fixed", "momentum"}]:
                bucket = "pb" if t["strategy"].startswith("pullback") else "ship"
                k = (t["day"], t["symbol"], bucket)
                if k not in fixed or t["entry_ts"] < fixed[k]["entry_ts"]:
                    fixed[k] = t
            mr = {}
            for t in trades:
                if t["strategy"] == "mean_reversion":
                    mr[(t["day"], t["symbol"])] = t
            combined = list(fixed.values()) + list(mr.values())
            rows.append(stats(combined, "stack+fix+mean_rev"))
        return rows

    print_table(rows_for(period_trades["last_week"]), "LAST WEEK (5 sessions)")
    print_table(rows_for(period_trades["full"]), f"FULL WINDOW ({len(all_days)} sessions)")

    print("\nPer-day signal counts (last week):")
    for day in sorted(str(d) for d in last_week):
        counts = signal_counts.get(day, {})
        line = "  ".join(f"{k}={counts.get(k,0)}" for k in labels)
        print(f"  {day}: {line}")

    # Trade log for last week
    print("\nLast-week trade log:")
    print(f"{'Day':<12}{'Symbol':<11}{'Strategy':<14}{'Dir':>4}{'Entry':>9}"
          f"{'Exit':>9}{'Reason':>8}{'P&L':>11}")
    for t in sorted(period_trades["last_week"],
                    key=lambda x: (x["day"], x["symbol"], x["entry_ts"])):
        print(f"{t['day']:<12}{t['symbol']:<11}{t['strategy']:<14}{t['kind']:>4}"
              f"{t['entry_premium']:>9.2f}{t['exit_premium']:>9.2f}"
              f"{t['reason']:>8}{t['pnl']:>11,.0f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
