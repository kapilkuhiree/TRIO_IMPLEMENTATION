"""Research: can a one-trade-per-day rule reach an 80-90% win rate?

Why this script exists
----------------------
Win rate is mostly a *geometry* choice, not a discovery.  At a fixed stop, a
target of ``r`` times the risk breaks even at ``1/(1+r)`` even with no edge:

    r = 0.11  ->  ~90% break-even win rate
    r = 0.25  ->  ~80%
    r = 1.00  ->  ~50%

So "80-90% win rate" is trivial to manufacture by shrinking the target.  The
only question worth answering is whether a rule still has **positive expectancy
after costs** at that geometry.

Controls (the part that makes the result believable)
----------------------------------------------------
1. **Win rate is a target hit**, not "net R > 0".  Costs are reported
   separately, so a win that barely covers the spread is still a win.
2. **No same-bar look-ahead.**  A rule that decides on a bar's close fills at
   the *next bar's open*.  A pullback limit fills inside the bar at the limit
   price, but the trend filter uses the *previous* bar, and the entry bar can
   only produce a stop, never a target.
3. **Random-entry control.**  The same direction, stop and target with a
   random entry inside the same time window.  If random entries win just as
   often, the rule adds nothing and the result is drift, not timing.
4. **Walk-forward halves.**  First 60% of sessions vs last 40%.
5. **Cost sensitivity.**  Option costs are charged in R, because that ratio,
   not the raw fee, decides whether a 0.25R target survives.

Data: ``output/research/nifty_5m_90d.csv`` (Zerodha 5m, IST, ~61 sessions).

Nothing here is wired into the trading system.  It is a measurement.
"""

from __future__ import annotations

import argparse
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = ROOT / "output" / "research" / "nifty_5m_90d.csv"

TARGET_FRACTIONS = (0.10, 0.15, 0.20, 0.25, 0.33, 0.50, 1.00, 2.00)


@dataclass
class Signal:
    day: str
    idx: int
    direction: int          # +1 long, -1 short
    rule: str
    limit_price: Optional[float] = None   # None => fill at next bar open
    risk_points: float = 0.0


# ---------------------------------------------------------------------------
# Data preparation
# ---------------------------------------------------------------------------


