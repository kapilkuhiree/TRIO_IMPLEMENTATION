"""Wide NFO options backtest: daily (end-of-day) strategies on 221 underlyings.

The spot QQQ-rule screen answered "does the signal work on the underlying?".
This script answers the question that actually matters for an options book:
*does the trade make money after premium, theta, skew and Indian option costs?*

No historical option chain exists in the repo, so every contract is priced with
Black-Scholes off a per-name implied vol built from that name's trailing 20-day
realised vol:

    IV_atm = max(RV20, 0.12) * vrp
    IV(K)  = IV_atm + skew * otm_distance_pct        (puts only)

``vrp`` is the volatility risk premium.  It is the single assumption the whole
result rests on, so it is a CLI flag and ``--vrp 1.0`` is the fair-pricing
control: at 1.0 a short-premium book must lose exactly its costs.  Anything
above 1.0 is the premium a real chain charges over subsequent realised vol.

Strategies (``--variant``):

  short_put        sell a ~0.30-delta put, 30 DTE, monthly roll
  bull_put_spread  sell 0.30-delta put / buy 0.15-delta put (defined risk)
  short_strangle   sell 0.25-delta put + 0.25-delta call
  covered_call     long the stock + sell a 0.30-delta call
  long_call        the QQQ mean-reversion rule bought as an ATM 30-DTE call

Every position is marked to the session close, so the output carries true
day-wise P&L, not just trade P&L.  Each symbol is an independent Rs 10L sleeve
(lots rounded to the real NFO lot size), so the aggregate is a per-candidate
screen, not a capital-constrained portfolio.

    python scripts/backtest_nfo_options_wide.py --variant short_put
    python scripts/backtest_nfo_options_wide.py --variant short_put --vrp 1.0
    python scripts/backtest_nfo_options_wide.py --variant bull_put_spread --since 2024-01-01

DISCLAIMER: Educational purposes only. Not financial advice.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest_orb_retest_v2 import option_costs  # noqa: E402
from scripts.backtest_qqq_mean_reversion import build_signals  # noqa: E402
from scripts.backtest_qqq_rules_wide import (  # noqa: E402
    DATA_DIR,
    load_bars,
    load_universe,
    split_segments,
)
from src.options_pricing import bs_price  # noqa: E402

VARIANTS = ("short_put", "bull_put_spread", "short_strangle", "covered_call",
            "long_call")

DEFAULT_JSON = ROOT / "output" / "nfo_options_wide.json"
DEFAULT_CANDIDATES = ROOT / "output" / "nfo_options_wide_candidates.csv"
DEFAULT_MONTHS = ROOT / "output" / "nfo_options_wide_months.csv"
DEFAULT_DAYWISE = ROOT / "output" / "nfo_options_wide_daywise.csv"

DEFAULT_NOTIONAL = 1_000_000.0
DEFAULT_DTE = 30.0
DEFAULT_VRP = 1.15
DEFAULT_SKEW = 0.006         # vol points per 1% out-of-the-money (0.6 per 1%)
DEFAULT_PROFIT_TARGET = 0.50  # buy back at 50% of credit
DEFAULT_STOP = 2.0            # stop when liability reaches 2x credit
DEFAULT_SPREAD_PCT = 0.0025   # extra half-spread per side, fraction of premium
DEFAULT_RF = 0.065


# ---------------------------------------------------------------------------
# Volatility model
# ---------------------------------------------------------------------------

def realized_vol(close: pd.Series, window: int = 20) -> pd.Series:
    ret = np.log(close / close.shift(1))
    return (ret.rolling(window).std() * math.sqrt(252.0)).replace(
        [np.inf, -np.inf], np.nan)


def entry_iv(rv: float, vrp: float) -> float:
    base = max(float(rv), 0.12) if np.isfinite(rv) else 0.25
    return float(min(base * vrp, 3.0))


def leg_iv(atm: float, spot: float, strike: float, kind: str,
           skew: float) -> float:
    """Add put skew for strikes below spot; leave calls at the ATM level.

    ``skew`` is absolute vol per 1% out-of-the-money, e.g. 0.006 means a
    strike 10% below spot is priced 6 vol points above the ATM level.
    """
    if kind == "PE" and spot > 0 and strike < spot:
        otm = (spot - strike) / spot * 100.0
        return float(min(atm + skew * otm, 2.5))
    return float(min(atm, 2.5))


# ---------------------------------------------------------------------------
# Strike selection
# ---------------------------------------------------------------------------

def _delta(spot: float, strike: float, t: float, sigma: float,
           kind: str) -> float:
    if t <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + (DEFAULT_RF + 0.5 * sigma * sigma) * t) \
        / (sigma * math.sqrt(t))
    cdf = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
    return cdf if kind == "CE" else cdf - 1.0


def strike_for_delta(spot: float, t: float, sigma: float, kind: str,
                     target: float, step: float) -> float:
    """Bisect the strike that gives ``target`` absolute delta, then snap."""
    if kind == "PE":
        lo, hi = spot * 0.30, spot * 1.00
    else:
        lo, hi = spot * 1.00, spot * 1.60
    for _ in range(48):
        mid = 0.5 * (lo + hi)
        delta = abs(_delta(spot, mid, t, sigma, kind))
        if kind == "PE":
            # deeper strike -> bigger |delta|
            if delta < target:
                lo = mid
            else:
                hi = mid
        else:
            # higher strike -> smaller delta
            if delta > target:
                lo = mid
            else:
                hi = mid
    strike = 0.5 * (lo + hi)
    if step > 0:
        strike = round(strike / step) * step
    return float(max(strike, step if step > 0 else 1.0))


# ---------------------------------------------------------------------------
# Position definition
# ---------------------------------------------------------------------------

def build_legs(variant: str, spot: float, t: float, atm_iv_value: float,
               step: float, skew: float, put_delta: float = 0.30,
               call_delta: float = 0.30,
               wing_delta: float = 0.15) -> List[Dict[str, Any]]:
    """Return legs as {kind, strike, direction, iv}. direction +1 = short."""
    def make(kind: str, delta: float, direction: int) -> Dict[str, Any]:
        strike = strike_for_delta(spot, t, atm_iv_value, kind, delta, step)
        return {"kind": kind, "strike": strike, "direction": direction,
                "iv": leg_iv(atm_iv_value, spot, strike, kind, skew)}

    if variant == "short_put":
        return [make("PE", put_delta, +1)]
    if variant == "bull_put_spread":
        return [make("PE", put_delta, +1), make("PE", wing_delta, -1)]
    if variant == "short_strangle":
        return [make("PE", put_delta, +1), make("CE", call_delta, +1)]
    if variant == "covered_call":
        return [make("CE", call_delta, +1)]
    if variant == "long_call":
        return [make("CE", 0.50, -1)]
    raise ValueError(f"unknown variant {variant!r}")


def leg_price(leg: Dict[str, Any], spot: float, t: float) -> float:
    if t <= 0:
        if leg["kind"] == "CE":
            return max(spot - leg["strike"], 0.0)
        return max(leg["strike"] - spot, 0.0)
    return bs_price(spot, leg["strike"], t, leg["iv"], leg["kind"],
                    r=DEFAULT_RF)


def net_value(legs: List[Dict[str, Any]], spot: float, t: float) -> float:
    return sum(leg["direction"] * leg_price(leg, spot, t) for leg in legs)


def leg_cost(direction: int, entry_prem: float, exit_prem: float,
             lot: int, lots: int) -> float:
    """Repo cost model. A short leg sells at entry, so swap buy/sell values."""
    if direction > 0:
        return option_costs(exit_prem, entry_prem, lot, lots)
    return option_costs(entry_prem, exit_prem, lot, lots)


def spread_cost(legs: List[Dict[str, Any]], entry_px: List[float],
                exit_px: List[float], lot: int, lots: int,
                spread_pct: float) -> float:
    total = 0.0
    for leg, e_px, x_px in zip(legs, entry_px, exit_px):
        total += leg_cost(leg["direction"], e_px, x_px, lot, lots)
        # Extra half-spread each side on top of the 1-tick model.
        total += spread_pct * (abs(e_px) + abs(x_px)) * lot * lots
    return total


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

def _prepare(bars: pd.DataFrame, is_index: bool) -> pd.DataFrame:
    bars = bars.copy()
    if not is_index:
        factor = (bars["Adj Close"] / bars["Close"]).replace(
            [np.inf, -np.inf], np.nan).ffill().fillna(1.0)
        for column in ("Open", "High", "Low", "Close"):
            bars[column] = bars[column] * factor
    return bars


def simulate_symbol(name: str, meta: Dict[str, Any], bars: pd.DataFrame,
                    *, variant: str, notional: float, dte: float,
                    vrp: float, skew: float, profit_target: float,
                    stop: float, spread_pct: float,
                    since: Optional[str], until: Optional[str],
                    monthly: bool = True,
                    trend_filter: bool = False,
                    put_delta: float = 0.30,
                    call_delta: float = 0.30,
                    wing_delta: float = 0.15) -> List[Dict[str, Any]]:
    is_index = meta.get("kind") == "index"
    lot = int(meta.get("lot") or 0)
    step = float(meta.get("step") or 0.0)
    if lot <= 0:
        return []

    trades: List[Dict[str, Any]] = []
    for segment in split_segments(_prepare(bars, is_index)):
        if len(segment) < 90:
            continue
        segment = segment.reset_index()
        date_col = "Date" if "Date" in segment.columns else segment.columns[0]
        dates = pd.to_datetime(segment[date_col])
        opens = segment["Open"].to_numpy(dtype=float)
        closes = segment["Close"].to_numpy(dtype=float)
        rv = realized_vol(segment["Close"], 20).to_numpy(dtype=float)
        sma200 = segment["Close"].rolling(200).mean().to_numpy(dtype=float)
        n = len(segment)
        if n < 90:
            continue

        if variant == "long_call":
            signals = build_signals(segment.set_index(date_col))
            entry_flags = signals["entry"].to_numpy(dtype=bool)
            exit_flags = signals["exit"].to_numpy(dtype=bool)
            candidates = [i for i in range(60, n - 1) if entry_flags[i]]
        else:
            exit_flags = np.zeros(n, dtype=bool)
            candidates = []
            for i in range(60, n - 1):
                if i == 0:
                    continue
                if not np.isfinite(rv[i]):
                    continue
                if trend_filter and (not np.isfinite(sma200[i])
                                     or closes[i] < sma200[i]):
                    continue
                if monthly:
                    # first session of a calendar month
                    if dates.iloc[i].month == dates.iloc[i - 1].month:
                        continue
                candidates.append(i)

        blocked_until = 0
        for i in candidates:
            entry_idx = i + 1
            if entry_idx >= n or entry_idx < blocked_until:
                continue
            entry_date = dates.iloc[entry_idx]
            if since and entry_date < pd.Timestamp(since):
                continue
            if until and entry_date > pd.Timestamp(until):
                continue

            spot0 = float(opens[entry_idx])
            if not np.isfinite(spot0) or spot0 < 5.0:
                continue
            # Today's lot/step metadata does not describe a pre-split price.
            if step > 0 and spot0 / step < 2.0:
                continue
            atm = entry_iv(float(rv[i]), vrp)
            t0 = dte / 365.0
            legs = build_legs(variant, spot0, t0, atm, step, skew,
                              put_delta=put_delta, call_delta=call_delta,
                              wing_delta=wing_delta)
            if any(not (0.5 * spot0 <= leg["strike"] <= 1.6 * spot0)
                   for leg in legs):
                continue
            if variant == "bull_put_spread" and \
                    abs(legs[0]["strike"] - legs[1]["strike"]) <= 0:
                continue
            entry_px = [leg_price(leg, spot0, t0) for leg in legs]
            credit = sum(leg["direction"] * px
                         for leg, px in zip(legs, entry_px))
            if variant == "long_call":
                if credit >= 0:
                    continue
            elif credit <= 0:
                continue

            lots = max(1, int(round(notional / (spot0 * lot))))
            qty = lots * lot

            if variant == "bull_put_spread":
                width = abs(legs[0]["strike"] - legs[1]["strike"])
                margin = max(width * qty, 1.0)
            elif variant == "covered_call":
                margin = spot0 * qty
            elif variant == "long_call":
                margin = max(abs(credit) * qty, 1.0)
            elif variant == "short_strangle":
                margin = 0.15 * spot0 * qty + max(credit, 0.0) * qty
            else:
                margin = 0.12 * spot0 * qty + max(credit, 0.0) * qty

            exit_idx = n - 1
            exit_reason = "expiry"
            exit_px = entry_px
            daily: List[Tuple[str, float]] = []
            prev_equity = 0.0

            for j in range(entry_idx, n):
                held_days = (dates.iloc[j] - entry_date).days
                remaining = dte - held_days
                t = max(remaining, 1.0) / 365.0
                spot = float(closes[j])
                if not np.isfinite(spot) or spot <= 0:
                    continue

                value = net_value(legs, spot, t)
                if variant == "covered_call":
                    equity = (spot - spot0) + (credit - value)
                else:
                    equity = credit - value
                daily.append((dates.iloc[j].date().isoformat(),
                              (equity - prev_equity) * qty))
                prev_equity = equity

                if variant == "long_call":
                    if exit_flags[j] and j > entry_idx:
                        exit_idx, exit_reason = j, "rsi_exit"
                        exit_px = [leg_price(leg, spot, t) for leg in legs]
                        break
                else:
                    if value >= stop * credit and j > entry_idx:
                        exit_idx, exit_reason = j, "stop"
                        exit_px = [leg_price(leg, spot, t) for leg in legs]
                        break
                    if profit_target > 0 and \
                            value <= profit_target * credit and j > entry_idx:
                        exit_idx, exit_reason = j, "target"
                        exit_px = [leg_price(leg, spot, t) for leg in legs]
                        break
                if remaining <= 1.0:
                    exit_idx, exit_reason = j, "expiry"
                    exit_px = [leg_price(leg, spot, t) for leg in legs]
                    break

            # Settle at the final mark.
            final_spot = float(closes[exit_idx])
            held_days = (dates.iloc[exit_idx] - entry_date).days
            t_final = max(dte - held_days, 1.0) / 365.0
            exit_px = [leg_price(leg, final_spot, t_final) for leg in legs]
            exit_credit = sum(leg["direction"] * px
                              for leg, px in zip(legs, exit_px))
            costs = spread_cost(legs, entry_px, exit_px, lot, lots, spread_pct)

            if variant == "covered_call":
                gross = ((final_spot - spot0) + (credit - exit_credit)) * qty
            else:
                gross = (credit - exit_credit) * qty
            pnl = gross - costs

            # Make the daily series sum exactly to net P&L.
            if daily:
                drift = pnl - sum(step_pnl for _, step_pnl in daily)
                last_date, last_pnl = daily[-1]
                daily[-1] = (last_date, last_pnl + drift)
            else:
                daily = [(dates.iloc[exit_idx].date().isoformat(), pnl)]

            trades.append({
                "symbol": name,
                "variant": variant,
                "entry_date": entry_date.date().isoformat(),
                "exit_date": dates.iloc[exit_idx].date().isoformat(),
                "bars": int(exit_idx - entry_idx + 1),
                "entry_spot": round(spot0, 2),
                "exit_spot": round(final_spot, 2),
                "lot": lot,
                "lots": lots,
                "qty": qty,
                "iv": round(atm, 4),
                "credit": round(credit * qty, 2),
                "margin": round(margin, 2),
                "costs": round(costs, 2),
                "pnl": round(pnl, 2),
                "ret_pct": round(pnl / margin * 100.0, 2) if margin else 0.0,
                "exit_reason": exit_reason,
                "legs": [{"kind": leg["kind"], "strike": leg["strike"],
                          "direction": leg["direction"],
                          "iv": round(leg["iv"], 4),
                          "entry": round(px, 2)}
                         for leg, px in zip(legs, entry_px)],
                "daily": daily,
            })
            blocked_until = exit_idx + 1
    return trades


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def summarize_trades(trades: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    if not trades:
        return {"trades": 0}
    pnl = np.array([t["pnl"] for t in trades], dtype=float)
    margin = np.array([t["margin"] for t in trades], dtype=float)
    wins = pnl > 0
    gross_profit = float(pnl[pnl > 0].sum())
    gross_loss = float(-pnl[pnl < 0].sum())
    ordered = sorted(trades, key=lambda t: t["exit_date"])
    equity = np.cumsum([t["pnl"] for t in ordered])
    peak = np.maximum.accumulate(np.concatenate([[0.0], equity]))[1:]
    dd = equity - peak
    return {
        "trades": int(len(trades)),
        "wins": int(wins.sum()),
        "win_rate": round(float(wins.mean() * 100.0), 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(gross_profit / gross_loss, 3)
        if gross_loss > 0 else float("inf"),
        "net_pnl": round(float(pnl.sum()), 2),
        "avg_pnl": round(float(pnl.mean()), 2),
        "avg_ret_pct": round(float(pnl.sum() / margin.sum() * 100.0), 3)
        if margin.sum() > 0 else 0.0,
        "mean_trade_ret_pct": round(float((pnl / np.where(
            margin > 0, margin, 1)).mean() * 100.0), 3),
        "max_drawdown": round(float(dd.min()), 2) if len(dd) else 0.0,
        "margin": round(float(margin.sum()), 2),
        "avg_bars": round(float(np.mean([t["bars"] for t in trades])), 2),
        "exit_reasons": {k: int(sum(1 for t in trades if t["exit_reason"] == k))
                         for k in {t["exit_reason"] for t in trades}},
    }


def month_table(trades: Sequence[Dict[str, Any]]) -> pd.DataFrame:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        grouped[trade["exit_date"][:7]].append(trade)
    rows = []
    for month in sorted(grouped):
        book = grouped[month]
        stats = summarize_trades(book)
        rows.append({"month": month, "trades": stats["trades"],
                     "win_rate": stats["win_rate"],
                     "net_pnl": stats["net_pnl"]})
    return pd.DataFrame(rows)


def symbol_table(trades: Sequence[Dict[str, Any]]) -> pd.DataFrame:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        grouped[trade["symbol"]].append(trade)
    rows = []
    for name in sorted(grouped):
        stats = summarize_trades(grouped[name])
        rows.append({"symbol": name, "trades": stats["trades"],
                     "win_rate": stats["win_rate"],
                     "profit_factor": stats["profit_factor"],
                     "avg_ret_pct": stats["avg_ret_pct"],
                     "net_pnl": stats["net_pnl"],
                     "max_drawdown": stats["max_drawdown"]})
    return pd.DataFrame(rows).sort_values("net_pnl", ascending=False)


def daywise_table(trades: Sequence[Dict[str, Any]]) -> pd.DataFrame:
    per_day: Dict[str, float] = defaultdict(float)
    for trade in trades:
        for day, pnl in trade["daily"]:
            per_day[day] += pnl
    frame = pd.DataFrame({"date": sorted(per_day),
                          "pnl": [per_day[d] for d in sorted(per_day)]})
    if frame.empty:
        return frame
    frame["equity"] = frame["pnl"].cumsum()
    return frame


def _fmt_pf(value: float) -> str:
    return "inf" if value == float("inf") else f"{value:.2f}"


def print_report(*, trades: List[Dict[str, Any]], variant: str,
                 notional: float, dte: float, vrp: float, skew: float,
                 since: Optional[str], until: Optional[str]) -> None:
    stats = summarize_trades(trades)
    print(f"NFO OPTIONS WIDE | {variant} | {dte:.0f} DTE | vrp {vrp:.2f} | "
          f"skew {skew:.2f} | notional Rs {notional:,.0f}/leg")
    print("=" * 118)
    if not stats["trades"]:
        print("no trades")
        return
    print(f"Trades {stats['trades']:,} | Win {stats['win_rate']:.1f}% | "
          f"PF {_fmt_pf(stats['profit_factor'])} | Net Rs "
          f"{stats['net_pnl']:,.0f} | Avg Rs {stats['avg_pnl']:,.0f} "
          f"({stats['avg_ret_pct']:+.2f}% margin) | DD Rs "
          f"{stats['max_drawdown']:,.0f} | Avg bars {stats['avg_bars']:.1f}")
    print(f"Exits: {stats['exit_reasons']}")

    months = month_table(trades)
    if not months.empty:
        pos = int((months["net_pnl"] > 0).sum())
        print(f"\nMONTHS: {len(months)} | positive {pos} "
              f"({pos / len(months) * 100:.1f}%) | best "
              f"Rs {months['net_pnl'].max():,.0f} | worst "
              f"Rs {months['net_pnl'].min():,.0f}")

    daywise = daywise_table(trades)
    if not daywise.empty:
        pos_days = int((daywise["pnl"] > 0).sum())
        print(f"DAYS  : {len(daywise):,} | positive {pos_days} "
              f"({pos_days / len(daywise) * 100:.1f}%) | best day "
              f"Rs {daywise['pnl'].max():,.0f} | worst day "
              f"Rs {daywise['pnl'].min():,.0f}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", default="short_put", choices=VARIANTS)
    parser.add_argument("--notional", type=float, default=DEFAULT_NOTIONAL)
    parser.add_argument("--dte", type=float, default=DEFAULT_DTE)
    parser.add_argument("--vrp", type=float, default=DEFAULT_VRP)
    parser.add_argument("--skew", type=float, default=DEFAULT_SKEW)
    parser.add_argument("--profit-target", type=float,
                        default=DEFAULT_PROFIT_TARGET)
    parser.add_argument("--stop", type=float, default=DEFAULT_STOP)
    parser.add_argument("--spread-pct", type=float, default=DEFAULT_SPREAD_PCT)
    parser.add_argument("--since", default=None)
    parser.add_argument("--until", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--trend-filter", action="store_true",
                        help="only sell premium when close is above its 200-SMA")
    parser.add_argument("--put-delta", type=float, default=0.30)
    parser.add_argument("--call-delta", type=float, default=0.30)
    parser.add_argument("--wing-delta", type=float, default=0.15)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--candidates", type=Path, default=None)
    parser.add_argument("--months", type=Path, default=None)
    parser.add_argument("--daywise", type=Path, default=None)
    args = parser.parse_args(argv)

    universe = load_universe()
    names = list(universe)
    if args.limit:
        names = names[:args.limit]

    all_trades: List[Dict[str, Any]] = []
    for index, name in enumerate(names, 1):
        meta = universe[name]
        path = DATA_DIR / f"{name.replace('&', 'AND').replace('/', '_')}.csv"
        if not path.exists():
            continue
        try:
            bars = load_bars(str(path), is_index=meta.get("kind") == "index")
        except Exception:  # noqa: BLE001
            continue
        all_trades.extend(simulate_symbol(
            name, meta, bars, variant=args.variant, notional=args.notional,
            dte=args.dte, vrp=args.vrp, skew=args.skew,
            profit_target=args.profit_target, stop=args.stop,
            spread_pct=args.spread_pct, since=args.since, until=args.until,
            trend_filter=args.trend_filter, put_delta=args.put_delta,
            call_delta=args.call_delta, wing_delta=args.wing_delta))
        if index % 40 == 0:
            print(f"  ...{index}/{len(names)} symbols, "
                  f"{len(all_trades):,} trades", flush=True)

    print_report(trades=all_trades, variant=args.variant,
                 notional=args.notional, dte=args.dte, vrp=args.vrp,
                 skew=args.skew, since=args.since, until=args.until)

    if not all_trades:
        return 1

    stats = summarize_trades(all_trades)
    months = month_table(all_trades)
    symbols = symbol_table(all_trades)
    daywise = daywise_table(all_trades)

    print("\nTOP / BOTTOM CANDIDATES (net Rs)")
    print("-" * 86)
    header = (f"{'Symbol':<14}{'Trades':>7}{'Win%':>7}{'PF':>7}"
              f"{'Avg%':>8}{'Net Rs':>15}{'MaxDD Rs':>14}")
    print(header)
    print("-" * len(header))
    for frame in (symbols.head(12), symbols.tail(8)):
        for row in frame.itertuples():
            print(f"{row.symbol:<14}{row.trades:>7}{row.win_rate:>7.1f}"
                  f"{_fmt_pf(row.profit_factor):>7}{row.avg_ret_pct:>8.2f}"
                  f"{row.net_pnl:>15,.0f}{row.max_drawdown:>14,.0f}")
        print("-" * len(header))

    profitable = int((symbols["net_pnl"] > 0).sum())
    print(f"\nPROFITABLE CANDIDATES: {profitable}/{len(symbols)} "
          f"({profitable / len(symbols) * 100:.1f}%)")

    print("\nMONTH-WISE NET P&L (last 24 months)")
    print("-" * 52)
    print(f"{'Month':<10}{'Trades':>8}{'Win%':>8}{'Net Rs':>16}")
    print("-" * 52)
    for row in months.tail(24).itertuples():
        print(f"{row.month:<10}{row.trades:>8}{row.win_rate:>8.1f}"
              f"{row.net_pnl:>16,.0f}")

    json_path = args.json or DEFAULT_JSON
    cand_path = args.candidates or DEFAULT_CANDIDATES
    month_path = args.months or DEFAULT_MONTHS
    day_path = args.daywise or DEFAULT_DAYWISE
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps({
        "config": {"variant": args.variant, "notional": args.notional,
                   "dte": args.dte, "vrp": args.vrp, "skew": args.skew,
                   "profit_target": args.profit_target, "stop": args.stop,
                   "spread_pct": args.spread_pct, "since": args.since,
                   "until": args.until, "trend_filter": args.trend_filter,
                   "put_delta": args.put_delta,
                   "call_delta": args.call_delta,
                   "wing_delta": args.wing_delta},
        "stats": stats,
        "months": months.to_dict(orient="records"),
        "candidates": symbols.to_dict(orient="records"),
        "daywise": daywise.to_dict(orient="records"),
        "trades": [{k: v for k, v in t.items() if k != "daily"}
                   for t in all_trades],
    }, indent=2, default=str), encoding="utf-8")
    symbols.to_csv(cand_path, index=False)
    months.to_csv(month_path, index=False)
    daywise.to_csv(day_path, index=False)
    print(f"\nSaved JSON: {json_path}")
    print(f"Saved CSV : {cand_path}")
    print(f"Saved CSV : {month_path}")
    print(f"Saved CSV : {day_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
