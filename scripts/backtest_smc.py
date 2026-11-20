"""Backtest for the SMC liquidity-sweep strategy on NIFTY and BANKNIFTY.

Execution model
---------------
* The retest entry is a **limit at the FVG level** (``--fill limit``).  The
  setup is known at the previous close, the trigger bar trades into the gap,
  and the fill is the gap level.  ``--fill next_open`` is kept for comparison
  and for the MSS-only diagnostic mode, where the entry is a market entry.
* Entry premium must clear ``--min-premium`` (default Rs 5).  Below that the
  one-tick slippage is a large share of the premium and penny contracts
  dominate the P&L - the same tradeability floor the active stack uses.
* ``--moneyness-steps`` buys ITM options (CE strike below spot, PE above) to
  cut tick cost as a share of premium.

Two exit models are reported:

* ``underlying`` (primary) - the strategy's own stop/target: stop beyond the
  sweep extreme plus a 0.25 ATR buffer, target at the configured RR.  The
  option premium is marked from the underlying price at the exit bar, so P&L
  is option P&L, not index points.
* ``premium`` (comparison) - the house model: -20% / +25% on the option
  premium, hard flat 15:10.  Kept so this sleeve can be compared with the
  existing ORB / retest / mean-reversion evidence.

A matched **random-entry control** is also run: same instruments, same time
window, same risk geometry, same caps, random direction.  If the SMC
selection does not beat it, the pattern is not doing any work.

    python scripts/backtest_smc.py
    python scripts/backtest_smc.py --rr 3 --moneyness-steps 1
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest_active_wide import nfo_universe, to_candles  # noqa: E402
from scripts.backtest_orb_retest_v2 import (  # noqa: E402
    HARD_FLAT,
    IST,
    STOP_FRACTION,
    TARGET_FRACTION,
    lots_for,
    option_costs,
    premium,
    print_table,
    stats,
)
from strategies.smc_liquidity_strategy import SmcLiquidityStrategy  # noqa: E402

DATA = ROOT / "output" / "research_5m_wide"
SYMBOLS = ("NIFTY", "BANKNIFTY")
MAX_TRADES_PER_DAY = 2
PREMIUM_TARGET = 0.25  # launcher value


# ---------------------------------------------------------------------------
# Trade simulation
# ---------------------------------------------------------------------------

def simulate(candles: List[Dict[str, Any]], entries: Dict[int, Dict[str, Any]],
             meta: Dict[str, Any], *, exit_mode: str = "underlying",
             max_trades_per_day: int = MAX_TRADES_PER_DAY,
             target_fraction: float = PREMIUM_TARGET,
             fill: str = "limit",
             moneyness_steps: int = 0,
             min_premium: float = 0.0) -> List[Dict[str, Any]]:
    """Simulate one entry stream with one open position per instrument.

    ``fill`` controls how a signal becomes a position:

    * ``limit`` (default) - the retest strategy rests a limit at the FVG
      level.  The setup is known at the previous close, the trigger bar
      touches the level, and the fill is the level itself.  Using the next
      bar's open instead books the entry *after* the bounce, which is not
      what the strategy specifies and systematically destroys the 1:2
      geometry.
    * ``next_open`` - market-on-open after the signal bar.  This is the right
      model for the MSS-only diagnostic mode, where the entry is a market
      entry at the displacement close.
    """
    by_date: Dict[str, List[int]] = defaultdict(list)
    for i, candle in enumerate(candles):
        by_date[candle["timestamp"][:10]].append(i)

    lot, step, vol = meta["lot"], meta["step"], meta["vol"]
    trades: List[Dict[str, Any]] = []

    for day, indices in sorted(by_date.items()):
        open_trade: Optional[Dict[str, Any]] = None
        taken = 0
        for i in indices:
            if open_trade is not None and open_trade["exit_idx"] <= i:
                open_trade = None
            if open_trade is not None or taken >= max_trades_per_day:
                continue
            payload = entries.get(i)
            if not payload:
                continue
            if fill == "limit":
                spot = float(payload.get("entry_price") or 0.0)
                bar = candles[i]
                if spot <= 0:
                    continue
                if payload["variant"] == "bullish":
                    if float(bar["low"]) > spot:
                        continue
                elif float(bar["high"]) < spot:
                    continue
                entry_idx, entry_ts, first_exit = i, bar["timestamp"], i + 1
            else:
                if i + 1 >= len(candles) or candles[i + 1]["timestamp"][:10] != day:
                    continue
                entry_bar = candles[i + 1]
                spot = float(entry_bar["open"])
                if spot <= 0:
                    continue
                entry_idx, entry_ts = i + 1, entry_bar["timestamp"]
                first_exit = i + 2
            strike = round(spot / step) * step
            kind = payload["option_type"]
            # ``moneyness_steps`` > 0 buys in-the-money options: a CE strike
            # below spot, a PE strike above it.  ITM options carry more delta
            # and less theta per rupee of premium, which matters when the
            # edge is thin relative to costs.
            if moneyness_steps:
                strike += (-moneyness_steps if kind == "CE" else moneyness_steps) * step
            if strike <= 0:
                continue
            # The structural stop comes from the sweep extreme.  The 1:2
            # target is recomputed from the *actual* fill so the realised
            # reward/risk matches the specification (with a limit fill this
            # is exactly the stored target).
            stop = float(payload["stop_loss"])
            rr = float(payload.get("risk_reward", 2.0))
            if payload["variant"] == "bullish":
                if spot <= stop:
                    continue
                target = spot + rr * (spot - stop)
            else:
                if spot >= stop:
                    continue
                target = spot - rr * (stop - spot)
            entry_prem = premium(spot, strike, kind, vol)
            if entry_prem < min_premium:
                continue
            lots = lots_for(entry_prem, lot)
            if entry_prem <= 0 or lots <= 0:
                continue
            qty = lot * lots
            prem_stop = entry_prem * (1.0 - STOP_FRACTION)
            prem_target = entry_prem * (1.0 + target_fraction)

            exit_spot = spot
            exit_prem = entry_prem
            reason = "eod"
            exit_idx, exit_ts = entry_idx, entry_ts

            for j in range(first_exit, len(candles)):
                bar = candles[j]
                if bar["timestamp"][:10] != day:
                    break
                if pd.Timestamp(bar["timestamp"]).time() > HARD_FLAT:
                    break
                low, high = float(bar["low"]), float(bar["high"])
                close_px = premium(float(bar["close"]), strike, kind, vol)
                exit_idx, exit_ts = j, bar["timestamp"]

                if exit_mode == "underlying":
                    # stop first when a bar spans both sides
                    if (payload["variant"] == "bullish" and low <= stop) or (
                            payload["variant"] == "bearish" and high >= stop):
                        exit_spot = stop
                        reason = "stop"
                        break
                    if (payload["variant"] == "bullish" and high >= target) or (
                            payload["variant"] == "bearish" and low <= target):
                        exit_spot = target
                        reason = "target"
                        break
                    exit_spot = float(bar["close"])
                    exit_prem = close_px
                else:
                    exit_prem = close_px
                    exit_spot = float(bar["close"])
                    if close_px <= prem_stop:
                        reason = "stop"
                        break
                    if close_px >= prem_target:
                        reason = "target"
                        break

            if exit_mode == "underlying":
                exit_prem = premium(exit_spot, strike, kind, vol)

            gross = (exit_prem - entry_prem) * qty
            costs = option_costs(entry_prem, exit_prem, lot, lots)
            trades.append({
                "day": day,
                "symbol": meta["name"],
                "kind": kind,
                "variant": payload["variant"],
                "entry_ts": entry_ts,
                "exit_ts": exit_ts,
                "entry_idx": entry_idx,
                "exit_idx": exit_idx,
                "entry_spot": round(spot, 2),
                "exit_spot": round(exit_spot, 2),
                "stop": round(stop, 2),
                "target": round(target, 2),
                "entry_premium": round(entry_prem, 2),
                "exit_premium": round(exit_prem, 2),
                "strike": strike,
                "lots": lots,
                "qty": qty,
                "reason": reason,
                "gross_pnl": round(gross, 2),
                "costs": round(costs, 2),
                "pnl": round(gross - costs, 2),
            })
            open_trade = trades[-1]
            taken += 1
    return trades


# ---------------------------------------------------------------------------
# Random-entry control
# ---------------------------------------------------------------------------

def random_entries(candles: List[Dict[str, Any]], count: int,
                   rr: float, rng: random.Random,
                   risk_points: float) -> Dict[int, Dict[str, Any]]:
    """Random entries with the same time window, direction mix and 1:2 geometry."""
    window = [(i, c["timestamp"]) for i, c in enumerate(candles)
              if "09:45" <= c["timestamp"][11:16] <= "14:30"]
    if not window or count <= 0:
        return {}
    picks = rng.sample(window, min(count, len(window)))
    out: Dict[int, Dict[str, Any]] = {}
    for i, _ts in picks:
        close = float(candles[i]["close"])
        if rng.random() < 0.5:
            variant, kind = "bullish", "CE"
            stop = close - risk_points
            target = close + rr * risk_points
        else:
            variant, kind = "bearish", "PE"
            stop = close + risk_points
            target = close - rr * risk_points
        out[i] = {"variant": variant, "option_type": kind,
                  "stop_loss": stop, "target": target}
    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def month_rows(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_month: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        by_month[trade["day"][:7]].append(trade)
    return [stats(group, month) for month, group in sorted(by_month.items())]


def symbol_rows(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        by_symbol[trade["symbol"]].append(trade)
    return [stats(group, symbol) for symbol, group in sorted(by_symbol.items())]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rr", type=float, default=2.0)
    parser.add_argument("--no-bias", action="store_true")
    parser.add_argument("--fill", choices=("limit", "next_open"), default="limit",
                        help="limit = rest at the FVG level (retest fill); "
                             "next_open = market on the following bar")
    parser.add_argument("--symbols", default=",".join(SYMBOLS),
                        help="comma-separated index symbols from the 5m cache")
    parser.add_argument("--min-premium", type=float, default=5.0,
                        help="tradeability floor on entry premium (Rs); the "
                             "active stack uses 5 to skip penny contracts")
    parser.add_argument("--moneyness-steps", type=int, default=0,
                        help=">0 buys ITM options, cutting tick cost as a "
                             "share of premium")
    parser.add_argument("--random-runs", type=int, default=200)
    parser.add_argument("--json", type=Path,
                        default=ROOT / "output" / "smc_backtest.json")
    args = parser.parse_args(argv)
    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())

    universe = nfo_universe()
    results: Dict[str, Any] = {"symbols": {}, "combined": {}}
    all_underlying: List[Dict[str, Any]] = []
    all_premium: List[Dict[str, Any]] = []
    funnel_total: Counter = Counter()

    for name in symbols:
        meta = dict(universe[name])
        meta["name"] = name
        frame = pd.read_pickle(DATA / f"{name}.pkl")
        candles = to_candles(frame, name, meta["kind"])
        strategy = SmcLiquidityStrategy(rr_target=args.rr,
                                        require_bias=not args.no_bias)
        signals, funnel = strategy.scan_signals(candles, name)
        funnel_total.update(funnel)

        under = simulate(candles, signals, meta, exit_mode="underlying",
                         max_trades_per_day=MAX_TRADES_PER_DAY, fill=args.fill,
                         moneyness_steps=args.moneyness_steps,
                         min_premium=args.min_premium)
        prem = simulate(candles, signals, meta, exit_mode="premium",
                        max_trades_per_day=MAX_TRADES_PER_DAY, fill=args.fill,
                        moneyness_steps=args.moneyness_steps,
                        min_premium=args.min_premium)
        all_underlying.extend(under)
        all_premium.extend(prem)

        days = len({c["timestamp"][:10] for c in candles})
        print(f"\n{name}: {len(candles)} bars, {days} sessions, "
              f"{len(signals)} entry triggers")
        print("  funnel: " + "  ".join(f"{k}={v}" for k, v in funnel.items()))
        print(f"  underlying exits: {len(under)} trades, "
              f"net Rs {sum(t['pnl'] for t in under):,.0f}")
        print(f"  premium exits:    {len(prem)} trades, "
              f"net Rs {sum(t['pnl'] for t in prem):,.0f}")
        print(f"  exit mix: {dict(Counter(t['reason'] for t in under))}")

        results["symbols"][name] = {
            "bars": len(candles), "sessions": days, "funnel": funnel,
            "underlying": {"stats": stats(under, name), "by_month": month_rows(under),
                           "trades": under},
            "premium": {"stats": stats(prem, name), "by_month": month_rows(prem),
                        "trades": prem},
        }

    print_table([stats(all_underlying, "SMC - underlying stop/target"),
                 stats(all_premium, "SMC - premium -20/+25")],
                f"SMC LIQUIDITY SWEEP - NIFTY + BANKNIFTY ({args.fill} fill)")

    by_symbol = symbol_rows(all_underlying)
    print_table(by_symbol, "SMC - BY INDEX (underlying exits)")

    months = month_rows(all_underlying)
    print_table(months, "SMC - MONTH-WISE (underlying exits)")
    print_table(month_rows(all_premium), "SMC - MONTH-WISE (premium exits)")

    # Matched random control, same number of trades and same risk geometry.
    risk_points = 0.0
    if all_underlying:
        stops = [abs(t["entry_spot"] - t["exit_spot"])
                 for t in all_underlying if t["reason"] == "stop"]
        if stops:
            risk_points = float(pd.Series(stops).median())

    rng = random.Random(20261111)
    random_nets: List[float] = []
    random_wins: List[float] = []
    per_symbol_entries = {
        name: results["symbols"][name]["funnel"]["entries"] for name in symbols
    }
    cached: Dict[str, Tuple[Dict[str, Any], List[Dict[str, Any]]]] = {}
    for name in symbols:
        meta = dict(universe[name])
        meta["name"] = name
        cached[name] = (meta, to_candles(pd.read_pickle(DATA / f"{name}.pkl"),
                                         name, meta["kind"]))
    for _ in range(args.random_runs):
        run_trades: List[Dict[str, Any]] = []
        for name in symbols:
            meta, candles = cached[name]
            fallback_risk = float(pd.Series(
                [float(c["high"]) - float(c["low"]) for c in candles[:200]]).median() or 20.0)
            entries = random_entries(
                candles, per_symbol_entries.get(name, 0), args.rr, rng,
                risk_points if risk_points > 0 else fallback_risk)
            run_trades.extend(simulate(candles, entries, meta,
                                       exit_mode="underlying",
                                       max_trades_per_day=MAX_TRADES_PER_DAY,
                                       fill="next_open",
                                       moneyness_steps=args.moneyness_steps,
                                       min_premium=args.min_premium))
        net = sum(t["pnl"] for t in run_trades)
        random_nets.append(net)
        wins = sum(1 for t in run_trades if t["pnl"] > 0)
        random_wins.append(100.0 * wins / max(1, len(run_trades)))

    random_nets.sort()
    real_net = sum(t["pnl"] for t in all_underlying)
    percentile = (sum(1 for n in random_nets if n < real_net)
                  / max(1, len(random_nets))) * 100.0
    print(f"\nRANDOM-ENTRY CONTROL ({args.random_runs} runs, "
          f"{per_symbol_entries} entries/index, risk {risk_points:.1f} pts)")
    print("-" * 96)
    print(f"  real SMC net          : {real_net:>12,.0f}")
    print(f"  random net median     : {random_nets[len(random_nets) // 2]:>12,.0f}")
    print(f"  random net p5 / p95   : {random_nets[len(random_nets) // 20]:>12,.0f}"
          f" / {random_nets[-len(random_nets) // 20]:>10,.0f}")
    print(f"  real percentile       : {percentile:>11.1f}%")
    print(f"  random win rate median: "
          f"{sorted(random_wins)[len(random_wins) // 2]:>11.1f}%")
    print(f"  real win rate         : "
          f"{100.0 * sum(1 for t in all_underlying if t['pnl'] > 0) / max(1, len(all_underlying)):>11.1f}%")
    print("-" * 96)

    results["combined"] = {
        "fill": args.fill,
        "stats_underlying": stats(all_underlying, "smc_underlying"),
        "stats_premium": stats(all_premium, "smc_premium"),
        "by_symbol": by_symbol,
        "by_month_underlying": months,
        "by_month_premium": month_rows(all_premium),
        "funnel": dict(funnel_total),
        "trades_underlying": all_underlying,
        "trades_premium": all_premium,
        "random_control": {
            "runs": args.random_runs,
            "real_net": real_net,
            "median": random_nets[len(random_nets) // 2],
            "p5": random_nets[len(random_nets) // 20],
            "p95": random_nets[-len(random_nets) // 20],
            "real_percentile": percentile,
            "risk_points": risk_points,
        },
    }
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(results, indent=2, default=str),
                         encoding="utf-8")
    print(f"\nSaved: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
