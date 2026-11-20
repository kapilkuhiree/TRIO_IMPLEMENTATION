"""60-session option-model backtest for the research strategies.

Strategies covered (all currently untracked / not wired into the live
registry):

* ``volatility_breakout``    - ATR consolidation breakout + volume
* ``9_ema_fib_pivot``        - EMA 9 + Fibonacci pivot breakout
* ``five_day_green_reversal``- 5+ green daily closes -> close below PDC

The first two run on 5-minute candles; the third runs on daily candles.
Pricing and costs deliberately match ``scripts/backtest_orb_retest_v2.py`` so
the numbers are comparable with the ORB/MR evidence:

* ATM Black-Scholes, 5 DTE, flat 18% IV,
* long CE/PE only (no short options),
* premium stop -20%, target +30%, same-day hard flat 15:10,
* brokerage + STT + exchange + GST + stamp + SEBI + 1 tick slippage/side,
* risk Rs 7,000/trade, max premium Rs 18,000,
* live-style portfolio cap: max 2 open, max 10 entries/day, one per symbol.

The daily strategy is held for at most 5 sessions and priced at each daily
close; it is not forced into the 15:10 intraday flat.

    python scripts/backtest_research_strategies.py
    python scripts/backtest_research_strategies.py --days 60 --symbols NIFTY RELIANCE
    python scripts/backtest_research_strategies.py --json output/research_strategies_60d.json

This is a fidelity/screening run, not a promotion gate. A zero-trade result
means the strategy produced no confirmed signal on this universe/window, not
that it is invalid.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections import defaultdict
from datetime import timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The two src/strategies modules were copied from a sibling project and import
# ``utils`` / ``risk_manager`` / ``signal_engine`` as top-level names.  Alias
# the repository modules so the classes can be imported without editing them.
import src.utils as _src_utils  # noqa: E402
import src.risk_manager as _src_risk_manager  # noqa: E402
import src.signal_engine as _src_signal_engine  # noqa: E402

sys.modules.setdefault("utils", _src_utils)
sys.modules.setdefault("risk_manager", _src_risk_manager)
sys.modules.setdefault("signal_engine", _src_signal_engine)

from scripts.backtest_orb_retest_v2 import (  # noqa: E402
    ENTRY_CUTOFF_MIN,
    IST,
    STOP_FRACTION,
    TARGET_FRACTION,
    build_candidates,
    build_candidate,
    lots_for,
    option_costs,
    premium,
    print_table,
    select_portfolio,
    stats,
)
from strategies.volatility_breakout_strategy import (  # noqa: E402
    VolatilityBreakoutStrategy,
)
from src.strategies.five_day_green_reversal_strategy import (  # noqa: E402
    FiveDayGreenReversalStrategy,
)
from src.strategies.nine_ema_fib_pivot_strategy import (  # noqa: E402
    NineEMaFibPivotStrategy,
)

CACHE_DIR = ROOT / "output"
DAILY_HOLD_SESSIONS = 5
DAILY_EARLIEST = 15  # 09:30 IST for the intraday sleeves

# ``symbol`` is the yfinance ticker; ``name`` is the strategy-facing identity.
UNIVERSE: Dict[str, Dict[str, Any]] = {
    "^NSEI": {"name": "NIFTY", "lot": 65, "step": 50, "kind": "index"},
    "^NSEBANK": {"name": "BANKNIFTY", "lot": 30, "step": 100, "kind": "index"},
    "RELIANCE.NS": {"name": "RELIANCE", "lot": 500, "step": 5, "kind": "stock"},
    "HDFCBANK.NS": {"name": "HDFCBANK", "lot": 550, "step": 5, "kind": "stock"},
    "ICICIBANK.NS": {"name": "ICICIBANK", "lot": 700, "step": 5, "kind": "stock"},
    "INFY.NS": {"name": "INFY", "lot": 400, "step": 5, "kind": "stock"},
    "SBIN.NS": {"name": "SBIN", "lot": 750, "step": 5, "kind": "stock"},
    "TCS.NS": {"name": "TCS", "lot": 175, "step": 10, "kind": "stock"},
}

STRATEGIES = {
    "volatility_breakout": VolatilityBreakoutStrategy,
    "9_ema_fib_pivot": NineEMaFibPivotStrategy,
    "five_day_green_reversal": FiveDayGreenReversalStrategy,
}
INTRADAY_STRATEGIES = (
    "volatility_breakout",
    "9_ema_fib_pivot",
)
DAILY_STRATEGIES = ("five_day_green_reversal",)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _fetch(symbol: str, *, interval: str, period: str, cache_name: str) -> Optional[pd.DataFrame]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / cache_name
    if cache.exists():
        try:
            return pd.read_pickle(cache)
        except Exception:
            pass
    df = yf.download(symbol, period=period, interval=interval,
                     auto_adjust=False, progress=False)
    if df is None or df.empty:
        return None
    if hasattr(df.columns, "levels"):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["Close"])
    if interval.endswith("m"):
        df.index = pd.to_datetime(df.index, utc=True).tz_convert(IST)
        df = df[(df.index.time >= pd.Timestamp("09:15").time()) &
                (df.index.time <= pd.Timestamp("15:30").time())]
    try:
        df.to_pickle(cache)
    except Exception:
        pass
    return df


def fetch_5m(symbol: str, days: int) -> Optional[pd.DataFrame]:
    return _fetch(symbol, interval="5m", period=f"{days}d",
                  cache_name=f"research_5m_{symbol.replace('^', '').replace('.', '_')}_{days}d.pkl")


def fetch_daily(symbol: str) -> Optional[pd.DataFrame]:
    return _fetch(symbol, interval="1d", period="1y",
                  cache_name=f"research_daily_{symbol.replace('^', '').replace('.', '_')}_1y.pkl")


def to_5m_candles(df: pd.DataFrame, meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    name = meta["name"]
    return [{
        "timestamp": ts.isoformat(),
        "open": float(r["Open"]), "high": float(r["High"]),
        "low": float(r["Low"]), "close": float(r["Close"]),
        "volume": float(r.get("Volume") or 0.0),
        "underlying_symbol": name, "exchange": "NSE",
        "instrument_type": meta["kind"],
    } for ts, r in df.iterrows()]


def to_daily_candles(df: pd.DataFrame, meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    name = meta["name"]
    rows = []
    for ts, r in df.iterrows():
        stamp = pd.Timestamp(ts)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize(IST)
        # The daily strategies carry an intraday "no entries after 15:00"
        # cutoff, so a 15:30 close stamp silently suppressed every signal.
        # 15:00 is the decision stamp; the trade is still entered at the next
        # session's open, so nothing is filled using the trigger-day close.
        stamp = stamp.replace(hour=15, minute=0, second=0, microsecond=0)
        rows.append({
            "timestamp": stamp.isoformat(),
            "open": float(r["Open"]), "high": float(r["High"]),
            "low": float(r["Low"]), "close": float(r["Close"]),
            "volume": float(r.get("Volume") or 0.0),
            "underlying_symbol": name, "exchange": "NSE",
            "instrument_type": meta["kind"],
        })
    return rows


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

def collect_intraday_signals(
    candles: List[Dict[str, Any]], name: str, strategy_name: str,
    lot: int, step: int,
) -> Tuple[Dict[int, str], int]:
    strategy = STRATEGIES[strategy_name](lot_size=lot)
    specs = {"lot_size": lot, "strike_step": step, "expiry_preference": "weekly"}
    signals: Dict[int, str] = {}
    raw = 0
    for index in range(1, len(candles)):
        try:
            signal = strategy.evaluate_signal(candles, index, specs, name)
        except Exception:
            continue
        if not bool(getattr(signal, "allowed", False)):
            continue
        kind = str(getattr(signal, "option_type", "") or "").upper()
        if kind in {"CE", "PE"}:
            signals[index] = kind
            raw += 1
    return signals, raw


def build_daily_candidate(
    candles: List[Dict[str, Any]], index: int, kind: str, meta: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Entry next daily open, premium stop/target, max 5 sessions."""
    if index + 1 >= len(candles):
        return None
    entry_bar = candles[index + 1]
    spot = float(entry_bar["open"])
    if spot <= 0:
        return None
    # The daily bars are stamped at the 15:00 decision time, but the fill is
    # the next session's open.  Stamp the entry/exit so the portfolio selector
    # sees the real holding window instead of a same-day 15:00 overlap.
    entry_day = entry_bar["timestamp"][:10]
    entry_ts = f"{entry_day}T09:15:00+05:30"
    lot, step = meta["lot"], meta["step"]
    strike = round(spot / step) * step
    entry_prem = premium(spot, strike, kind, 0.18)
    lots = lots_for(entry_prem, lot)
    if entry_prem <= 0 or lots <= 0:
        return None
    stop = entry_prem * (1.0 - STOP_FRACTION)
    target = entry_prem * (1.0 + TARGET_FRACTION)
    last = min(index + 1 + DAILY_HOLD_SESSIONS - 1, len(candles) - 1)
    exit_prem = entry_prem
    reason = "time_exit"
    exit_idx = last
    exit_bar = candles[last]
    for j in range(index + 1, last + 1):
        px = premium(float(candles[j]["close"]), strike, kind, 0.18)
        exit_prem, exit_idx, exit_bar = px, j, candles[j]
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
        "day": entry_day,
        "symbol": meta["name"],
        "kind": kind,
        "entry_ts": entry_ts,
        "exit_ts": f"{exit_bar['timestamp'][:10]}T15:00:00+05:30",
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
        "strategy": "five_day_green_reversal",
    }


