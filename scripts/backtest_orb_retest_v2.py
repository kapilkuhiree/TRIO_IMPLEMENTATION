"""Head-to-head: legacy ORB vs ORB-Retest v2, plus realistic portfolios.

Runs the REAL strategy classes over ~60 sessions of 5m NIFTY / BANKNIFTY data.
Option entries are priced with Black-Scholes (ATM, 5 DTE, 18% vol) and exits
use the live premium rules (stop -20%, target +30%, hard flat 15:10).

This version reports:
  * single-strategy P&L after real option costs (brokerage, STT, exchange,
    GST, stamp duty, SEBI + one tick of slippage per side),
  * a walk-forward split (first 40 / last 19 sessions),
  * a small ORB-Retest parameter grid,
  * portfolio-constrained combinations (max 2 open, max 10 entries/day).

Usage:
    python scripts/backtest_orb_retest_v2.py [--days 60] [--no-cache]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.options_pricing import bs_price
from strategies.orb_strategy import OrbStrategy
from strategies.orb_retest_strategy import OrbRetestStrategy
from strategies.mean_reversion_strategy import MeanReversionStrategy
import src.options_live_scan as scan

IST = timezone(timedelta(hours=5, minutes=30))

SYMBOLS = {
    "^NSEI": {"name": "NIFTY", "lot": 65, "step": 50, "vol": 0.18},
    "^NSEBANK": {"name": "BANKNIFTY", "lot": 30, "step": 100, "vol": 0.18},
}

RISK_BUDGET = 1_000_000.0 * 0.007   # 0.7% of Rs 10L
MAX_PREMIUM = 18_000.0
STOP_FRACTION = 0.20
TARGET_FRACTION = 0.30
DTE_DAYS = 5.0
ENTRY_CUTOFF_MIN = 315   # 14:30 IST = 315 minutes after 09:15
HARD_FLAT = pd.Timestamp("15:10").time()
CACHE_DIR = ROOT / "output"

PRIORITY = {"orb_retest": 0, "mean_reversion": 1, "orb": 2}


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

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


def fetch_5m(symbol: str, days: int, use_cache: bool = True) -> Optional[pd.DataFrame]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"orb_retest_5m_{symbol.replace('^', '')}_{days}d.pkl"
    if use_cache and cache.exists():
        try:
            return pd.read_pickle(cache)
        except Exception:
            pass
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
    try:
        df.to_pickle(cache)
    except Exception:
        pass
    return df


def to_candles(df: pd.DataFrame, name: str) -> List[Dict[str, Any]]:
    return [{
        "timestamp": ts.isoformat(),
        "open": float(r["Open"]), "high": float(r["High"]),
        "low": float(r["Low"]), "close": float(r["Close"]),
        "volume": float(r.get("Volume") or 0.0),
        "underlying_symbol": name, "exchange": "NSE", "instrument_type": "index",
    } for ts, r in df.iterrows()]


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

def collect_orb_signals(candles, name, lot, step) -> Dict[int, str]:
    strategy = OrbStrategy(lot_size=lot, orb_confirm_closes=2)
    specs = {"lot_size": lot, "strike_step": step, "expiry_preference": "weekly"}
    signals: Dict[int, str] = {}
    for i in range(3, len(candles)):
        try:
            sig = strategy.evaluate_signal(candles, i, specs, name)
        except Exception:
            continue
        if getattr(sig, "allowed", False):
            kind = str(getattr(sig, "option_type", "") or "").upper()
            if kind in {"CE", "PE"}:
                signals[i] = kind
    return signals


def collect_retest_signals(candles, name, lot, step, **params) -> Dict[int, str]:
    strategy = OrbRetestStrategy(lot_size=lot, **params)
    specs = {"lot_size": lot, "strike_step": step, "expiry_preference": "weekly"}
    signals: Dict[int, str] = {}
    for i in range(3, len(candles)):
        try:
            sig = strategy.evaluate_signal(candles, i, specs, name)
        except Exception:
            continue
        if getattr(sig, "allowed", False):
            kind = str(getattr(sig, "option_type", "") or "").upper()
            if kind in {"CE", "PE"}:
                signals[i] = kind
    return signals


def collect_mr_signals(candles) -> Dict[int, str]:
    strategy = MeanReversionStrategy()
    signals: Dict[int, str] = {}
    for i in range(len(candles)):
        try:
            sig = strategy.evaluate_signal(candles, i)
        except Exception:
            continue
        if getattr(sig, "allowed", False):
            kind = str(getattr(sig, "option_type", "") or "").upper()
            if kind in {"CE", "PE"}:
                signals[i] = kind
    return signals


# ---------------------------------------------------------------------------
# Option simulation
# ---------------------------------------------------------------------------

def premium(spot: float, strike: float, kind: str, vol: float) -> float:
    return bs_price(spot, strike, max(DTE_DAYS, 1) / 365.0, vol, kind)


def lots_for(entry_prem: float, lot: int) -> int:
    if entry_prem <= 0:
        return 0
    risk_per_lot = entry_prem * STOP_FRACTION * lot
    if risk_per_lot <= 0:
        return 0
    by_risk = int(RISK_BUDGET / risk_per_lot)
    by_premium = int(MAX_PREMIUM / (entry_prem * lot)) if entry_prem * lot > 0 else 0
    return max(0, min(by_risk, by_premium))


def option_costs(entry_prem: float, exit_prem: float, lot: int, lots: int) -> float:
    qty = lot * lots
    buy_value = entry_prem * qty
    sell_value = exit_prem * qty
    turnover = buy_value + sell_value
    brokerage = 20.0 * 2.0
    stt = 0.001 * sell_value              # 0.10% on option sell premium
    exchange = 0.0003503 * turnover       # NSE options transaction charge
    sebi = 0.000001 * turnover
    gst = 0.18 * (brokerage + exchange + sebi)
    stamp = 0.00003 * buy_value
    slippage = 0.05 * qty * 2.0           # one tick each side
    return brokerage + stt + exchange + sebi + gst + stamp + slippage


def minute_of_day(ts: pd.Timestamp) -> int:
    return ts.hour * 60 + ts.minute - (9 * 60 + 15)


def build_candidate(
    candles, index: int, kind: str, meta: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Build one option trade from a signal at ``index`` (entry next bar)."""
    if index + 1 >= len(candles):
        return None
    entry_bar = candles[index + 1]
    day = entry_bar["timestamp"][:10]
    entry_ts = pd.Timestamp(entry_bar["timestamp"])
    spot = float(entry_bar["open"])
    if spot <= 0:
        return None
    lot, step, vol = meta["lot"], meta["step"], meta["vol"]
    strike = round(spot / step) * step
    entry_prem = premium(spot, strike, kind, vol)
    lots = lots_for(entry_prem, lot)
    if entry_prem <= 0 or lots <= 0:
        return None
    stop = entry_prem * (1.0 - STOP_FRACTION)
    target = entry_prem * (1.0 + TARGET_FRACTION)
    exit_prem, reason, exit_idx, exit_ts = entry_prem, "eod", index + 1, entry_bar["timestamp"]
    for j in range(index + 2, len(candles)):
        bar = candles[j]
        if bar["timestamp"][:10] != day:
            break
        if pd.Timestamp(bar["timestamp"]).time() > HARD_FLAT:
            break
        px = premium(float(bar["close"]), strike, kind, vol)
        exit_prem, exit_idx, exit_ts = px, j, bar["timestamp"]
        reason = "eod"
        if px <= stop:
            reason = "stop"
            break
        if px >= target:
            reason = "target"
            break
    qty = lot * lots
    gross = (exit_prem - entry_prem) * qty
    costs = option_costs(entry_prem, exit_prem, lot, lots)
    return {
        "day": day,
        "symbol": meta["name"],
        "kind": kind,
        "entry_ts": entry_bar["timestamp"],
        "exit_ts": exit_ts,
        "entry_idx": index + 1,
        "exit_idx": exit_idx,
        "entry_premium": round(entry_prem, 2),
        "exit_premium": round(exit_prem, 2),
        "strike": strike,
        "lots": lots,
        "qty": qty,
        "reason": reason,
        "gross_pnl": round(gross, 2),
        "costs": round(costs, 2),
        "pnl": round(gross - costs, 2),
    }