def load_bars(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    if "timestamp" not in frame.columns:
        stamp = "date" if "date" in frame.columns else frame.columns[0]
        frame = frame.rename(columns={
            stamp: "timestamp", "Open": "open", "High": "high",
            "Low": "low", "Close": "close", "Volume": "volume",
        })
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["timestamp"] = frame["timestamp"].dt.tz_convert("Asia/Kolkata")
    frame = frame.sort_values("timestamp").reset_index(drop=True)
    frame["day"] = frame["timestamp"].dt.strftime("%Y-%m-%d")
    frame["minute"] = frame["timestamp"].dt.strftime("%H:%M")
    return frame


def add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    """Causal indicators: every value at bar t uses bars <= t only."""
    out = frame.copy()
    close = out["close"].astype(float)
    high = out["high"].astype(float)
    low = out["low"].astype(float)

    previous_close = close.shift(1)
    true_range = pd.concat([
        high - low,
        (high - previous_close).abs(),
        (low - previous_close).abs(),
    ], axis=1).max(axis=1)
    out["atr"] = true_range.rolling(14, min_periods=14).mean()

    typical = (high + low + close) / 3.0
    volume = out["volume"].astype(float).clip(lower=0.0)
    # Zerodha index candles often carry zero volume; fall back to equal weight.
    weight = volume.where(volume > 0, 1.0)
    out["vwap"] = ((typical * weight).groupby(out["day"]).cumsum()
                   / weight.groupby(out["day"]).cumsum())

    out["ema20"] = close.ewm(span=20, adjust=False).mean()
    out["ema50"] = close.ewm(span=50, adjust=False).mean()

    opening = out[out["minute"].isin({"09:15", "09:20", "09:25"})]
    ranges = opening.groupby("day").agg(orb_high=("high", "max"),
                                        orb_low=("low", "min"))
    out = out.merge(ranges, left_on="day", right_index=True, how="left")

    day_close = out.groupby("day")["close"].last().shift(1)
    out["prev_close"] = out["day"].map(day_close)
    return out


# ---------------------------------------------------------------------------
# Entry rules: at most one candidate per day, first match wins
# ---------------------------------------------------------------------------


def _session_rows(frame: pd.DataFrame, day: str) -> pd.DataFrame:
    return frame[frame["day"] == day]


def rule_orb_break(frame: pd.DataFrame, day: str) -> Optional[Signal]:
    """First 2-close break of the 09:15-09:30 opening range."""
    rows = _session_rows(frame, day)
    rows = rows[rows["minute"] >= "09:30"]
    above = below = 0
    for idx, row in rows.iterrows():
        atr = float(row["atr"]) if pd.notna(row["atr"]) else 0.0
        if atr <= 0 or pd.isna(row["orb_high"]):
            continue
        above = above + 1 if row["close"] > row["orb_high"] else 0
        below = below + 1 if row["close"] < row["orb_low"] else 0
        if above >= 2:
            return Signal(day, int(idx), +1, "orb_break", risk_points=1.0 * atr)
        if below >= 2:
            return Signal(day, int(idx), -1, "orb_break", risk_points=1.0 * atr)
    return None


def rule_vwap_fade(frame: pd.DataFrame, day: str) -> Optional[Signal]:
    """Fade a 1.5-ATR stretch away from session VWAP after 11:00."""
    rows = _session_rows(frame, day)
    rows = rows[(rows["minute"] >= "11:00") & (rows["minute"] <= "14:30")]
    for idx, row in rows.iterrows():
        atr = float(row["atr"]) if pd.notna(row["atr"]) else 0.0
        if atr <= 0 or pd.isna(row["vwap"]):
            continue
        stretch = (float(row["close"]) - float(row["vwap"])) / atr
        if stretch >= 1.5:
            return Signal(day, int(idx), -1, "vwap_fade", risk_points=1.2 * atr)
        if stretch <= -1.5:
            return Signal(day, int(idx), +1, "vwap_fade", risk_points=1.2 * atr)
    return None


def rule_trend_pullback(frame: pd.DataFrame, day: str) -> Optional[Signal]:
    """Resting buy limit at EMA20 while the prior bar's trend stack is bullish."""
    rows = _session_rows(frame, day)
    rows = rows[(rows["minute"] >= "10:00") & (rows["minute"] <= "14:00")]
    previous = None
    for idx, row in rows.iterrows():
        atr = float(row["atr"]) if pd.notna(row["atr"]) else 0.0
        ema20 = float(row["ema20"]) if pd.notna(row["ema20"]) else None
        if previous is not None and atr > 0 and ema20 is not None:
            # Trend is judged on the completed previous bar only.
            trend_ok = (float(previous["close"]) > float(previous["vwap"])
                        and float(previous["ema20"]) > float(previous["ema50"])
                        and float(previous["close"]) > float(previous["ema20"]))
            tag = Signal(day, int(idx), +1, "trend_pullback",
                         limit_price=ema20, risk_points=1.0 * atr)
            if (trend_ok and float(row["open"]) > ema20
                    and float(row["low"]) <= ema20 <= float(row["high"])):
                return tag
        previous = row
    return None


def rule_gap_fade(frame: pd.DataFrame, day: str) -> Optional[Signal]:
    """Fade an opening gap of 0.25% or more, decision at 09:20."""
    rows = _session_rows(frame, day)
    first_two = rows[rows["minute"] <= "09:20"]
    if len(first_two) < 2:
        return None
    row = first_two.iloc[-1]
    prev_close = row["prev_close"]
    atr = float(row["atr"]) if pd.notna(row["atr"]) else 0.0
    if pd.isna(prev_close) or atr <= 0:
        return None
    gap = (float(row["close"]) - float(prev_close)) / float(prev_close)
    if abs(gap) < 0.0025:
        return None
    direction = -1 if gap > 0 else +1
    return Signal(day, int(row.name), direction, "gap_fade", risk_points=1.2 * atr)


def rule_range_fade(frame: pd.DataFrame, day: str) -> Optional[Signal]:
    """Fade a fresh touch of the day's extreme inside a compressed session."""
    rows = _session_rows(frame, day)
    rows = rows[(rows["minute"] >= "12:00") & (rows["minute"] <= "14:00")]
    for idx, row in rows.iterrows():
        atr = float(row["atr"]) if pd.notna(row["atr"]) else 0.0
        if atr <= 0:
            continue
        upto = frame[(frame["day"] == day) & (frame.index <= idx)]
        day_high = float(upto["high"].max())
        day_low = float(upto["low"].min())
        if (day_high - day_low) / atr > 8.0:
            return None        # trending day: the range only widens from here
        close = float(row["close"])
        if day_high - close <= 0.2 * atr:
            return Signal(day, int(idx), -1, "range_fade", risk_points=1.0 * atr)
        if close - day_low <= 0.2 * atr:
            return Signal(day, int(idx), +1, "range_fade", risk_points=1.0 * atr)
    return None


RULES: Dict[str, Callable[[pd.DataFrame, str], Optional[Signal]]] = {
    "orb_break": rule_orb_break,
    "vwap_fade": rule_vwap_fade,
    "trend_pullback": rule_trend_pullback,
    "gap_fade": rule_gap_fade,
    "range_fade": rule_range_fade,
}


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------


def simulate(frame: pd.DataFrame, signal: Signal, target_r: float,
             cost_r: float) -> Optional[Dict[str, object]]:
    """Walk forward from the entry; return one outcome or None."""
    rows = _session_rows(frame, signal.day)
    risk = float(signal.risk_points)
    if risk <= 0:
        return None

    if signal.limit_price is None:
        # Decision is made on the signal bar's close; fill at the next open.
        future_all = rows[rows.index > signal.idx]
        if future_all.empty:
            return None
        entry = float(future_all.iloc[0]["open"])
        start_index = future_all.index[0]
        # Entry bar: a gap can put the stop immediately in play, never the
        # target (the decision to hold only exists after the fill).
        check_entry_bar = True
    else:
        entry = float(signal.limit_price)
        future_all = rows[rows.index >= signal.idx]
        if future_all.empty:
            return None
        start_index = signal.idx
        check_entry_bar = True

    if signal.direction > 0:
        stop = entry - risk
        target = entry + target_r * risk
    else:
        stop = entry + risk
        target = entry - target_r * risk

    for position, (idx, row) in enumerate(future_all.iterrows()):
        high, low = float(row["high"]), float(row["low"])
        hit_stop = low <= stop if signal.direction > 0 else high >= stop
        hit_target = high >= target if signal.direction > 0 else low <= target
        if position == 0 and check_entry_bar and signal.limit_price is not None:
            hit_target = False      # filled inside the bar: target too optimistic
        if hit_stop and hit_target:
            outcome, exit_price = "stop", stop        # conservative
        elif hit_target:
            outcome, exit_price = "target", target
        elif hit_stop:
            outcome, exit_price = "stop", stop
        else:
            continue
        gross = ((exit_price - entry) * signal.direction) / risk
        return {
            "rule": signal.rule, "day": signal.day, "outcome": outcome,
            "gross_r": gross, "net_r": gross - cost_r,
            "exit_time": str(row["timestamp"]),
        }

    last = future_all.iloc[-1]
    gross = ((float(last["close"]) - entry) * signal.direction) / risk
    return {
        "rule": signal.rule, "day": signal.day, "outcome": "time",
        "gross_r": gross, "net_r": gross - cost_r,
        "exit_time": str(last["timestamp"]),
    }


def run_rule(frame: pd.DataFrame, rule: str, target_r: float,
             cost_r: float) -> List[Dict[str, object]]:
    trades: List[Dict[str, object]] = []
    builder = RULES[rule]
    for day in sorted(frame["day"].unique()):
        signal = builder(frame, day)
        if signal is None:
            continue
        result = simulate(frame, signal, target_r, cost_r)
        if result is not None:
            trades.append(result)
    return trades


def run_random(frame: pd.DataFrame, target_r: float, cost_r: float,
               seed: int = 7, direction: int = +1) -> List[Dict[str, object]]:
    """Control: random entry time in the same window, same geometry."""
    rng = random.Random(seed)
    trades: List[Dict[str, object]] = []
    for day in sorted(frame["day"].unique()):
        rows = _session_rows(frame, day)
        window = rows[(rows["minute"] >= "10:00") & (rows["minute"] <= "13:00")]
        if len(window) < 5:
            continue
        idx = int(rng.choice(list(window.index)))
        atr = float(frame.loc[idx, "atr"]) if pd.notna(frame.loc[idx, "atr"]) else 0.0
        if atr <= 0:
            continue
        result = simulate(frame, Signal(day, idx, direction, "random",
                                        risk_points=1.0 * atr),
                          target_r, cost_r)
        if result is not None:
            trades.append(result)
    return trades


def summarise(trades: List[Dict[str, object]]) -> Dict[str, float]:
    if not trades:
        return {}
    gross = np.array([float(t["gross_r"]) for t in trades])
    net = np.array([float(t["net_r"]) for t in trades])
    wins = sum(1 for t in trades if t["outcome"] == "target")
    cumulative = np.cumsum(net)
    drawdown = float(np.min(cumulative - np.maximum.accumulate(cumulative)))
    return {
        "trades": len(trades),
        "win_rate": 100.0 * wins / len(trades),
        "gross_r": float(gross.sum()),
        "net_r": float(net.sum()),
        "avg_r": float(net.mean()),
        "max_dd": drawdown,
        "time_exits": sum(1 for t in trades if t["outcome"] == "time"),
    }


def _row(label: str, target_r: float, stats: Dict[str, float]) -> str:
    if not stats:
        return f"  {label:<22} {target_r:>5.2f} {'-':>7}"
    break_even = 100.0 / (1.0 + target_r)
    return (f"  {label:<22} {target_r:>5.2f} {break_even:>9.1f}% "
            f"{stats['trades']:>6} {stats['win_rate']:>6.1f}% "
            f"{stats['gross_r']:>7.2f} {stats['net_r']:>7.2f} "
            f"{stats['avg_r']:>7.3f} {stats['max_dd']:>7.2f} "
            f"{stats['time_exits']:>5}")


HEADER = (f"  {'rule':<22} {'tgt':>5} {'break-even':>10} {'trades':>6} "
          f"{'win%':>7} {'grossR':>7} {'netR':>7} {'avgR':>7} {'maxDD':>7} "
          f"{'time':>5}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--cost-r", type=float, default=0.10,
                        help="round-trip cost per trade in R units")
    args = parser.parse_args(argv)

    if not args.csv.exists():
        print(f"missing data: {args.csv}")
        return 1

    frame = add_indicators(load_bars(args.csv))
    sessions = sorted(frame["day"].unique())
    print(f"data        : {args.csv.name}")
    print(f"sessions    : {len(sessions)}   bars: {len(frame)}")
    print(f"cost model  : {args.cost_r:.2f}R per round trip")
    print()

    # --- 1. sweep every rule at every target geometry -------------------
    print("TARGET GEOMETRY SWEEP")
    print(HEADER)
    for rule in RULES:
        for target_r in TARGET_FRACTIONS:
            stats = summarise(run_rule(frame, rule, target_r, args.cost_r))
            print(_row(rule, target_r, stats))
        print()

    # --- 2. random-entry control at the same geometry -------------------
    print("RANDOM-ENTRY CONTROL (long-only, random time 10:00-13:00)")
    print(HEADER)
    for target_r in TARGET_FRACTIONS:
        pooled: List[Dict[str, object]] = []
        for seed in range(20):
            pooled.extend(run_random(frame, target_r, args.cost_r, seed=seed))
        stats = summarise(pooled)
        if not stats:
            print(_row("random_long", target_r, {}))
            continue
        stats["trades"] = stats["trades"] // 20     # per-run average
        print(_row("random_long", target_r, stats))
    print()

    # --- 3. walk-forward halves for the only rule that pays -------------
    print("WALK-FORWARD HALVES: trend_pullback")
    split = int(len(sessions) * 0.6)
    first_days, second_days = set(sessions[:split]), set(sessions[split:])
    print(HEADER)
    for target_r in (0.25, 0.50, 1.00, 2.00):
        for label, days in (("first 60%", first_days), ("last 40%", second_days)):
            trades = [t for t in run_rule(frame, "trend_pullback", target_r, args.cost_r)
                      if t["day"] in days]
            print(_row(f"trend_pullback {label}", target_r, summarise(trades)))
    print()

    # --- 4. cost sensitivity -------------------------------------------
    print("COST SENSITIVITY: trend_pullback")
    print(f"  {'target':>7} " + " ".join(f"{c:>8.2f}R" for c in
                                         (0.0, 0.05, 0.10, 0.15, 0.20)))
    for target_r in (0.25, 0.50, 1.00, 2.00):
        cells = []
        for cost in (0.0, 0.05, 0.10, 0.15, 0.20):
            stats = summarise(run_rule(frame, "trend_pullback", target_r, cost))
            cells.append(f"{stats['net_r']:>9.2f}")
        print(f"  {target_r:>7.2f} " + " ".join(cells))
    print()

    print("Reading this:")
    print("  A row only matters when win% clears break-even AND netR > 0 AND")
    print("  it beats the random-entry control at the same geometry.")
    print("  Tiny targets clear the win-rate bar for free; the cost column is")
    print("  what decides whether the trade is worth taking.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