def collect_daily_candidates(
    candles: List[Dict[str, Any]], meta: Dict[str, Any],
    strategy: FiveDayGreenReversalStrategy,
    window: Tuple[str, str],
) -> Tuple[List[Dict[str, Any]], int, int]:
    """Returns (in-window candidates, in-window signals, all-cache signals)."""
    specs = {"lot_size": meta["lot"], "strike_step": meta["step"],
             "expiry_preference": "monthly"}
    start, end = window
    out: List[Dict[str, Any]] = []
    raw = 0
    raw_total = 0
    for index in range(1, len(candles) - 1):
        try:
            signal = strategy.evaluate_signal(candles, index, specs, meta["name"])
        except Exception:
            continue
        if not bool(getattr(signal, "allowed", False)):
            continue
        kind = str(getattr(signal, "option_type", "") or "").upper()
        if kind not in {"CE", "PE"}:
            continue
        raw_total += 1
        entry_day = candles[index + 1]["timestamp"][:10]
        if not (start <= entry_day <= end):
            continue
        raw += 1
        trade = build_daily_candidate(candles, index, kind, meta)
        if trade is not None:
            out.append(trade)
    return out, raw, raw_total


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def per_symbol_rows(trades: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        grouped[trade["symbol"]].append(trade)
    return [stats(group, symbol) for symbol, group in sorted(grouped.items())]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--symbols", nargs="+", default=None,
                        help="Yahoo tickers or short names (default: 8-symbol basket)")
    parser.add_argument("--max-open", type=int, default=2)
    parser.add_argument("--max-entries", type=int, default=10)
    parser.add_argument("--json", type=Path,
                        default=ROOT / "research_strategies_60d.json")
    args = parser.parse_args(argv)

    if args.symbols:
        wanted = {s.upper() for s in args.symbols}
        universe = {
            ticker: meta for ticker, meta in UNIVERSE.items()
            if ticker.upper() in wanted or meta["name"] in wanted
        }
    else:
        universe = dict(UNIVERSE)
    if not universe:
        print("No matching symbols.")
        return 1

    today = pd.Timestamp.now(tz=IST).date().isoformat()
    intraday: Dict[str, Tuple[List[Dict[str, Any]], Dict[str, Any]]] = {}
    daily: Dict[str, Tuple[List[Dict[str, Any]], Dict[str, Any]]] = {}
    for ticker, meta in universe.items():
        meta = {**meta, "vol": 0.18}
        frame = fetch_5m(ticker, args.days)
        if frame is None or len(frame) < 200:
            print(f"skip {ticker}: no 5m data")
            continue
        intraday[meta["name"]] = (to_5m_candles(frame, meta), meta)
        daily_frame = fetch_daily(ticker)
        if daily_frame is not None and len(daily_frame) > 30:
            daily[meta["name"]] = (to_daily_candles(daily_frame, meta), meta)
    if not intraday:
        print("No intraday data fetched.")
        return 1

    sessions = sorted({row["timestamp"][:10]
                       for candles, _ in intraday.values() for row in candles})
    window = (sessions[0], sessions[-1])
    print(f"Data: {window[0]} .. {window[1]}  ({len(sessions)} sessions)")
    print(f"Universe: {', '.join(sorted(intraday))}")
    print(f"Pricing: ATM BS 5 DTE 18% IV; premium stop -{STOP_FRACTION*100:.0f}%, "
          f"target +{TARGET_FRACTION*100:.0f}%, intraday flat 15:10")
    print(f"Portfolio: max {args.max_open} open, {args.max_entries} entries/day, "
          "one position per symbol")
    print("Daily sleeve: entry next open, max 5 sessions, priced at daily close")

    results: Dict[str, Any] = {}
    all_candidates: List[Dict[str, Any]] = []

    for strategy_name in INTRADAY_STRATEGIES:
        candidates: List[Dict[str, Any]] = []
        raw = 0
        per_symbol_raw: Dict[str, int] = {}
        for name, (candles, meta) in intraday.items():
            signals, count = collect_intraday_signals(
                candles, name, strategy_name, meta["lot"], meta["step"])
            raw += count
            per_symbol_raw[name] = count
            candidates.extend(build_candidates(
                candles, signals, meta, strategy=strategy_name,
                earliest_min=DAILY_EARLIEST, as_of=today))
        taken = select_portfolio(candidates, max_open=args.max_open,
                                 max_entries=args.max_entries)
        results[strategy_name] = {
            "raw_signals": raw,
            "per_symbol_raw": per_symbol_raw,
            "candidates": len(candidates),
            "taken": len(taken),
            "stats": stats(taken, strategy_name),
            "by_symbol": per_symbol_rows(taken),
            "trades": taken,
        }
        all_candidates.extend(candidates)

    for strategy_name in DAILY_STRATEGIES:
        candidates = []
        raw = 0
        raw_total = 0
        per_symbol_raw = {}
        strategy = FiveDayGreenReversalStrategy(lot_size=65)
        for name, (candles, meta) in daily.items():
            found, count, total = collect_daily_candidates(
                candles, meta, strategy, window)
            raw += count
            raw_total += total
            per_symbol_raw[name] = count
            candidates.extend(found)
        taken = select_portfolio(candidates, max_open=args.max_open,
                                 max_entries=args.max_entries)
        results[strategy_name] = {
            "raw_signals": raw,
            "raw_signals_1y": raw_total,
            "per_symbol_raw": per_symbol_raw,
            "candidates": len(candidates),
            "taken": len(taken),
            "stats": stats(taken, strategy_name),
            "by_symbol": per_symbol_rows(taken),
            "trades": taken,
        }
        all_candidates.extend(candidates)

    combined = select_portfolio(all_candidates, max_open=args.max_open,
                                max_entries=args.max_entries)

    print("\nSTRATEGY SCREEN (all signals, live-style portfolio cap)")
    print_table([results[name]["stats"] for name in results],
                "RESULTS BY STRATEGY")
    print(f"\n{'Strategy':<26}{'Raw':>7}{'Candidates':>12}{'Taken':>8}")
    print("-" * 55)
    for name, result in results.items():
        print(f"{name:<26}{result['raw_signals']:>7}{result['candidates']:>12}"
              f"{result['taken']:>8}")
        if "raw_signals_1y" in result:
            print(f"{'':<26}{'(signals in full 1y daily cache: '}"
                  f"{result['raw_signals_1y']})")
    print_table([stats(combined, "all research strategies combined")], "COMBINED STREAM")

    for name, result in results.items():
        print_table(result["by_symbol"], f"{name} BY SYMBOL")
        if not result["taken"]:
            print("  (no taken trades: zero confirmed signals or every signal "
                  "was filtered by the portfolio cap)")

    print("\nASSUMPTIONS / CAVEATS")
    print("-" * 118)
    print("- Model prices, not archived bid/ask. Real IV and spreads will differ.")
    print("- The 5m strategies are screened on stock/index proxies; a stock's "
          "option lot/strike is approximated.")
    print("- Volume confirmation (volatility breakout) cannot fire on the index "
          "feed because Yahoo reports zero volume for ^NSEI/^NSEBANK.")
    print("- The daily sleeve holds at most 5 sessions and does not model theta "
          "beyond the fixed 5-DTE mark.")
    print("- The daily sleeve fills at the next session's open (09:15), not at "
          "the trigger-day close.")
    print("- No tuning was performed on this window. Zero-trade rows are "
          "screening outcomes, not strategy rejections.")

    payload = {
        "window": {"start": window[0], "end": window[1],
                   "sessions": len(sessions), "days": args.days},
        "universe": sorted(intraday),
        "assumptions": {
            "pricing": "ATM Black-Scholes, 5 DTE, 18% IV",
            "stop_fraction": STOP_FRACTION,
            "target_fraction": TARGET_FRACTION,
            "entry_cutoff_minute": ENTRY_CUTOFF_MIN,
            "intraday_earliest_minute": DAILY_EARLIEST,
            "daily_hold_sessions": DAILY_HOLD_SESSIONS,
            "max_open": args.max_open,
            "max_entries_per_day": args.max_entries,
            "costs": "brokerage+STT+exchange+GST+stamp+SEBI + 1 tick slippage/side",
        },
        "strategies": {
            name: {key: value for key, value in result.items() if key != "trades"}
            for name, result in results.items()
        },
        "combined": stats(combined, "all research strategies combined"),
        "trades": {
            name: result["trades"] for name, result in results.items()
        },
        "combined_trades": combined,
    }
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(payload, indent=2, default=str),
                         encoding="utf-8")
    print(f"\nSaved: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
