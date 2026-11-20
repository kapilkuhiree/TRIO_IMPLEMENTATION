"""Research: can an intraday defined-risk short-premium spread end most days green?

Why this script exists
---------------------
The long-premium book cannot reach a high daily green rate with positive
expectancy: win rate is a target/stop geometry dial and every 80-90% setting
sits at or below its own break-even after costs (see HIGH_WINRATE_RESEARCH.md).

Selling premium is the one structure whose win rate is *structurally* high,
because most spreads expire worthless.  The open question is whether the
credit is large enough to pay for the occasional full-width loss.  This script
answers it on **real archived NIFTY option premiums**, not index points.

Method
------
* Entry decision 09:45 IST, fill at the 09:45 option bar close.
* Bull put spread: sell the put nearest ``spot * (1 - otm)``, buy the put
  ``width`` points lower, same expiry.
* Exit at the 15:05 bar close (respects the 15:10 hard flat), or earlier if
  the mark-to-market loss reaches ``stop_mult * credit``.
* Only bars with volume > 0 are used (stale marks are rejected).
* Premiums are mid/closes; costs are charged explicitly per spread round trip.

Data: ``output/research/nifty_5m_90d.csv`` (spot) and the SQLite option archive
at ``TRIO_OPTIONS_ARCHIVE_PATH``.  The archive covers 22 NIFTY sessions
(2026-10-12 -> 2026-11-11), which is a *pilot*, not proof.

Nothing here is wired into the trading system.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INDEX = ROOT / "output" / "research" / "nifty_5m_90d.csv"
DEFAULT_ARCHIVE = Path(
    os.environ.get("TRIO_OPTIONS_ARCHIVE_PATH")
    or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "TRIO" / "options"
    / "options_archive.sqlite3"
)

LOT_SIZE = 65
ENTRY_MINUTE = "09:45"
EXIT_MINUTE = "15:05"


@dataclass
class Spread:
    day: str
    expiry: str
    spot: float
    short_strike: float
    long_strike: float
    credit: float
    width: float


def load_index(path: Path) -> Dict[str, pd.DataFrame]:
    frame = pd.read_csv(path)
    if "timestamp" not in frame.columns:
        stamp = "date" if "date" in frame.columns else frame.columns[0]
        frame = frame.rename(columns={
            stamp: "timestamp", "Open": "open", "High": "high",
            "Low": "low", "Close": "close", "Volume": "volume",
        })
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["timestamp"] = frame["timestamp"].dt.tz_convert("Asia/Kolkata")
    frame["day"] = frame["timestamp"].dt.strftime("%Y-%m-%d")
    frame["minute"] = frame["timestamp"].dt.strftime("%H:%M")
    return {day: rows.reset_index(drop=True)
            for day, rows in frame.groupby("day")}


def load_chain(conn: sqlite3.Connection, day: str,
               option_type: str) -> Dict[Tuple[str, float, str], float]:
    """(expiry, strike, minute) -> close, volume-filtered."""
    rows = conn.execute(
        """
        SELECT c.expiry_date, c.strike, substr(k.timestamp_utc, 12, 5), k.close
        FROM candles k JOIN contracts c ON c.contract_id = k.contract_id
        WHERE c.underlying = 'NIFTY' AND c.option_type = ?
          AND substr(k.timestamp_utc, 1, 10) = ?
          AND k.volume > 0 AND k.close > 0
        """,
        (option_type, day),
    ).fetchall()
    chain: Dict[Tuple[str, float, str], float] = {}
    for expiry, strike, minute, close in rows:
        # 09:45 IST == 04:15 UTC, 15:05 IST == 09:35 UTC
        ist = _utc_minute_to_ist(minute)
        chain[(expiry, round(float(strike), 2), ist)] = float(close)
    return chain


def _utc_minute_to_ist(minute: str) -> str:
    hours, mins = (int(part) for part in minute.split(":"))
    total = hours * 60 + mins + 330
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


def pick_expiry(chain: Dict[Tuple[str, float, str], float], day: str,
                min_strikes: int = 12) -> Optional[str]:
    by_expiry: Dict[str, int] = {}
    for expiry, _strike, minute in chain:
        if minute == ENTRY_MINUTE:
            by_expiry[expiry] = by_expiry.get(expiry, 0) + 1
    candidates = [(expiry, count) for expiry, count in by_expiry.items()
                  if expiry > day and count >= min_strikes]
    if not candidates:
        return None
    return min(candidates)[0]


def build_spread(chain: Dict[Tuple[str, float, str], float], day: str,
                 expiry: str, spot: float, otm: float,
                 width: float) -> Optional[Spread]:
    target = spot * (1.0 - otm)
    strikes = sorted({strike for exp, strike, minute in chain
                      if exp == expiry and minute == ENTRY_MINUTE})
    if not strikes:
        return None
    short_strike = max((s for s in strikes if s <= target), default=None)
    if short_strike is None:
        return None
    long_strike = short_strike - width
    short_px = chain.get((expiry, short_strike, ENTRY_MINUTE))
    long_px = chain.get((expiry, long_strike, ENTRY_MINUTE))
    if short_px is None or long_px is None:
        return None
    credit = short_px - long_px
    if credit <= 0 or credit >= width:
        return None
    return Spread(day, expiry, spot, short_strike, long_strike, credit, width)


def simulate(chain: Dict[Tuple[str, float, str], float], spread: Spread,
             stop_mult: Optional[float], cost_points: float) -> Optional[Dict]:
    minutes = sorted({minute for exp, _s, minute in chain
                      if exp == spread.expiry and minute > ENTRY_MINUTE})
    if not minutes:
        return None
    exit_value = None
    exit_reason = "flat"
    for minute in minutes:
        short_px = chain.get((spread.expiry, spread.short_strike, minute))
        long_px = chain.get((spread.expiry, spread.long_strike, minute))
        if short_px is None or long_px is None:
            continue
        value = short_px - long_px          # debit to close
        if minute >= EXIT_MINUTE:
            exit_value, exit_reason = value, "time"
            break
        if stop_mult is not None and value >= stop_mult * spread.credit:
            exit_value, exit_reason = value, "stop"
            break
    if exit_value is None:
        return None
    gross = spread.credit - exit_value
    net = gross - cost_points
    max_loss = spread.width - spread.credit
    return {
        "day": spread.day, "expiry": spread.expiry, "spot": spread.spot,
        "short_k": spread.short_strike, "long_k": spread.long_strike,
        "credit": spread.credit, "exit_value": exit_value,
        "gross_points": gross, "net_points": net,
        "net_inr": net * LOT_SIZE, "max_loss": max_loss,
        "reason": exit_reason,
        "win": net > 0,
    }


def metrics(trades: List[Dict]) -> Dict[str, float]:
    if not trades:
        return {}
    net = [t["net_points"] for t in trades]
    gross = [t["gross_points"] for t in trades]
    wins = [v for v in net if v > 0]
    losses = [v for v in net if v <= 0]
    equity, peak, dd = 0.0, 0.0, 0.0
    for value in net:
        equity += value
        peak = max(peak, equity)
        dd = min(dd, equity - peak)
    streak = worst_streak = 0
    for value in net:
        streak = streak + 1 if value <= 0 else 0
        worst_streak = max(worst_streak, streak)
    return {
        "trades": len(trades),
        "green": len(wins),
        "green_pct": 100.0 * len(wins) / len(trades),
        "total_points": sum(net),
        "total_inr": sum(net) * LOT_SIZE,
        "avg_points": sum(net) / len(net),
        "avg_inr": sum(net) * LOT_SIZE / len(net),
        "worst_day_inr": min(net) * LOT_SIZE,
        "best_day_inr": max(net) * LOT_SIZE,
        "gross_points": sum(gross),
        "profit_factor": (sum(wins) / abs(sum(losses))) if losses else float("inf"),
        "max_dd_points": dd,
        "max_loss_streak": worst_streak,
        "avg_credit": sum(t["credit"] for t in trades) / len(trades),
        "break_even_win": (100.0 * (1 - sum(t["credit"] for t in trades)
                                    / sum(t["max_loss"] + t["credit"]
                                          for t in trades))),
    }


def run(index: Dict[str, pd.DataFrame], conn: sqlite3.Connection,
        otm: float, width: float, stop_mult: Optional[float],
        cost_points: float, min_strikes: int = 12) -> List[Dict]:
    trades: List[Dict] = []
    for day in sorted(index):
        rows = index[day]
        decision = rows[rows["minute"] == "09:40"]      # completed 09:45 bar
        if decision.empty:
            continue
        spot = float(decision.iloc[-1]["close"])
        chain = load_chain(conn, day, "PE")
        expiry = pick_expiry(chain, day, min_strikes)
        if expiry is None:
            continue
        spread = build_spread(chain, day, expiry, spot, otm, width)
        if spread is None:
            continue
        result = simulate(chain, spread, stop_mult, cost_points)
        if result is not None:
            trades.append(result)
    return trades


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--cost-points", type=float, default=2.0,
                        help="total round-trip cost for both legs, in points")
    args = parser.parse_args(argv)

    if not args.archive.exists():
        print(f"missing option archive: {args.archive}")
        return 1

    index = load_index(args.index)
    conn = sqlite3.connect(str(args.archive))

    print(f"spot data : {args.index.name} ({len(index)} sessions)")
    print(f"archive   : {args.archive}")
    print(f"cost      : {args.cost_points:.2f} points per spread round trip "
          f"(= Rs {args.cost_points * LOT_SIZE:.0f}/lot)")
    print()

    # --- 1. OTM / width sweep, no stop ---------------------------------
    print("BULL PUT SPREAD, 09:45 -> 15:05, NO STOP")
    print(f"  {'otm%':>5} {'width':>6} {'n':>4} {'green%':>7} {'BE win':>7} "
          f"{'avgCr':>7} {'netPts':>8} {'netRs/lot':>10} {'worstRs':>9} "
          f"{'PF':>6} {'streak':>7}")
    for otm in (0.005, 0.010, 0.015, 0.020, 0.025, 0.030):
        for width in (50, 100, 200):
            trades = run(index, conn, otm, width, None, args.cost_points)
            m = metrics(trades)
            if not m:
                print(f"  {otm*100:>5.1f} {width:>6}    0")
                continue
            print(f"  {otm*100:>5.1f} {width:>6} {m['trades']:>4} "
                  f"{m['green_pct']:>6.1f}% {m['break_even_win']:>6.1f}% "
                  f"{m['avg_credit']:>7.2f} {m['total_points']:>8.1f} "
                  f"{m['total_inr']:>10.0f} {m['worst_day_inr']:>9.0f} "
                  f"{m['profit_factor']:>6.2f} {m['max_loss_streak']:>7}")
        print()

    # --- 2. stop sweep on the most promising geometry -------------------
    print("STOP SWEEP: 1.5% OTM, 100 wide")
    print(f"  {'stop':>6} {'n':>4} {'green%':>7} {'netPts':>8} {'netRs':>9} "
          f"{'worstRs':>9} {'PF':>6}")
    for stop_mult in (None, 1.5, 2.0, 2.5, 3.0, 4.0):
        trades = run(index, conn, 0.015, 100, stop_mult, args.cost_points)
        m = metrics(trades)
        if not m:
            continue
        label = "none" if stop_mult is None else f"{stop_mult:.1f}x"
        print(f"  {label:>6} {m['trades']:>4} {m['green_pct']:>6.1f}% "
              f"{m['total_points']:>8.1f} {m['total_inr']:>9.0f} "
              f"{m['worst_day_inr']:>9.0f} {m['profit_factor']:>6.2f}")
    print()

    # --- 3. cost sensitivity -------------------------------------------
    print("COST SENSITIVITY: 1.5% OTM, 100 wide, no stop")
    print(f"  {'cost pts':>9} {'netPts':>8} {'netRs':>9} {'green%':>7}")
    for cost in (0.0, 1.0, 2.0, 3.0, 4.0, 6.0):
        trades = run(index, conn, 0.015, 100, None, cost)
        m = metrics(trades)
        if not m:
            continue
        print(f"  {cost:>9.1f} {m['total_points']:>8.1f} "
              f"{m['total_inr']:>9.0f} {m['green_pct']:>6.1f}%")
    print()

    # --- 4. day-by-day for the headline geometry ------------------------
    print("DAY BY DAY: 1.5% OTM, 100 wide, no stop")
    trades = run(index, conn, 0.015, 100, None, args.cost_points)
    print(f"  {'day':<11} {'expiry':<11} {'spot':>8} {'short':>7} "
          f"{'credit':>7} {'exit':>7} {'netRs':>8} {'why':>5}")
    for t in trades:
        print(f"  {t['day']:<11} {t['expiry']:<11} {t['spot']:>8.0f} "
              f"{t['short_k']:>7.0f} {t['credit']:>7.2f} "
              f"{t['exit_value']:>7.2f} {t['net_inr']:>8.0f} {t['reason']:>5}")
    m = metrics(trades)
    if m:
        print()
        print(f"  green days     : {m['green']}/{m['trades']} "
              f"({m['green_pct']:.1f}%)")
        print(f"  total          : {m['total_points']:+.1f} pts = "
              f"Rs {m['total_inr']:+,.0f}/lot")
        print(f"  avg per day    : {m['avg_points']:+.2f} pts = "
              f"Rs {m['avg_inr']:+,.0f}/lot")
        print(f"  worst / best   : Rs {m['worst_day_inr']:,.0f} / "
              f"Rs {m['best_day_inr']:,.0f}")
        print(f"  max drawdown   : {m['max_dd_points']:.1f} pts")
        print(f"  profit factor  : {m['profit_factor']:.2f}")
        print(f"  break-even win : {m['break_even_win']:.1f}% "
              f"(realised {m['green_pct']:.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