def run_variant(
    candles,
    by_date,
    signals: Dict[int, str],
    meta: Dict[str, Any],
    *,
    strategy: str,
    earliest_min: int = 30,
    cooldown: int = 0,
    cooldown_rule: str = "any",
    max_trades_per_day: int = 10,
    entry_cutoff_min: Optional[int] = None,
    as_of: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Simulate one signal stream with one open position per symbol.

    ``entry_cutoff_min`` overrides the module's 14:30 last-entry minute for
    this run only, so a 12:30 cutoff can be tested without changing the
    default every other harness relies on.
    """
    trades: List[Dict[str, Any]] = []
    cutoff = ENTRY_CUTOFF_MIN if entry_cutoff_min is None else entry_cutoff_min

    for day, indices in sorted(by_date.items()):
        if as_of and day >= as_of:
            continue
        open_trade: Optional[Dict[str, Any]] = None
        last_trade: Optional[Dict[str, Any]] = None
        taken_today = 0

        for i in indices:
            if open_trade is not None and open_trade["exit_idx"] <= i:
                last_trade = open_trade
                open_trade = None
            if open_trade is not None:
                continue
            if taken_today >= max_trades_per_day:
                continue
            kind = signals.get(i)
            if kind not in {"CE", "PE"}:
                continue
            if i + 1 >= len(candles) or candles[i + 1]["timestamp"][:10] != day:
                continue
            entry_ts = pd.Timestamp(candles[i + 1]["timestamp"])
            entry_minute = minute_of_day(entry_ts)
            if entry_minute < earliest_min or entry_minute > cutoff:
                continue
            if last_trade is not None and cooldown > 0:
                elapsed = (entry_ts - pd.Timestamp(last_trade["exit_ts"])).total_seconds()
                if elapsed < cooldown:
                    if cooldown_rule == "loss" and last_trade["pnl"] >= 0:
                        pass
                    else:
                        continue
            trade = build_candidate(candles, i, kind, meta)
            if trade is None:
                continue
            trade["strategy"] = strategy
            trades.append(trade)
            open_trade = trade
            last_trade = trade
            taken_today += 1

        open_trade = None

    return trades


def build_candidates(
    candles,
    signals: Dict[int, str],
    meta: Dict[str, Any],
    *,
    strategy: str,
    earliest_min: int,
    entry_cutoff_min: Optional[int] = None,
    as_of: Optional[str] = None,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    cutoff = ENTRY_CUTOFF_MIN if entry_cutoff_min is None else entry_cutoff_min
    for i, kind in signals.items():
        if i + 1 >= len(candles):
            continue
        entry_ts = pd.Timestamp(candles[i + 1]["timestamp"])
        if as_of and str(entry_ts.date()) >= as_of:
            continue
        entry_minute = minute_of_day(entry_ts)
        if entry_minute < earliest_min or entry_minute > cutoff:
            continue
        trade = build_candidate(candles, i, kind, meta)
        if trade is None:
            continue
        trade["strategy"] = strategy
        out.append(trade)
    return out


def select_portfolio(
    candidates: List[Dict[str, Any]],
    *,
    max_open: int = 2,
    max_entries: int = 10,
    cooldowns: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    """Greedy chronological portfolio selection with live-style caps."""
    cooldowns = cooldowns or {}
    ordered = sorted(
        candidates,
        key=lambda t: (pd.Timestamp(t["entry_ts"]), PRIORITY.get(t["strategy"], 9)),
    )
    open_positions: List[Dict[str, Any]] = []
    entries_by_day: Dict[str, int] = defaultdict(int)
    last_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    accepted: List[Dict[str, Any]] = []

    for trade in ordered:
        entry_ts = pd.Timestamp(trade["entry_ts"])
        open_positions = [p for p in open_positions if p["exit_ts"] > entry_ts]
        day = trade["day"]
        if entries_by_day[day] >= max_entries:
            continue
        if len(open_positions) >= max_open:
            continue
        # The strategy streams are designed around one position per
        # underlying.  Without this a strong-trend day could stack two NIFTY
        # trades and double the underlying exposure.
        if any(p["symbol"] == trade["symbol"] for p in open_positions):
            continue
        key = (trade["strategy"], trade["symbol"])
        cooldown = cooldowns.get(trade["strategy"], 0)
        last = last_by_key.get(key)
        if last is not None and cooldown > 0 and last["pnl"] < 0:
            elapsed = (entry_ts - pd.Timestamp(last["exit_ts"])).total_seconds()
            if elapsed < cooldown:
                continue
        accepted.append(trade)
        open_positions.append({
            "symbol": trade["symbol"],
            "exit_ts": pd.Timestamp(trade["exit_ts"]),
        })
        entries_by_day[day] += 1
        last_by_key[key] = trade

    return accepted


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def stats(trades: List[Dict[str, Any]], label: str) -> Dict[str, Any]:
    n = len(trades)
    if not n:
        return {
            "label": label, "trades": 0, "win_rate": 0.0, "net": 0.0,
            "profit_factor": 0.0, "max_dd": 0.0, "avg": 0.0, "t_stat": 0.0,
            "win_days": 0, "sessions": 0, "costs": 0.0,
        }
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
    mean = sum(pnls) / n
    var = (sum((p - mean) ** 2 for p in pnls) / (n - 1)) if n > 1 else 0.0
    std = var ** 0.5
    t_stat = (mean / (std / (n ** 0.5))) if n > 1 and std > 0 else 0.0
    by_day = defaultdict(float)
    for t in trades:
        by_day[t["day"]] += t["pnl"]
    return {
        "label": label,
        "trades": n,
        "win_rate": len(wins) / n * 100.0,
        "net": sum(pnls),
        "profit_factor": (gross_win / gross_loss) if gross_loss else float("inf"),
        "max_dd": dd,
        "avg": mean,
        "t_stat": t_stat,
        "win_days": sum(1 for v in by_day.values() if v > 0),
        "sessions": len(by_day),
        "costs": sum(t["costs"] for t in trades),
    }


def print_table(rows: List[Dict[str, Any]], title: str) -> None:
    print(f"\n{title}")
    print("-" * 118)
    print(f"{'Variant':<34}{'Trades':>7}{'Win%':>7}{'Net Rs':>13}{'Costs':>10}"
          f"{'PF':>7}{'MaxDD':>12}{'Avg':>9}{'t':>7}{'WinDays':>10}")
    print("-" * 118)
    for r in rows:
        pf = "inf" if r["profit_factor"] == float("inf") else f"{r['profit_factor']:.2f}"
        print(f"{r['label']:<34}{r['trades']:>7}{r['win_rate']:>6.1f}%"
              f"{r['net']:>13,.0f}{r['costs']:>10,.0f}{pf:>7}"
              f"{r['max_dd']:>12,.0f}{r['avg']:>9,.0f}{r['t_stat']:>7.2f}"
              f"{r['win_days']:>4}/{r['sessions']:<5}")
    print("-" * 118)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    data: Dict[str, pd.DataFrame] = {}
    for key in SYMBOLS:
        df = fetch_5m(key, args.days, use_cache=not args.no_cache)
        if df is not None and len(df) > 200:
            data[key] = df
    if not data:
        print("No data fetched.")
        return 1

    today = pd.Timestamp.now(tz=IST).date()
    all_days = sorted({d for df in data.values() for d in df.index.date if d < today})
    day_strings = [str(d) for d in all_days]
    split_at = 40 if len(all_days) > 40 else len(all_days) // 2
    early_days = set(day_strings[:split_at])
    late_days = set(day_strings[split_at:])
    print(f"Data: {all_days[0]} .. {all_days[-1]}  ({len(all_days)} completed sessions)")
    print(f"Walk-forward: first {split_at} / last {len(all_days) - split_at} sessions")
    print(f"Pricing: ATM BS 5 DTE 18% IV; stop -{STOP_FRACTION*100:.0f}%, "
          f"target +{TARGET_FRACTION*100:.0f}%, hard flat 15:10")
    print(f"Sizing: risk Rs {RISK_BUDGET:,.0f}/trade, max premium "
          f"Rs {MAX_PREMIUM:,.0f}; costs include brokerage/STT/exchange/GST/"
          f"stamp/SEBI + 1 tick slippage per side")

    candles_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    by_date_by_symbol: Dict[str, Dict[str, List[int]]] = {}
    meta_by_symbol = {m["name"]: m for m in SYMBOLS.values()}
    orb_signals: Dict[str, Dict[int, str]] = {}
    mr_signals: Dict[str, Dict[int, str]] = {}
    retest_grid: Dict[str, Dict[str, Dict[int, str]]] = {}

    retest_variants = {
        "v2 default": dict(or_min_width_atr=3.0, max_breakout_body=0.90,
                           resumption_buffer_atr=0.20, atr_pct_max=0.30),
        "v2 no thrust": dict(or_min_width_atr=3.0, max_breakout_body=1.00,
                             resumption_buffer_atr=0.20, atr_pct_max=0.30),
        "v2 buffer 0": dict(or_min_width_atr=3.0, max_breakout_body=0.90,
                            resumption_buffer_atr=0.00, atr_pct_max=0.30),
        "v2 wide 3.5": dict(or_min_width_atr=3.5, max_breakout_body=0.90,
                            resumption_buffer_atr=0.20, atr_pct_max=0.30),
        "v2 atr<=0.45": dict(or_min_width_atr=3.0, max_breakout_body=0.90,
                             resumption_buffer_atr=0.20, atr_pct_max=0.45),
        "v2 window 9": dict(or_min_width_atr=3.0, max_breakout_body=0.90,
                            resumption_buffer_atr=0.20, atr_pct_max=0.30,
                            retest_window_bars=9),
        "v2 body>=0.30": dict(or_min_width_atr=3.0, max_breakout_body=0.90,
                              resumption_buffer_atr=0.20, atr_pct_max=0.30,
                              resumption_min_body=0.30),
    }

    for key, df in data.items():
        meta = meta_by_symbol[SYMBOLS[key]["name"]]
        name = meta["name"]
        candles = to_candles(df, name)
        candles_by_symbol[name] = candles
        by_date: Dict[str, List[int]] = defaultdict(list)
        for i, c in enumerate(candles):
            by_date[c["timestamp"][:10]].append(i)
        by_date_by_symbol[name] = by_date
        orb_signals[name] = collect_orb_signals(candles, name, meta["lot"], meta["step"])
        mr_signals[name] = collect_mr_signals(candles)
        retest_grid[name] = {}
        for label, params in retest_variants.items():
            retest_grid[name][label] = collect_retest_signals(
                candles, name, meta["lot"], meta["step"], **params)
        print(f"{name}: ORB={len(orb_signals[name])} MR={len(mr_signals[name])} " +
              " ".join(f"{k}={len(v)}" for k, v in retest_grid[name].items()))

    def run_all(signal_map, **kwargs):
        out = []
        for name, candles in candles_by_symbol.items():
            out += run_variant(candles, by_date_by_symbol[name], signal_map[name],
                               meta_by_symbol[name], as_of=str(today), **kwargs)
        return out

    # ------------------------------------------------------------------
    # Single-strategy comparison
    # ------------------------------------------------------------------
    retest_by_variant = {
        label: {name: retest_grid[name][label] for name in candles_by_symbol}
        for label in retest_variants
    }
    variants = {
        "ORB live (600s any, 09:30)": run_all(
            orb_signals, strategy="orb", earliest_min=15, cooldown=600,
            cooldown_rule="any"),
        "ORB + C (300s loss, 10:00)": run_all(
            orb_signals, strategy="orb", earliest_min=45, cooldown=300,
            cooldown_rule="loss"),
        "Retest v2 + C (300s loss, 10:00)": run_all(
            retest_by_variant["v2 default"], strategy="orb_retest",
            earliest_min=45, cooldown=300, cooldown_rule="loss"),
        "Retest v2 no cd (09:45)": run_all(
            retest_by_variant["v2 default"], strategy="orb_retest",
            earliest_min=30, cooldown=0),
    }
    mr_trades = run_all(mr_signals, strategy="mean_reversion", earliest_min=15)
    rows = [stats(t, k) for k, t in variants.items()]
    print_table(rows, f"FULL WINDOW ({len(all_days)} sessions, after costs)")

    for title, days in (("TRAIN: first 40 sessions", early_days),
                        (f"TEST: last {len(late_days)} sessions", late_days)):
        sub = {k: [t for t in v if t["day"] in days] for k, v in variants.items()}
        print_table([stats(t, k) for k, t in sub.items()], title)

    # ------------------------------------------------------------------
    # Retest parameter grid (full + test split)
    # ------------------------------------------------------------------
    grid_rows = []
    for label in retest_variants:
        trades = run_all(retest_by_variant[label], strategy="orb_retest",
                         earliest_min=30, cooldown=0)
        r = stats(trades, f"no cd / {label}")
        r["label"] = f"no cd / {label}"
        grid_rows.append(r)
        r2 = stats([t for t in trades if t["day"] in late_days],
                   f"TEST / {label}")
        r2["label"] = f"TEST / {label}"
        grid_rows.append(r2)
    print_table(grid_rows, "ORB-RETEST PARAMETER GRID (single strategy)")

    # ------------------------------------------------------------------
    # Portfolio-constrained combinations
    # ------------------------------------------------------------------
    def candidate_stream(name, signal_map, strategy, earliest):
        return build_candidates(
            candles_by_symbol[name], signal_map[name], meta_by_symbol[name],
            strategy=strategy, earliest_min=earliest, as_of=str(today))

    def collect_candidates(specs):
        out = []
        for name in candles_by_symbol:
            for strategy, signal_map, earliest in specs:
                out += candidate_stream(name, signal_map, strategy, earliest)
        return out

    orb_default = orb_signals
    retest_default = retest_by_variant["v2 default"]
    retest_buffer0 = retest_by_variant["v2 buffer 0"]

    portfolios = {
        "ORB live + MR": collect_candidates([
            ("orb", orb_default, 15),
            ("mean_reversion", mr_signals, 15),
        ]),
        "ORB + C + MR": collect_candidates([
            ("orb", orb_default, 45),
            ("mean_reversion", mr_signals, 15),
        ]),
        "ORB + C + Retest + MR": collect_candidates([
            ("orb", orb_default, 45),
            ("orb_retest", retest_default, 30),
            ("mean_reversion", mr_signals, 15),
        ]),
        "ORB + C + Retest(buf0) + MR": collect_candidates([
            ("orb", orb_default, 45),
            ("orb_retest", retest_buffer0, 30),
            ("mean_reversion", mr_signals, 15),
        ]),
        "Retest + MR": collect_candidates([
            ("orb_retest", retest_default, 30),
            ("mean_reversion", mr_signals, 15),
        ]),
    }
    cooldowns = {"orb": 300, "orb_retest": 0, "mean_reversion": 300}
    portfolio_rows = {}
    rows = []
    for label, candidates in portfolios.items():
        selected = select_portfolio(candidates, max_open=2, max_entries=10,
                                    cooldowns=cooldowns)
        portfolio_rows[label] = stats(selected, label)
        rows.append(portfolio_rows[label])
    print_table(rows, "PORTFOLIO-CONSTRAINED COMBINATIONS (max 2 open, 10 entries/day)")

    # Conservative estimate: treat the three sleeves as independent streams
    # (no path-dependent slot sharing).  This is the number to use when the
    # portfolio sim looks optimistic.
    independent = {
        "ORB live + MR": variants["ORB live (600s any, 09:30)"] + mr_trades,
        "ORB + C + MR": variants["ORB + C (300s loss, 10:00)"] + mr_trades,
        "ORB + C + Retest + MR": (
            variants["ORB + C (300s loss, 10:00)"] + mr_trades +
            variants["Retest v2 no cd (09:45)"]),
    }
    print("\nINDEPENDENT-STREAM SUM (conservative; ignores slot sharing):")
    print("-" * 118)
    independent_rows = {}
    for label, trades in independent.items():
        independent_rows[label] = stats(trades, label)
        s = independent_rows[label]
        pf = "inf" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
        print(f"{label:<34}{s['trades']:>7}{s['win_rate']:>6.1f}%"
              f"{s['net']:>13,.0f}{pf:>7}{s['max_dd']:>12,.0f}")

    print("\nPer-symbol breakdown (full window):")
    print("-" * 112)
    for label in ("ORB live (600s any, 09:30)", "ORB + C (300s loss, 10:00)",
                  "Retest v2 no cd (09:45)"):
        for name in ("NIFTY", "BANKNIFTY"):
            sub = [t for t in variants[label] if t["symbol"] == name]
            s = stats(sub, f"{label} / {name}")
            pf = "inf" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
            print(f"{s['label']:<54}{s['trades']:>6}{s['win_rate']:>7.1f}%"
                  f"{s['net']:>12,.0f}{pf:>7}{s['max_dd']:>12,.0f}")

    artifact = {
        "window": {"start": str(all_days[0]), "end": str(all_days[-1]),
                   "sessions": len(all_days)},
        "assumptions": {
            "pricing": "ATM Black-Scholes, 5 DTE, 18% IV",
            "stop_fraction": STOP_FRACTION,
            "target_fraction": TARGET_FRACTION,
            "risk_per_trade": RISK_BUDGET,
            "max_premium": MAX_PREMIUM,
            "costs": "brokerage+STT+exchange+GST+stamp+SEBI+1 tick slippage/side",
            "portfolio": "max 2 open, max 10 entries/day, priority retest>MR>ORB",
        },
        "variants": {k: stats(v, k) for k, v in variants.items()},
        "mean_reversion": stats(mr_trades, "mean_reversion"),
        "grid": {r["label"]: r for r in grid_rows},
        "portfolios": portfolio_rows,
        "independent_streams": independent_rows,
    }
    out = ROOT / "orb_retest_v2_backtest.json"
    out.write_text(json.dumps(artifact, indent=2, default=str), encoding="utf-8")
    print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
