"""Indian-market version of the QQQ "today's trade + P&L" check.

Reports, for the NFO option universe (default: NIFTY):

* the indicator state for the last few sessions,
* whether the rule currently holds a position and its unrealised P&L,
* the last trades and this year's realised P&L,
* what a fresh signal has historically paid, conditioned on entry RSI.

Also supports a universe-wide sweep of open positions and of today's /
yesterday's entry signals, each sized at a fixed Rs 10,00,000 notional.

    python scripts/india_today_trade.py
    python scripts/india_today_trade.py --symbol BANKNIFTY
    python scripts/india_today_trade.py --open-positions
    python scripts/india_today_trade.py --signals

DISCLAIMER: Educational purposes only. Not financial advice.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest_qqq_mean_reversion import (  # noqa: E402
    Trade,
    build_signals,
    run_backtest,
    summarize,
)
from scripts.backtest_qqq_rules_wide import (  # noqa: E402
    DATA_DIR,
    load_bars,
    load_universe,
    split_segments,
)
from scripts.scan_qqq_rules_latest import prepare  # noqa: E402

DEFAULT_JSON = ROOT / "output" / "india_today_trade.json"
DEFAULT_NOTIONAL = 1_000_000.0


def _cache(name: str) -> Path:
    return DATA_DIR / f"{name.replace('&', 'AND').replace('/', '_')}.csv"


def analyse(name: str, meta: Dict[str, Any], *, notional: float) -> Dict[str, Any]:
    path = _cache(name)
    bars = load_bars(str(path), is_index=meta.get("kind") == "index")
    bars = prepare(bars, is_index=meta.get("kind") == "index")
    segment = split_segments(bars)[-1]
    signals = build_signals(segment)
    trades = run_backtest(segment, signals, exit_timing="next_open",
                          cost_bps=5.0, capital=notional)

    open_at: Optional[int] = None
    for position in range(len(segment)):
        if open_at is None and bool(signals["entry"].iloc[position]):
            open_at = position
        elif open_at is not None and bool(signals["exit"].iloc[position]):
            open_at = None

    position = None
    if open_at is not None and open_at + 1 < len(segment):
        entry_price = float(segment["Open"].iloc[open_at + 1])
        entry_date = segment.index[open_at + 1].date().isoformat()
        close = float(segment["Close"].iloc[-1])
        position = {
            "entry_date": entry_date,
            "entry_price": entry_price,
            "close": close,
            "return_pct": (close / entry_price - 1.0) * 100.0,
            "unrealised_pnl": (close / entry_price - 1.0) * notional,
            "bars_held": len(segment) - open_at - 1,
        }

    view = pd.DataFrame({
        "close": segment["Close"].tail(6),
        "rsi3": signals["rsi"].tail(6),
        "ema_rising": signals["ema_rising"].tail(6),
        "vwap10": signals["vwap"].tail(6),
        "entry": signals["entry"].tail(6),
        "exit": signals["exit"].tail(6),
    })
    view["vs_vwap_pct"] = (view["close"] / view["vwap10"] - 1.0) * 100.0

    ytd = [trade for trade in trades if trade.exit_date >= "2026-01-01"]
    return {
        "name": name,
        "last_bar": segment.index[-1].date().isoformat(),
        "close": float(segment["Close"].iloc[-1]),
        "view": view,
        "position": position,
        "trades": trades,
        "ytd_trades": len(ytd),
        "ytd_wins": sum(1 for trade in ytd if trade.pnl > 0),
        "ytd_pnl": sum(trade.pnl for trade in ytd),
        "stats": summarize(trades, capital=notional),
    }


def conditional(trades: Sequence[Trade], *, notional: float) -> pd.DataFrame:
    rows = []
    returns_all = np.array([trade.return_pct for trade in trades], dtype=float)
    for rsi_max in (10, 12, 15, 18, 20, 22):
        subset = [trade for trade in trades if trade.entry_rsi <= rsi_max]
        if not subset:
            continue
        returns = np.array([trade.return_pct for trade in subset], dtype=float)
        rows.append({
            "rsi_max": rsi_max,
            "trades": len(subset),
            "win_rate": float((returns > 0).mean() * 100.0),
            "mean_pct": float(returns.mean()),
            "median_pct": float(np.median(returns)),
            "mean_pnl": float(returns.mean() / 100.0 * notional),
            "median_pnl": float(np.median(returns) / 100.0 * notional),
            "p10_pnl": float(np.percentile(returns, 10) / 100.0 * notional),
            "p90_pnl": float(np.percentile(returns, 90) / 100.0 * notional),
            "worst_pnl": float(returns.min() / 100.0 * notional),
        })
    frame = pd.DataFrame(rows)
    frame.attrs["all"] = {
        "trades": len(returns_all),
        "win_rate": float((returns_all > 0).mean() * 100.0),
        "mean_pct": float(returns_all.mean()),
    }
    return frame


def print_view(result: Dict[str, Any], *, notional: float) -> None:
    name = result["name"]
    print(f"\n{name} — last bar {result['last_bar']}, close "
          f"{result['close']:,.2f}, notional Rs {notional:,.0f}")
    print("=" * 104)
    header = (f"{'Date':<12}{'Close':>12}{'RSI3':>7}{'EMA up':>8}{'VWAP10':>12}"
              f"{'vs VWAP':>9}{'Entry':>7}{'Exit':>6}")
    print(header)
    print("-" * len(header))
    for stamp, row in result["view"].iterrows():
        print(f"{stamp.date().isoformat():<12}{row['close']:>12,.2f}"
              f"{row['rsi3']:>7.1f}{'yes' if row['ema_rising'] else 'no':>8}"
              f"{row['vwap10']:>12,.2f}{row['vs_vwap_pct']:>8.1f}%"
              f"{'YES' if row['entry'] else '-':>7}{'YES' if row['exit'] else '-':>6}")

    position = result["position"]
    print("\nPOSITION")
    print("=" * 104)
    if position:
        print(f"OPEN  since {position['entry_date']} at "
              f"{position['entry_price']:,.2f} | close {position['close']:,.2f} | "
              f"unrealised Rs {position['unrealised_pnl']:,.2f} "
              f"({position['return_pct']:+.2f}%) over {position['bars_held']} bars")
    else:
        last = result["trades"][-1] if result["trades"] else None
        print("FLAT  no open position" + (
            f"; last trade closed {last.exit_date} "
            f"(Rs {last.pnl:+,.2f})" if last else "."))

    print(f"\n2026 realised: {result['ytd_trades']} trades, "
          f"{result['ytd_wins']} wins, net Rs {result['ytd_pnl']:,.2f}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="NIFTY")
    parser.add_argument("--notional", type=float, default=DEFAULT_NOTIONAL)
    parser.add_argument("--open-positions", action="store_true",
                        help="sweep every symbol for an open position")
    parser.add_argument("--signals", action="store_true",
                        help="show the latest two sessions' entry signals")
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    args = parser.parse_args(argv)

    universe = load_universe()

    if args.symbol not in universe:
        print(f"Unknown symbol {args.symbol}; try one of "
              f"{', '.join(list(universe)[:10])} ...")
        return 1

    result = analyse(args.symbol, universe[args.symbol], notional=args.notional)
    print_view(result, notional=args.notional)

    print("\nLAST 10 TRADES")
    print("=" * 104)
    header = (f"{'Entry':<12}{'Exit':<12}{'In':>12}{'Out':>12}{'Ret%':>8}"
              f"{'P&L Rs':>12}{'Days':>6}{'RSI':>7}")
    print(header)
    print("-" * len(header))
    for trade in result["trades"][-10:]:
        print(f"{trade.entry_date:<12}{trade.exit_date:<12}"
              f"{trade.entry_price:>12,.2f}{trade.exit_price:>12,.2f}"
              f"{trade.return_pct:>8.2f}{trade.pnl:>12,.0f}"
              f"{trade.bars_held:>6}{trade.entry_rsi:>7.1f}")

    table = conditional(result["trades"], notional=args.notional)
    print("\nWHAT A FRESH SIGNAL HAS PAID ON THIS SYMBOL")
    print("=" * 104)
    header = (f"{'Entry RSI <=':>13}{'Trades':>8}{'Win%':>7}{'Mean%':>8}"
              f"{'Median%':>9}{'Mean Rs':>12}{'Median Rs':>12}{'P10 Rs':>12}"
              f"{'P90 Rs':>12}{'Worst Rs':>12}")
    print(header)
    print("-" * len(header))
    for row in table.itertuples():
        print(f"{row.rsi_max:>13}{row.trades:>8}{row.win_rate:>6.1f}%"
              f"{row.mean_pct:>8.2f}{row.median_pct:>9.2f}"
              f"{row.mean_pnl:>12,.0f}{row.median_pnl:>12,.0f}"
              f"{row.p10_pnl:>12,.0f}{row.p90_pnl:>12,.0f}"
              f"{row.worst_pnl:>12,.0f}")

    stats = result["stats"]
    print(f"\nFull sample: {stats['trades']} trades, win {stats['win_rate']:.1f}%, "
          f"PF {stats['profit_factor']:.2f}, avg {stats['avg_trade_return_pct']:.2f}% "
          f"(Rs {stats['avg_trade_return_pct'] / 100 * args.notional:,.0f} per trade), "
          f"net Rs {stats['net_pnl']:,.0f}, "
          f"max DD {stats['max_drawdown_pct_of_capital']:.1f}%")

    payload: Dict[str, Any] = {
        "symbol": result["name"],
        "last_bar": result["last_bar"],
        "close": round(result["close"], 2),
        "position": result["position"],
        "ytd": {"trades": result["ytd_trades"], "wins": result["ytd_wins"],
                "net_pnl": round(result["ytd_pnl"], 2)},
        "stats": {key: stats[key] for key in
                  ("trades", "win_rate", "profit_factor",
                   "avg_trade_return_pct", "net_pnl",
                   "max_drawdown", "max_drawdown_pct_of_capital")},
    }

    if args.open_positions:
        rows = []
        for name, meta in universe.items():
            try:
                item = analyse(name, meta, notional=args.notional)
            except Exception:  # noqa: BLE001 - a bad cache should not stop the sweep
                continue
            if item["position"]:
                rows.append({"symbol": name, **item["position"]})
        frame = pd.DataFrame(rows).sort_values("unrealised_pnl", ascending=False)
        print(f"\nOPEN POSITIONS ACROSS THE UNIVERSE: {len(frame)} "
              f"(Rs {args.notional:,.0f} each)")
        print("=" * 104)
        if len(frame):
            header = (f"{'Symbol':<14}{'Entry':<12}{'In':>11}{'Now':>11}"
                      f"{'Ret%':>8}{'Unrealised Rs':>15}{'Bars':>6}")
            print(header)
            print("-" * len(header))
            for row in frame.itertuples():
                print(f"{row.symbol:<14}{row.entry_date:<12}"
                      f"{row.entry_price:>11,.2f}{row.close:>11,.2f}"
                      f"{row.return_pct:>8.2f}{row.unrealised_pnl:>15,.0f}"
                      f"{row.bars_held:>6}")
            print(f"\nTotal unrealised: Rs {frame['unrealised_pnl'].sum():,.0f} "
                  f"on Rs {len(frame) * args.notional:,.0f} deployed")
        payload["open_positions"] = rows

    if args.signals:
        rows = []
        for name, meta in universe.items():
            try:
                item = analyse(name, meta, notional=args.notional)
            except Exception:  # noqa: BLE001
                continue
            view = item["view"]
            for stamp, row in view.iterrows():
                if bool(row["entry"]):
                    rows.append({
                        "symbol": name,
                        "date": stamp.date().isoformat(),
                        "close": float(row["close"]),
                        "rsi3": float(row["rsi3"]),
                        "vs_vwap_pct": float(row["vs_vwap_pct"]),
                    })
        frame = pd.DataFrame(rows).sort_values(["date", "rsi3"])
        if len(frame):
            last_two = sorted(frame["date"].unique())[-2:]
            frame = frame[frame["date"].isin(last_two)]
        print("\nENTRY SIGNALS IN THE LAST TWO SESSIONS")
        print("=" * 104)
        if len(frame):
            print(frame.to_string(index=False))
        payload["signals"] = rows

    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(payload, indent=2, default=str),
                         encoding="utf-8")
    print(f"\nSaved JSON: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
