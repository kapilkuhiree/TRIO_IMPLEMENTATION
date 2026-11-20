"""Wide 5-minute backtest for the three active options strategies.

Runs the exact live logic for ``orb``, ``orb_retest`` and ``mean_reversion``
across every NSE F&O underlying that has options today, using the same option
model as ``scripts/backtest_orb_retest_v2.py``:

* real NFO lot size and strike step per underlying (cached Kite dump),
* ATM Black-Scholes, 5 DTE, flat 18% IV, long CE/PE only,
* premium stop -20%, target +30%, hard flat 15:10,
* brokerage + STT + exchange + GST + stamp + SEBI + 1 tick slippage/side,
* Rs 7,000 risk/trade, max premium Rs 18,000,
* live entry floors: ORB 10:00, ORB-Retest 09:45, MR 09:30,
* loss-only 300s per-strategy cooldown, max 10 entries/day, one open position
  per symbol per strategy, portfolio cap 2 open / 10 entries a day.

Reports ARE per strategy, per candidate (symbol) and per month, plus the
combined portfolio with the live priority order (retest > MR > ORB).

Two filters keep the wide screen honest:

* contracts whose modelled entry premium is below Rs 5 are dropped, because
  the flat 18% IV prices penny underlyings near the Rs 0.05 tick and the
  per-side slippage then dwarfs the premium;
* each strategy is reported twice - "all signals" (10 entries/day per symbol,
  one position per symbol) and "capped 2/10" (the real 2-open / 10-entry book)
  - so the portfolio numbers are not inflated by trading 200 names at once.

Yahoo only serves 60 days of 5-minute history, so the window is 60 sessions.
Signal collection is CPU-bound (the strategies rebuild their own indicators on
every bar), so it is spread over a process pool.

    python scripts/backtest_active_wide.py
    python scripts/backtest_active_wide.py --limit 40 --workers 8
    python scripts/backtest_active_wide.py --refresh-data
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import pickle
import sys
import time
from collections import defaultdict
from datetime import timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest_orb_retest_v2 import (  # noqa: E402
    IST,
    collect_mr_signals,
    collect_orb_signals,
    collect_retest_signals,
    print_table,
    run_variant,
    select_portfolio,
    stats,
)

DATA_DIR = ROOT / "output" / "research_5m_wide"
UNIVERSE_SKIP = {"MIDCPNIFTY", "NIFTYNXT50", "NIFTYFPI"}
YAHOO_OVERRIDES = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "ZOMATO": "ETERNAL.NS",
    "M&M": "M&M.NS",
    "BAJAJ-AUTO": "BAJAJ-AUTO.NS",
    "NAM-INDIA": "NAM-INDIA.NS",
}

STRATEGY_PARAMS = {
    "orb": {"earliest_min": 45, "cooldown": 300, "cooldown_rule": "loss"},
    "orb_retest": {"earliest_min": 30, "cooldown": 0, "cooldown_rule": "any"},
    "mean_reversion": {"earliest_min": 15, "cooldown": 300,
                       "cooldown_rule": "loss"},
}
STRATEGIES = ("orb", "orb_retest", "mean_reversion")
PRIORITY = {"orb_retest": 0, "mean_reversion": 1, "orb": 2}


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------

def nfo_universe() -> Dict[str, Dict[str, Any]]:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "TRIO" / "instruments"
    files = sorted(base.glob("NFO-*.pkl"))
    if not files:
        raise SystemExit("No cached NFO dump. Run scripts/zerodha_readonly_smoke.py")
    rows = pickle.load(files[-1].open("rb"))["rows"]
    options = [r for r in rows if r.get("instrument_type") in ("CE", "PE")]
    by_name: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in options:
        by_name[row["name"]].append(row)

    universe: Dict[str, Dict[str, Any]] = {}
    for name, instruments in by_name.items():
        if name in UNIVERSE_SKIP:
            continue
        lots = sorted({int(r["lot_size"]) for r in instruments if r.get("lot_size")})
        expiries = sorted({r["expiry"] for r in instruments})
        nearest = [r for r in instruments if r["expiry"] == expiries[0]]
        strikes = sorted({float(r["strike"]) for r in nearest})
        diffs = [round(b - a, 4) for a, b in zip(strikes, strikes[1:])]
        if not lots or not diffs:
            continue
        universe[name] = {
            "name": name,
            "lot": lots[0],
            "step": max(set(diffs), key=diffs.count),
            "vol": 0.18,
            "ticker": YAHOO_OVERRIDES.get(name, f"{name}.NS"),
            "kind": "index" if name in {"NIFTY", "BANKNIFTY", "FINNIFTY"} else "stock",
        }
    return universe


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    if hasattr(df.columns, "levels"):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["Close"])
    df.index = pd.to_datetime(df.index, utc=True).tz_convert(IST)
    return df[(df.index.time >= pd.Timestamp("09:15").time()) &
              (df.index.time <= pd.Timestamp("15:30").time())]


def download_5m(universe: Dict[str, Dict[str, Any]], days: int,
                refresh: bool) -> Dict[str, str]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    paths: Dict[str, str] = {}
    pending = []
    for name, meta in universe.items():
        path = DATA_DIR / f"{name.replace('&', 'AND')}.pkl"
        if path.exists() and not refresh:
            try:
                if len(pd.read_pickle(path)) > 100:
                    paths[name] = str(path)
                    continue
            except Exception:
                pass
        pending.append((name, meta, path))

    if not pending:
        return paths

    print(f"downloading 5m data for {len(pending)} underlyings...")
    for start in range(0, len(pending), 15):
        chunk = pending[start:start + 15]
        tickers = [meta["ticker"] for _, meta, _ in chunk]
        try:
            data = yf.download(tickers, period=f"{days}d", interval="5m",
                               auto_adjust=False, progress=False,
                               group_by="ticker", threads=True)
        except Exception as exc:
            print(f"  chunk failed: {exc}")
            continue
        for name, meta, path in chunk:
            try:
                frame = data[meta["ticker"]] if isinstance(data.columns, pd.MultiIndex) else data
                frame = _normalise(frame)
                if len(frame) < 100:
                    continue
                frame.to_pickle(path)
                paths[name] = str(path)
            except Exception:
                continue
        print(f"  {min(start + 15, len(pending))}/{len(pending)} fetched, "
              f"{len(paths)} usable")
    return paths


def to_candles(df: pd.DataFrame, name: str, kind: str) -> List[Dict[str, Any]]:
    return [{
        "timestamp": ts.isoformat(),
        "open": float(r["Open"]), "high": float(r["High"]),
        "low": float(r["Low"]), "close": float(r["Close"]),
        "volume": float(r.get("Volume") or 0.0),
        "underlying_symbol": name, "exchange": "NSE",
        "instrument_type": kind,
    } for ts, r in df.iterrows()]


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def _worker(payload: Tuple[str, Dict[str, Any], str]) -> Dict[str, Any]:
    name, meta, path = payload
    frame = pd.read_pickle(path)
    candles = to_candles(frame, name, meta["kind"])
    by_date: Dict[str, List[int]] = defaultdict(list)
    for i, candle in enumerate(candles):
        by_date[candle["timestamp"][:10]].append(i)

    orb = collect_orb_signals(candles, name, meta["lot"], meta["step"])
    retest = collect_retest_signals(
        candles, name, meta["lot"], meta["step"],
        or_min_width_atr=3.0, max_breakout_body=0.90,
        resumption_buffer_atr=0.20, atr_pct_max=0.30)
    mr = collect_mr_signals(candles)

    signal_map = {"orb": orb, "orb_retest": retest, "mean_reversion": mr}
    trades: Dict[str, List[Dict[str, Any]]] = {}
    for strategy, signals in signal_map.items():
        trades[strategy] = run_variant(
            candles, by_date, signals, meta,
            strategy=strategy, **STRATEGY_PARAMS[strategy])

    return {
        "name": name,
        "bars": len(candles),
        "signals": {k: len(v) for k, v in signal_map.items()},
        "trades": trades,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def monthly_rows(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_month: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        by_month[trade["day"][:7]].append(trade)
    return [stats(group, month) for month, group in sorted(by_month.items())]


def symbol_rows(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        by_symbol[trade["symbol"]].append(trade)
    rows = [stats(group, symbol) for symbol, group in by_symbol.items()]
    return sorted(rows, key=lambda r: r["net"], reverse=True)


def month_table(books: Dict[str, List[Dict[str, Any]]],
                months: List[str]) -> None:
    print(f"\nMONTHLY NET P&L (Rs)")
    print("-" * 92)
    header = f"{'Month':<10}{'ORB':>14}{'Retest':>14}{'MR':>14}{'Combined':>16}"
    print(header)
    print("-" * 92)
    for month in months:
        cells = []
        for key in ("orb", "orb_retest", "mean_reversion", "combined"):
            group = [t for t in books[key] if t["day"].startswith(month)]
            net = sum(t["pnl"] for t in group)
            cells.append(f"{net:,.0f} ({len(group)})")
        print(f"{month:<10}{cells[0]:>14}{cells[1]:>14}{cells[2]:>14}{cells[3]:>16}")
    print("-" * 92)
    total = []
    for key in ("orb", "orb_retest", "mean_reversion", "combined"):
        net = sum(t["pnl"] for t in books[key])
        total.append(f"{net:,.0f} ({len(books[key])})")
    print(f"{'TOTAL':<10}{total[0]:>14}{total[1]:>14}{total[2]:>14}{total[3]:>16}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=min(10, os.cpu_count() or 4))
    parser.add_argument("--refresh-data", action="store_true")
    parser.add_argument("--min-premium", type=float, default=5.0,
                        help="drop contracts whose modelled entry premium is "
                             "below this (tick-size sanity, default 5)")
    parser.add_argument("--from-json", type=Path, default=None,
                        help="recompute the report from an existing artifact")
    parser.add_argument("--json", type=Path,
                        default=ROOT / "output" / "research_active_wide.json")
    parser.add_argument("--csv", type=Path,
                        default=ROOT / "output" / "research_active_wide_trades.csv")
    parser.add_argument("--candidates-csv", type=Path,
                        default=ROOT / "output" / "research_active_candidates.csv")
    args = parser.parse_args(argv)

    signals: Dict[str, int] = {s: 0 for s in STRATEGIES}
    raw_books: Dict[str, List[Dict[str, Any]]] = {s: [] for s in STRATEGIES}
    underlyings = 0
    sessions: List[str] = []

    if args.from_json:
        payload = json.loads(args.from_json.read_text(encoding="utf-8"))
        raw_books = {s: payload["strategies"][s]["trades"] for s in STRATEGIES}
        signals = payload.get("raw_signals", signals)
        underlyings = payload.get("underlyings", 0)
        print(f"Loaded {len(raw_books['orb'])} ORB / "
              f"{len(raw_books['orb_retest'])} retest / "
              f"{len(raw_books['mean_reversion'])} MR trades from "
              f"{args.from_json}")
    else:
        universe = nfo_universe()
        names = sorted(universe)
        if args.limit:
            names = names[:args.limit]
        universe = {name: universe[name] for name in names}
        print(f"NSE F&O underlyings: {len(universe)}")

        paths = download_5m(universe, args.days, args.refresh_data)
        if not paths:
            print("No 5m data available.")
            return 1
        print(f"5m data ready for {len(paths)} underlyings")

        payloads = [(name, universe[name], path)
                    for name, path in sorted(paths.items())]
        started = time.time()
        results: List[Dict[str, Any]] = []
        with mp.Pool(processes=args.workers) as pool:
            for done, result in enumerate(pool.imap_unordered(_worker, payloads), 1):
                results.append(result)
                if done % 10 == 0 or done == len(payloads):
                    print(f"  evaluated {done}/{len(payloads)}"
                          f"  ({time.time() - started:.0f}s)")
        results.sort(key=lambda r: r["name"])
        for result in results:
            for strategy in STRATEGIES:
                raw_books[strategy].extend(result["trades"][strategy])
                signals[strategy] += result["signals"][strategy]
        underlyings = len(results)

    # Tick-size sanity: a Rs 2 premium is 40 ticks, but the model's flat 18%
    # IV makes cheap underlyings (IDEA, YESBANK, ...) price near the tick, and
    # the Rs 0.05/side slippage then dwarfs the premium.  Those contracts are
    # not tradeable in the real book either.
    books: Dict[str, List[Dict[str, Any]]] = {}
    dropped = 0
    for strategy in STRATEGIES:
        kept = [t for t in raw_books[strategy]
                if t["entry_premium"] >= args.min_premium]
        dropped += len(raw_books[strategy]) - len(kept)
        books[strategy] = kept
    capped = {
        s: select_portfolio(
            books[s], max_open=2, max_entries=10,
            cooldowns={s: STRATEGY_PARAMS[s]["cooldown"]})
        for s in STRATEGIES
    }
    combined_candidates = [t for s in STRATEGIES for t in books[s]]
    combined = select_portfolio(
        combined_candidates, max_open=2, max_entries=10,
        cooldowns={"orb": 300, "orb_retest": 0, "mean_reversion": 300})

    all_days = sorted({t["day"] for s in STRATEGIES for t in books[s]})
    months = sorted({day[:7] for day in all_days})
    window = (all_days[0], all_days[-1]) if all_days else ("", "")

    print(f"\nWindow: {window[0]} .. {window[1]}  ({len(all_days)} sessions, "
          f"{underlyings} underlyings)")
    print(f"Raw signals: " + ", ".join(f"{s}={signals[s]}" for s in STRATEGIES))
    print(f"Dropped by the Rs {args.min_premium:.0f} premium floor: {dropped}")

    rows = []
    for strategy in STRATEGIES:
        label = strategy.replace("_", " ")
        rows.append(stats(books[strategy], f"{label} — all signals"))
        rows.append(stats(capped[strategy], f"{label} — capped 2/10"))
    rows.append(stats(combined, "COMBINED — capped 2/10"))
    print_table(rows, "ACTIVE STACK — AFTER COSTS")

    month_books = dict(capped)
    month_books["combined"] = combined
    month_table(month_books, months)

    for strategy in STRATEGIES:
        ordered = symbol_rows(capped[strategy])
        print_table(ordered[:12], f"{strategy} — TOP 12 CANDIDATES (capped)")
        print_table(ordered[-12:], f"{strategy} — BOTTOM 12 CANDIDATES (capped)")

    combined_rows = symbol_rows(combined)
    print_table(combined_rows[:15], "COMBINED — TOP 15 CANDIDATES")

    args.csv.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(combined_candidates + combined).drop_duplicates(
        subset=["strategy", "symbol", "entry_ts"]).to_csv(args.csv, index=False)

    candidate_rows = []
    for strategy in STRATEGIES:
        for row in symbol_rows(capped[strategy]):
            candidate_rows.append({"book": strategy, **row})
    for row in combined_rows:
        candidate_rows.append({"book": "combined", **row})
    pd.DataFrame(candidate_rows).to_csv(args.candidates_csv, index=False)

    payload = {
        "generated_at": pd.Timestamp.now(tz=IST).isoformat(),
        "window": {"start": window[0], "end": window[1],
                   "sessions": len(all_days)},
        "underlyings": underlyings,
        "raw_signals": signals,
        "min_entry_premium": args.min_premium,
        "dropped_by_premium_floor": dropped,
        "model": {
            "pricing": "ATM Black-Scholes, 5 DTE, 18% IV",
            "stop_fraction": 0.20, "target_fraction": 0.30,
            "hard_flat": "15:10",
            "entry_floors": {s: STRATEGY_PARAMS[s]["earliest_min"]
                             for s in STRATEGIES},
            "cooldowns": {s: STRATEGY_PARAMS[s]["cooldown"] for s in STRATEGIES},
            "portfolio_cap": {"max_open": 2, "max_entries_per_day": 10},
            "costs": "brokerage+STT+exchange+GST+stamp+SEBI + 1 tick/side",
        },
        "strategies": {
            s: {
                "all_signals": stats(books[s], s),
                "capped": stats(capped[s], f"{s}_capped"),
                "by_symbol_capped": symbol_rows(capped[s]),
                "by_month_capped": monthly_rows(capped[s]),
                "trades": books[s],
            }
            for s in STRATEGIES
        },
        "combined": {
            "stats": stats(combined, "combined"),
            "by_symbol": combined_rows,
            "by_month": monthly_rows(combined),
            "trades": combined,
        },
    }
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(payload, indent=2, default=str),
                         encoding="utf-8")
    print(f"\nSaved: {args.json}")
    print(f"Saved: {args.csv}")
    print(f"Saved: {args.candidates_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
