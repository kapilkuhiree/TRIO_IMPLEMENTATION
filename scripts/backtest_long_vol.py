"""7-session NIFTY backtest for the long-volatility structure sleeves.

Usage (from repo root):
    python scripts/backtest_long_vol.py

The strategy modules are research-only: they emit signals from underlying
candles and refuse to model option P&L.  This script therefore reports TWO
separately labelled layers:

1. Signal layer (real data): counts/times from the actual strategy classes.
2. Structure outcome layer (MODELED): the move of the underlying from entry to
   15:10 IST, used only to say whether a direction-neutral long-volatility
   structure would have been in the money.  No option premium is fabricated
   and no claim of executable option P&L is made.
"""

from __future__ import annotations

import pickle
import sys
from collections import defaultdict
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from strategies.long_volatility_base import LongVolatilityStructure
from strategies.long_strangle_strategy import LongStrangleStrategy
from strategies.long_straddle_strategy import LongStraddleStrategy

IST = timezone(timedelta(hours=5, minutes=30))
PKL = ROOT / "output" / "post_expiry_5m_NIFTY_20260706_20261120.pkl"
SESSIONS = 7
ENTRY_CUTOFF = dtime(14, 30)
FLAT = dtime(15, 10)


def load_candles() -> list[dict]:
    frame = pickle.load(PKL.open("rb"))
    daily = sorted({ts.date() for ts in frame.index})
    keep = set(daily[-SESSIONS:])
    out = []
    for ts, row in frame.iterrows():
        if ts.date() not in keep:
            continue
        t = ts.tz_convert(IST).time()
        if not (dtime(9, 15) <= t <= dtime(15, 25)):
            continue
        out.append({
            "timestamp": ts.tz_convert(IST).isoformat(),
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
            "volume": float(row["Volume"]),
            "exchange": "NSE",
            "underlying_symbol": "NIFTY",
            "instrument_type": "index",
        })
    return out, sorted(keep)


def structure_outcome(candles: list[dict], index: int, entry: float,
                      legs: list) -> dict | None:
    """MODELED in-the-money test, never an option premium.

    A long straddle/strangle at strike(s) K is in the money at expiry-like
    end-of-day spot S when |S - K| exceeds the entry distance |S0 - K|.
    """
    offsets = [int(leg.offset) for leg in legs]
    atm = None
    step = 50.0
    for leg in legs:
        if int(leg.offset) == 0:
            atm = leg.strike
            break
    if atm is None and legs:
        atm = legs[0].strike - legs[0].offset * step
    if atm is None:
        return None
    strikes = [atm + off * step for off in offsets]
    flat_bar = None
    for row in candles[index + 1:]:
        ts = LongVolatilityStructure._parse_timestamp(row["timestamp"])
        if ts and ts.time() >= FLAT:
            flat_bar = row
            break
    if flat_bar is None:
        return None
    exit_spot = float(flat_bar["close"])
    entry_dist = sum(abs(entry - k) for k in strikes) / len(strikes)
    exit_dist = sum(abs(exit_spot - k) for k in strikes) / len(strikes)
    return {
        "exit_time": flat_bar["timestamp"],
        "exit_spot": round(exit_spot, 2),
        "entry_distance": round(entry_dist, 2),
        "exit_distance": round(exit_dist, 2),
        "modeled_itm": exit_dist > entry_dist,
    }


def main() -> None:
    candles, dates = load_candles()
    print(f"DATA   : {PKL.name}  (real NIFTY 5m candles)")
    print(f"WINDOW : {dates[0]} .. {dates[-1]}  ({len(dates)} sessions, {len(candles)} bars)")
    print()

    specs = {"lot_size": 65, "expiry_preference": "weekly", "strike_interval": 50}
    engines = {
        "long_strangle": LongStrangleStrategy(
            lot_size=65, trigger_strategies=("orb",)),
        "long_straddle": LongStraddleStrategy(
            lot_size=65, trigger_strategies=("orb",)),
    }

    by_date: dict[str, list[dict]] = defaultdict(list)
    for candle in candles:
        by_date[candle["timestamp"][:10]].append(candle)
    ordered_dates = sorted(by_date)

    for key, engine in engines.items():
        signals = []
        for day in ordered_dates:
            day_candles = by_date[day]
            for i, candle in enumerate(day_candles):
                ts = LongVolatilityStructure._parse_timestamp(candle["timestamp"])
                if ts is None or ts.time() > ENTRY_CUTOFF:
                    continue
                result = engine.evaluate_signal(
                    day_candles, i, specs, underlying_symbol="NIFTY")
                if not result.allowed:
                    continue
                outcome = structure_outcome(
                    day_candles, i, float(result.entry_price), result.legs)
                signals.append({
                    "date": day,
                    "time": ts.time().isoformat(timespec="minutes"),
                    "trigger": result.trigger_source,
                    "spot": round(float(result.entry_price), 2),
                    "strikes": [leg.strike for leg in result.legs],
                    "outcome": outcome,
                })

        itm = sum(1 for s in signals if s["outcome"] and s["outcome"]["modeled_itm"])
        firsts = []
        for day in ordered_dates:
            day_signals = [s for s in signals if s["date"] == day]
            if day_signals:
                firsts.append(day_signals[0])
        first_itm = sum(1 for s in firsts if s["outcome"] and s["outcome"]["modeled_itm"])
        print(f"=== {key.upper()} ({engine.NAME}, {engine.PAGE}) ===")
        print(f"raw ORB-triggered signals : {len(signals)}  "
              f"(modeled ITM {itm} / OTM {len(signals) - itm})")
        print(f"first entry per session   : {len(firsts)}  "
              f"(modeled ITM {first_itm} / OTM {len(firsts) - first_itm})")
        for s in firsts:
            o = s["outcome"] or {}
            print(f"  {s['date']} {s['time']} spot={s['spot']} strikes={s['strikes']} "
                  f"exit={o.get('exit_spot')} ITM={o.get('modeled_itm')}")
        print()
        print("First 12 raw signals:")
        for s in signals[:12]:
            o = s["outcome"] or {}
            print(f"  {s['date']} {s['time']} trigger={s['trigger']} spot={s['spot']} "
                  f"strikes={s['strikes']} exit={o.get('exit_spot')} "
                  f"ITM={o.get('modeled_itm')}")
        if len(signals) > 12:
            print(f"  ... {len(signals) - 12} more")
        print()

    # Baseline from the existing live sleeves on exactly the same candles.
    from candidate_pipeline import CandidatePipeline
    print("=== BASELINE: existing single-leg sleeves (same 7 sessions) ===")
    for key in ("orb", "orb_smt", "orb_retest"):
        count = 0
        days = set()
        for day in ordered_dates:
            day_candles = by_date[day]
            for i in range(len(day_candles)):
                got = CandidatePipeline.evaluate_strategy_signals_pure(
                    day_candles, key, underlying_symbol="NIFTY",
                    contract_specs=specs, index=i)
                if got:
                    count += 1
                    days.add(day)
        print(f"  {key:<14} raw signals={count:<4} days with signal={len(days)}")

    print()
    print("NOTE: 'modeled ITM' is an underlying-move test, not option P&L. "
          "The source modules intentionally report NOT_READY_FOR_OPTIONS_PNL "
          "without validated bid/ask option history.")


if __name__ == "__main__":
    main()
