"""Wide NSE F&O backtest for the 5-Day Green Reversal strategy.

The strategy is a *daily* pattern: five or more consecutive green daily
candles, then a close below the previous day's close -> buy a PE at the next
session's open.  The original screen only used an 8-symbol basket, which is
far too narrow for a pattern this rare.  This script widens the universe to
every underlying that actually has NFO options today (221 underlyings: 218
stocks plus NIFTY, BANKNIFTY and FINNIFTY), taken straight from the cached
Kite instrument dump, and reuses the exact option model from
``scripts/backtest_orb_retest_v2.py``:

* ATM Black-Scholes, 5 DTE, flat 18% IV,
* real NFO lot size and strike step per underlying,
* premium stop -20%, target +30%, max 5 sessions, marked at daily close,
* brokerage + STT + exchange + GST + stamp + SEBI + 1 tick slippage/side,
* Rs 7,000 risk/trade, max premium Rs 18,000, max 2 open / 10 per day.

Two books are reported:

* ``uncapped`` - every signal, no portfolio cap. This measures signal quality
  without the selection artefact a 200-symbol universe would otherwise create.
* ``portfolio`` - the live-style greedy cap (2 open, 10 entries/day).

Because a daily bar cannot tell us where inside the day the premium crossed a
level, three exit fills are stored for every trade:

* ``bands`` (headline) - the +30% target and -20% stop fill at their levels,
* ``stop at close``   - targets fill at +30%, stops slip to the daily close,
* ``mark-at-close``   - everything fills at the close, which books the whole
  gap on a winning option and is therefore an optimistic upper bound.

Daily bars are stamped 15:00 for the strategy's decision cutoff; the fill is
the next session's open (09:15), so no trigger-day close is used as an entry.

    python scripts/backtest_five_day_green_wide.py
    python scripts/backtest_five_day_green_wide.py --sessions 60
    python scripts/backtest_five_day_green_wide.py --limit 30   # quick smoke
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import src.utils as _src_utils  # noqa: E402
import src.risk_manager as _src_risk_manager  # noqa: E402
import src.signal_engine as _src_signal_engine  # noqa: E402

sys.modules.setdefault("utils", _src_utils)
sys.modules.setdefault("risk_manager", _src_risk_manager)
sys.modules.setdefault("signal_engine", _src_signal_engine)

from scripts.backtest_orb_retest_v2 import (  # noqa: E402
    IST,
    STOP_FRACTION,
    TARGET_FRACTION,
    lots_for,
    option_costs,
    premium,
    print_table,
    select_portfolio,
    stats,
)
from src.strategies.five_day_green_reversal_strategy import (  # noqa: E402
    FiveDayGreenReversalStrategy,
)

CACHE_DIR = ROOT / "output" / "research_daily_wide"
HOLD_SESSIONS = 5

# Underlyings whose NSE symbol is not simply ``<name>.NS`` on Yahoo.
YAHOO_OVERRIDES = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "ZOMATO": "ETERNAL.NS",       # renamed ETERNAL on the exchange
    "M&M": "M&M.NS",
    "BAJAJ-AUTO": "BAJAJ-AUTO.NS",
    "NAM-INDIA": "NAM-INDIA.NS",
}
SKIP_UNDERLYINGS = {"MIDCPNIFTY", "NIFTYNXT50", "NIFTYFPI"}  # no usable Yahoo series


# ---------------------------------------------------------------------------
# NFO universe (lot size + strike step) from the cached Kite dump
# ---------------------------------------------------------------------------

def nfo_universe() -> Dict[str, Dict[str, Any]]:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "TRIO" / "instruments"
    files = sorted(base.glob("NFO-*.pkl"))
    if not files:
        raise SystemExit(
            "No cached NFO instrument dump found. Run "
            "`python scripts/zerodha_readonly_smoke.py` first."
        )
    rows = pickle.load(files[-1].open("rb"))["rows"]
    options = [r for r in rows if r.get("instrument_type") in ("CE", "PE")]
    by_name: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in options:
        by_name[row["name"]].append(row)

    universe: Dict[str, Dict[str, Any]] = {}
    for name, instruments in by_name.items():
        if name in SKIP_UNDERLYINGS:
            continue
        lots = sorted({int(r["lot_size"]) for r in instruments if r.get("lot_size")})
        if not lots:
            continue
        expiries = sorted({r["expiry"] for r in instruments})
        nearest = [r for r in instruments if r["expiry"] == expiries[0]]
        strikes = sorted({float(r["strike"]) for r in nearest})
        diffs = [round(b - a, 4) for a, b in zip(strikes, strikes[1:])]
        if not diffs:
            continue
        step = max(set(diffs), key=diffs.count)
        universe[name] = {
            "lot": lots[0],
            "step": step,
            "kind": "index" if name in {"NIFTY", "BANKNIFTY", "FINNIFTY"} else "stock",
            "ticker": YAHOO_OVERRIDES.get(name, f"{name}.NS"),
        }
    return universe


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _chunks(items: List[str], size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def fetch_daily(universe: Dict[str, Dict[str, Any]], refresh: bool) -> Dict[str, pd.DataFrame]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / "daily_1y.pkl"
    frames: Dict[str, pd.DataFrame] = {}
    if cache.exists() and not refresh:
        try:
            frames = pickle.load(cache.open("rb"))
        except Exception:
            frames = {}

    wanted = [(name, meta["ticker"]) for name, meta in universe.items()
              if meta["ticker"] not in frames]
    if not wanted:
        return frames

    for chunk in _chunks(wanted, 30):
        tickers = [ticker for _, ticker in chunk]
        try:
            data = yf.download(tickers, period="1y", interval="1d",
                               auto_adjust=False, progress=False,
                               group_by="ticker", threads=True)
        except Exception as exc:
            print(f"  download failed for chunk: {exc}")
            continue
        for name, ticker in chunk:
            try:
                frame = data[ticker] if isinstance(data.columns, pd.MultiIndex) else data
                frame = frame.dropna(subset=["Close"])
                if len(frame) > 30:
                    frames[ticker] = frame
            except Exception:
                continue
        pickle.dump(frames, cache.open("wb"))
        print(f"  fetched {len(frames)}/{len(universe)} series")
    return frames


def to_daily_candles(frame: pd.DataFrame, name: str, kind: str) -> List[Dict[str, Any]]:
    rows = []
    for ts, row in frame.iterrows():
        stamp = pd.Timestamp(ts)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize(IST)
        # 15:00 is the decision stamp; the entry is the next session's open.
        stamp = stamp.replace(hour=15, minute=0, second=0, microsecond=0)
        rows.append({
            "timestamp": stamp.isoformat(),
            "open": float(row["Open"]), "high": float(row["High"]),
            "low": float(row["Low"]), "close": float(row["Close"]),
            "volume": float(row.get("Volume") or 0.0),
            "underlying_symbol": name, "exchange": "NSE",
            "instrument_type": kind,
        })
    return rows


# ---------------------------------------------------------------------------
# Signals / trades
# ---------------------------------------------------------------------------

def build_trade(candles: List[Dict[str, Any]], index: int, kind: str,
                name: str, meta: Dict[str, Any], *,
                fill: str = "bands") -> Optional[Dict[str, Any]]:
    if index + 1 >= len(candles):
        return None
    layer = candles[index + 1]
    spot = float(layer["open"])
    if spot <= 0:
        return None
    step, lot = meta["step"], meta["lot"]
    strike = round(spot / step) * step
    entry_prem = premium(spot, strike, kind, 0.18)
    lots = lots_for(entry_prem, lot)
    if entry_prem <= 0 or lots <= 0:
        return None
    stop = entry_prem * (1.0 - STOP_FRACTION)
    target = entry_prem * (1.0 + TARGET_FRACTION)
    last = min(index + HOLD_SESSIONS, len(candles) - 1)
    exit_prem, exit_bar, exit_idx, reason = entry_prem, layer, index + 1, "time_exit"
    for j in range(index + 1, last + 1):
        px = premium(float(candles[j]["close"]), strike, kind, 0.18)
        exit_prem, exit_bar, exit_idx = px, candles[j], j
        reason = "time_exit"
        if px <= stop:
            reason = "stop"
            exit_prem = stop if fill == "bands" else px
            break
        if px >= target:
            reason = "target"
            exit_prem = target if fill in {"cap", "bands"} else px
            break
    qty = lot * lots
    entry_day = layer["timestamp"][:10]
    return {
        "day": entry_day,
        "symbol": name,
        "kind": kind,
        "entry_ts": f"{entry_day}T09:15:00+05:30",
        "exit_ts": f"{exit_bar['timestamp'][:10]}T15:00:00+05:30",
        "entry_premium": round(entry_prem, 2),
        "exit_premium": round(exit_prem, 2),
        "strike": strike,
        "lots": lots,
        "qty": qty,
        "reason": reason,
        "gross_pnl": round((exit_prem - entry_prem) * qty, 2),
        "costs": round(option_costs(entry_prem, exit_prem, lot, lots), 2),
        "pnl": round((exit_prem - entry_prem) * qty
                     - option_costs(entry_prem, exit_prem, lot, lots), 2),
        "strategy": "five_day_green_reversal",
    }


def evaluate(candles: List[Dict[str, Any]], name: str, meta: Dict[str, Any],
             window: Tuple[str, str]) -> Dict[str, Any]:
    strategy = FiveDayGreenReversalStrategy(lot_size=meta["lot"])
    specs = {"lot_size": meta["lot"], "strike_step": meta["step"],
             "expiry_preference": "monthly"}
    start, end = window
    out: Dict[str, Any] = {"signals_in_window": 0,
                           "signals": [], "signals_all_cache": 0}
    for index in range(1, len(candles) - 1):
        try:
            signal = strategy.evaluate_signal(candles, index, specs, name)
        except Exception:
            continue
        kind = str(getattr(signal, "option_type", "") or "").upper()
        if not bool(getattr(signal, "allowed", False)) or kind not in {"CE", "PE"}:
            continue
        out["signals_all_cache"] += 1
        entry_day = candles[index + 1]["timestamp"][:10]
        if not (start <= entry_day <= end):
            continue
        out["signals_in_window"] += 1
        out["signals"].append((index, kind))
    return out


def per_symbol(trades: List[Dict[str, Any]], minimum: int = 2) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        grouped[trade["symbol"]].append(trade)
    rows = [stats(group, symbol) for symbol, group in grouped.items()
            if len(group) >= minimum]
    return sorted(rows, key=lambda row: row["net"], reverse=True)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=60,
                        help="trading sessions in the signal window (default 60)")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--limit", type=int, default=None,
                        help="cap the universe size (alphabetical, for smoke runs)")
    parser.add_argument("--max-open", type=int, default=2)
    parser.add_argument("--max-entries", type=int, default=10)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--json", type=Path,
                        default=ROOT / "output" / "research_green_reversal_wide.json")
    args = parser.parse_args(argv)

    universe = nfo_universe()
    names = sorted(universe)
    if args.limit:
        names = names[:args.limit]
    universe = {name: universe[name] for name in names}
    print(f"NSE F&O underlyings with options: {len(universe)}")

    frames = fetch_daily(universe, args.refresh)
    available = {name: meta for name, meta in universe.items()
                 if meta["ticker"] in frames}
    missing = sorted(set(universe) - set(available))
    print(f"Daily data available: {len(available)}"
          + (f"  (missing {len(missing)}: {', '.join(missing[:8])}...)"
             if missing else ""))

    sessions = sorted({
        pd.Timestamp(ts).date().isoformat()
        for name in available
        for ts in frames[available[name]["ticker"]].index
    })
    if not sessions:
        print("No daily sessions available.")
        return 1
    default_start = sessions[max(0, len(sessions) - args.sessions)]
    window = (args.start or default_start, args.end or sessions[-1])
    print(f"Signal window: {window[0]} .. {window[1]}"
          f"  ({args.sessions} sessions)")

    books: Dict[str, List[Dict[str, Any]]] = {"close": [], "cap": [], "bands": []}
    total = {"signals_in_window": 0, "signals_all_cache": 0,
             "dropped_capital": 0, "symbols_with_signal": 0,
             "symbols": 0, "entry_days": {}}
    for name, meta in sorted(available.items()):
        candles = to_daily_candles(frames[meta["ticker"]], name, meta["kind"])
        result = evaluate(candles, name, meta, window)
        total["signals_in_window"] += result["signals_in_window"]
        total["signals_all_cache"] += result["signals_all_cache"]
        if result["signals_in_window"]:
            total["symbols_with_signal"] += 1
        for index, kind in result["signals"]:
            entry_day = candles[index + 1]["timestamp"][:10]
            total["entry_days"][entry_day] = (
                total["entry_days"].get(entry_day, 0) + 1)
            built = False
            for policy in books:
                trade = build_trade(candles, index, kind, name, meta,
                                    fill=policy)
                if trade is None:
                    continue
                books[policy].append(trade)
                built = True
            if not built:
                total["dropped_capital"] += 1

    uncapped = sorted(books["bands"], key=lambda t: t["entry_ts"])
    uncapped_cap = sorted(books["cap"], key=lambda t: t["entry_ts"])
    uncapped_close = sorted(books["close"], key=lambda t: t["entry_ts"])
    capped = select_portfolio(books["bands"], max_open=args.max_open,
                              max_entries=args.max_entries)

    print("\nSIGNAL FUNNEL")
    print(f"  signals in 1y daily cache : {total['signals_all_cache']}")
    print(f"  signals inside window     : {total['signals_in_window']}"
          f"  (on {total['symbols_with_signal']} symbols)")
    print(f"  dropped by capital model  : {total['dropped_capital']}")
    print(f"  tradable candidates       : {len(uncapped)}")
    print(f"  distinct entry days       : {len(total['entry_days'])}"
          f"  (max {max(total['entry_days'].values()) if total['entry_days'] else 0}"
          " signals on one day)")
    print("  exit models: 'bands' fills the +/-30/20% resting orders;"
          " 'target-capped' also lets stops slip to the close;"
          " 'mark-at-close' books the whole gap")

    print_table([stats(uncapped, "uncapped, bands (+30/-20)"),
                 stats(uncapped_cap, "uncapped, stop at close"),
                 stats(uncapped_close, "uncapped, mark-at-close"),
                 stats(capped, f"portfolio cap {args.max_open}/{args.max_entries}")],
                "5-DAY GREEN REVERSAL")

    rows = per_symbol(uncapped)
    if rows:
        print_table(rows[:10], "TOP 10 SYMBOLS BY NET (bands)")
        print_table(rows[-10:], "BOTTOM 10 SYMBOLS BY NET (bands)")

    payload = {
        "generated_at": pd.Timestamp.now(tz=IST).isoformat(),
        "universe": {"underlyings": len(universe), "with_data": len(available),
                     "missing": missing},
        "window": {"start": window[0], "end": window[1],
                   "sessions": args.sessions},
        "model": {
            "pricing": "ATM Black-Scholes, 5 DTE, 18% IV",
            "stop_fraction": STOP_FRACTION,
            "target_fraction": TARGET_FRACTION,
            "hold_sessions": HOLD_SESSIONS,
            "max_open": args.max_open,
            "max_entries_per_day": args.max_entries,
            "exits": "bands (+30% target / -20% stop fills); stop-at-close and "
                     "mark-at-close sentinels also stored",
            "costs": "brokerage+STT+exchange+GST+stamp+SEBI + 1 tick slippage/side",
        },
        "funnel": {key: value for key, value in total.items()
                   if key != "entry_days"},
        "entry_days": total["entry_days"],
        "uncapped": stats(uncapped, "uncapped"),
        "uncapped_target_cap": stats(uncapped_cap, "uncapped_target_cap"),
        "uncapped_mark_at_close": stats(uncapped_close, "uncapped_close"),
        "portfolio": stats(capped, "portfolio"),
        "by_symbol": rows,
        "trades": {"uncapped": uncapped,
                   "uncapped_target_cap": uncapped_cap,
                   "uncapped_mark_at_close": uncapped_close,
                   "portfolio": capped},
    }
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(payload, indent=2, default=str),
                         encoding="utf-8")
    print(f"\nSaved: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
