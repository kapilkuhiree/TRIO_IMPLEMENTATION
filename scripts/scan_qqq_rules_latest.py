"""Live-ish scan: does the QQQ daily rule fire today or yesterday anywhere?

Applies the exact canonical QQQ rules to every NFO option underlying and
prints the indicator state for the last two daily bars:

* RSI(3) < 22,
* EMA(100) above its value 5 sessions ago,
* close <= 10-day rolling VWAP x 0.99,
* entry at the next session's open.

A signal on the previous session means "enter at today's open" (already
actionable); a signal on the latest bar means "enter at the next open".

IMPORTANT: Yahoo's daily bar for the current session is a live intraday
snapshot until 15:30 IST.  Anything dated today is provisional; the script
prints the bar timestamp so the staleness is explicit.

    python scripts/scan_qqq_rules_latest.py
    python scripts/scan_qqq_rules_latest.py --refresh
    python scripts/scan_qqq_rules_latest.py --watchlist 40

DISCLAIMER: Educational purposes only. Not financial advice.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest_qqq_mean_reversion import build_signals  # noqa: E402
from scripts.backtest_qqq_rules_wide import (  # noqa: E402
    DEFAULT_END,
    DEFAULT_START,
    load_bars,
    load_universe,
    split_segments,
)

DEFAULT_CSV = ROOT / "output" / "qqq_rules_scan_latest.csv"
DEFAULT_JSON = ROOT / "output" / "qqq_rules_scan_latest.json"


def prepare(bars: pd.DataFrame, *, is_index: bool) -> pd.DataFrame:
    """Same hygiene as the wide backtest: total-return bars, no dead rows."""
    bars = bars.copy()
    if is_index or float(bars["Volume"].fillna(0.0).sum()) <= 0:
        bars["Volume"] = 1.0
    else:
        factor = (bars["Adj Close"] / bars["Close"]).replace(
            [np.inf, -np.inf], np.nan).ffill().fillna(1.0)
        for column in ("Open", "High", "Low", "Close"):
            bars[column] = bars[column] * factor
    return bars


def scan_symbol(name: str, bars: pd.DataFrame, *, is_index: bool,
                days: int = 2) -> List[Dict[str, Any]]:
    bars = prepare(bars, is_index=is_index)
    segment = split_segments(bars)[-1]
    if len(segment) < 110:
        return []
    signals = build_signals(segment)
    tail = signals.tail(days)
    # Exact fill timing: a signal on bar i is filled at bar i+1's open, so the
    # position is open from the close of bar i+1.  A signal on the latest bar
    # is therefore pending, not open.
    states: List[bool] = []
    in_position = False
    for position in range(len(segment)):
        if position > 0:
            if in_position and bool(signals["exit"].iloc[position - 1]):
                in_position = False
            elif not in_position and bool(signals["entry"].iloc[position - 1]):
                in_position = True
        states.append(in_position)
    rows: List[Dict[str, Any]] = []
    last_index = len(segment) - 1
    for stamp, row in tail.iterrows():
        close = float(segment.loc[stamp, "Close"])
        vwap = float(row["vwap"]) if pd.notna(row["vwap"]) else float("nan")
        position = segment.index.get_loc(stamp)
        # A signal on the prior bar is executed at the latest bar's open.
        filled_open = (float(segment["Open"].iloc[last_index])
                       if position == last_index - 1 else float("nan"))
        rows.append({
            "symbol": name,
            "date": stamp.date().isoformat(),
            "close": close,
            "rsi3": float(row["rsi"]),
            "ema100": float(row["ema"]),
            "ema_rising": bool(row["ema_rising"]),
            "vwap10": vwap,
            "vwap_gap_pct": (close / vwap - 1.0) * 100.0 if vwap else float("nan"),
            "entry": bool(row["entry"]),
            "exit": bool(row["exit"]),
            "holding": states[position],
            "next_open": filled_open,
        })
    return rows


def classify(row: Dict[str, Any]) -> str:
    if row["entry"]:
        return "SIGNAL"
    if not row["ema_rising"]:
        return "trend down"
    if row["rsi3"] < 22:
        return "needs VWAP discount"
    if row["vwap_gap_pct"] <= -1.0:
        return "needs RSI"
    if row["rsi3"] < 35 and row["vwap_gap_pct"] < 0:
        return "close"
    return "-"


def _fmt(value: float, digits: int = 1) -> str:
    if value is None or not np.isfinite(value):
        return "-"
    return f"{value:,.{digits}f}"


def print_table(rows: List[Dict[str, Any]], *, title: str,
                limit: int = 0) -> None:
    print(f"\n{title}")
    print("=" * 112)
    header = (f"{'Symbol':<14}{'Date':<12}{'Close':>11}{'RSI3':>7}{'EMA up':>8}"
              f"{'VWAP':>11}{'vs VWAP':>9}{'Entry':>8}  State")
    print(header)
    print("-" * len(header))
    shown = rows if not limit else rows[:limit]
    for row in shown:
        print(f"{row['symbol']:<14}{row['date']:<12}{_fmt(row['close'], 2):>11}"
              f"{_fmt(row['rsi3']):>7}{'yes' if row['ema_rising'] else 'no':>8}"
              f"{_fmt(row['vwap10'], 2):>11}{_fmt(row['vwap_gap_pct']):>8}%"
              f"{'YES' if row['entry'] else '-':>8}  {classify(row)}")
    if limit and len(rows) > limit:
        print(f"... {len(rows) - limit} more rows in the CSV")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--refresh", action="store_true",
                        help="re-download the daily cache before scanning")
    parser.add_argument("--watchlist", type=int, default=25,
                        help="rows of near-miss names to print (0 = none)")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    args = parser.parse_args(argv)

    if args.refresh:
        from scripts.backtest_qqq_rules_wide import download_daily
        download_daily(load_universe(), start=args.start, end=args.end,
                       refresh=True)

    universe = load_universe()
    rows: List[Dict[str, Any]] = []
    missing: List[str] = []
    for name, meta in universe.items():
        path = ROOT / "output" / "qqq_wide_daily" / (
            name.replace("&", "AND").replace("/", "_") + ".csv")
        if not path.exists():
            missing.append(name)
            continue
        try:
            bars = load_bars(str(path), is_index=meta["kind"] == "index")
        except Exception as exc:  # noqa: BLE001
            print(f"  {name}: unreadable ({exc})")
            missing.append(name)
            continue
        rows.extend(scan_symbol(name, bars,
                                is_index=meta["kind"] == "index", days=2))

    if not rows:
        print("No data.")
        return 1

    frame = pd.DataFrame(rows)
    frame["state"] = frame.apply(classify, axis=1)
    frame.to_csv(args.csv, index=False)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "rows": rows,
    }, indent=2, default=str), encoding="utf-8")

    latest = frame["date"].max()
    prior = sorted(frame["date"].unique())[-2] if frame["date"].nunique() > 1 else latest
    print(f"Scanned {frame['symbol'].nunique()} symbols | "
          f"bars {prior} and {latest} | generated {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("NOTE: the latest bar is a live intraday snapshot until 15:30 IST.")

    for label, day in (("TODAY", latest), ("YESTERDAY", prior)):
        day_rows = frame[frame["date"] == day]
        signals = day_rows[day_rows["entry"]].sort_values("rsi3")
        print(f"\n{label} ({day}): {len(signals)} signal(s) out of "
              f"{len(day_rows)} symbols")
        if len(signals):
            print_table(signals.to_dict("records"),
                        title=f"{label} ENTRY SIGNALS", limit=0)

        # Yesterday's signals were bought at today's open, so mark them to the
        # latest close.
        if label == "YESTERDAY" and len(signals):
            print(f"\nYESTERDAY SIGNALS — mark-to-market at {latest} close")
            print("-" * 72)
            for row in signals.to_dict("records"):
                entry = row["next_open"]
                last = frame[(frame["symbol"] == row["symbol"])
                             & (frame["date"] == latest)]
                if not len(last) or not np.isfinite(entry):
                    continue
                close = float(last["close"].iloc[0])
                print(f"  {row['symbol']:<14} entry {entry:>10,.2f}  "
                      f"close {close:>10,.2f}  "
                      f"P&L {(close / entry - 1) * 100:>+6.2f}%")

        open_now = day_rows[day_rows["holding"]]
        if len(open_now):
            pending = open_now[open_now["exit"]]
            print(f"\n{label}: {len(open_now)} open position(s) from this rule; "
                  f"{len(pending)} with RSI3 > 55 -> sell next open")
            if len(pending):
                print("  " + ", ".join(sorted(pending["symbol"])))

        if args.watchlist:
            near = day_rows[(~day_rows["entry"])
                            & (day_rows["ema_rising"])
                            & (day_rows["rsi3"] < 35)
                            & (day_rows["vwap_gap_pct"] < 0)]
            near = near.sort_values("rsi3").head(args.watchlist)
            if len(near):
                print_table(near.to_dict("records"),
                            title=f"{label} NEAR MISSES (uptrend, RSI<35, below VWAP)",
                            limit=args.watchlist)

    print(f"\nSaved CSV : {args.csv}")
    print(f"Saved JSON: {args.json}")
    if missing:
        print(f"No cache for {len(missing)}: {', '.join(missing[:10])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
