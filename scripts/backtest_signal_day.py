"""Detailed backtest of a single session across the NFO option universe.

For one date it reports, symbol by symbol:

* every entry signal generated that session (filled at the next open),
* every entry actually filled that session (from the prior session's signal),
* the fill price, quantity, current mark and unrealised P&L,
* the last 12 sessions of RSI(3), EMA(100) and 10-day VWAP,
* the close the symbol needs to print for RSI(3) to cross the 55 exit,
* how the same symbol's history performed on comparable entry RSI values.

    python scripts/backtest_signal_day.py --date 2026-11-12
    python scripts/backtest_signal_day.py --date 2026-11-12 --symbols HAL,TCS
    python scripts/backtest_signal_day.py --date 2026-11-12 --notional 1000000

DISCLAIMER: Educational purposes only. Not financial advice.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest_qqq_mean_reversion import (  # noqa: E402
    build_signals,
    run_backtest,
)
from scripts.backtest_qqq_rules_wide import (  # noqa: E402
    DATA_DIR,
    load_bars,
    load_universe,
    split_segments,
)
from scripts.scan_qqq_rules_latest import prepare  # noqa: E402

DEFAULT_JSON = ROOT / "output" / "signal_day_detail.json"
DEFAULT_CSV = ROOT / "output" / "signal_day_detail.csv"
RSI_EXIT = 55.0


def _cache(name: str) -> Path:
    return DATA_DIR / f"{name.replace('&', 'AND').replace('/', '_')}.csv"


def wilder_state(close: pd.Series, period: int = 3) -> Tuple[float, float]:
    """Final Wilder average gain / average loss, matching rsi_wilder."""
    values = close.to_numpy(dtype=float)
    deltas = np.diff(values)
    avg_gain = float(np.mean(np.clip(deltas[:period], 0.0, None)))
    avg_loss = float(np.mean(np.clip(-deltas[:period], 0.0, None)))
    for index in range(period + 1, len(values)):
        delta = deltas[index - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0.0)) / period
    return avg_gain, avg_loss


def exit_trigger_price(close: pd.Series, *, rsi_exit: float = RSI_EXIT,
                       period: int = 3) -> Optional[float]:
    """Close needed on the next session for RSI(3) to finish above 55."""
    avg_gain, avg_loss = wilder_state(close, period)
    if avg_loss <= 0:
        return None  # RSI is already 100; any close keeps it there
    target = rsi_exit / (100.0 - rsi_exit)
    previous = float(close.iloc[-1])
    rsi_at_flat = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    if rsi_at_flat < rsi_exit:
        return previous + target * (period - 1) * avg_loss - (period - 1) * avg_gain
    return previous + (period - 1) * avg_loss - (period - 1) * avg_gain / target


def load_state(name: str, meta: Dict[str, Any], *, notional: float) -> Dict[str, Any]:
    is_index = meta.get("kind") == "index"
    bars = load_bars(str(_cache(name)), is_index=is_index)
    bars = prepare(bars, is_index=is_index)
    segment = split_segments(bars)[-1]
    signals = build_signals(segment)
    trades = run_backtest(segment, signals, exit_timing="next_open",
                          cost_bps=5.0, capital=notional)
    return {"name": name, "bars": segment, "signals": signals, "trades": trades}


def analog_stats(trades: Sequence[Any], *, entry_rsi: float,
                 notional: float, tolerance: float = 2.0) -> Dict[str, Any]:
    subset = [trade for trade in trades
              if abs(trade.entry_rsi - entry_rsi) <= tolerance]
    if not subset:
        return {"trades": 0}
    returns = np.array([trade.return_pct for trade in subset], dtype=float)
    holds = np.array([trade.bars_held for trade in subset], dtype=float)
    return {
        "trades": len(subset),
        "win_rate": float((returns > 0).mean() * 100.0),
        "mean_pct": float(returns.mean()),
        "median_pct": float(np.median(returns)),
        "mean_pnl": float(returns.mean() / 100.0 * notional),
        "median_pnl": float(np.median(returns) / 100.0 * notional),
        "worst_pct": float(returns.min()),
        "p10_pct": float(np.percentile(returns, 10)),
        "p90_pct": float(np.percentile(returns, 90)),
        "median_bars": float(np.median(holds)),
        "max_bars": float(holds.max()),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default="2026-11-12",
                        help="session to backtest (YYYY-MM-DD)")
    parser.add_argument("--notional", type=float, default=1_000_000.0)
    parser.add_argument("--symbols", default="",
                        help="comma-separated subset (default: all that traded)")
    parser.add_argument("--bars", type=int, default=12)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    args = parser.parse_args(argv)

    universe = load_universe()
    wanted = {item.strip().upper() for item in args.symbols.split(",") if item.strip()}

    signals_on: List[Dict[str, Any]] = []
    fills_on: List[Dict[str, Any]] = []
    details: Dict[str, Dict[str, Any]] = {}

    for name, meta in universe.items():
        if wanted and name not in wanted:
            continue
        try:
            state = load_state(name, meta, notional=args.notional)
        except Exception:  # noqa: BLE001 - skip unreadable caches
            continue
        bars, signals = state["bars"], state["signals"]
        dates = [stamp.date().isoformat() for stamp in bars.index]
        if args.date not in dates:
            continue
        position = dates.index(args.date)

        # A signal on the requested session -> filled at the next open.
        if bool(signals["entry"].iloc[position]):
            close = float(bars["Close"].iloc[position])
            vwap = float(signals["vwap"].iloc[position])
            fill = (float(bars["Open"].iloc[position + 1])
                    if position + 1 < len(bars) else None)
            latest_close = float(bars["Close"].iloc[-1])
            signals_on.append({
                "symbol": name,
                "signal_date": args.date,
                "close": close,
                "rsi3": float(signals["rsi"].iloc[position]),
                "vwap10": vwap,
                "vs_vwap_pct": (close / vwap - 1.0) * 100.0,
                "ema_rising": bool(signals["ema_rising"].iloc[position]),
                "fill_date": dates[position + 1] if position + 1 < len(bars) else None,
                "fill_price": fill,
                "qty": (args.notional / fill) if fill else None,
                "mark": latest_close,
                "unrealised_pnl": ((latest_close / fill - 1.0) * args.notional
                                   if fill else None),
                "unrealised_pct": ((latest_close / fill - 1.0) * 100.0
                                   if fill else None),
                "exit_trigger": exit_trigger_price(bars["Close"].iloc[:position + 1]),
            })

        # An entry filled on the requested session came from the prior signal.
        if position > 0 and bool(signals["entry"].iloc[position - 1]):
            fill = float(bars["Open"].iloc[position])
            latest_close = float(bars["Close"].iloc[-1])
            fills_on.append({
                "symbol": name,
                "signal_date": dates[position - 1],
                "fill_date": args.date,
                "fill_price": fill,
                "qty": args.notional / fill,
                "mark": latest_close,
                "unrealised_pnl": (latest_close / fill - 1.0) * args.notional,
                "unrealised_pct": (latest_close / fill - 1.0) * 100.0,
                "bars_held": len(bars) - position,
                "rsi_now": float(signals["rsi"].iloc[-1]),
                "exit_trigger": exit_trigger_price(bars["Close"]),
            })

        if name in wanted or wanted == set():
            details[name] = state

    # ------------------------------------------------------------------ report
    print(f"SESSION {args.date} | notional Rs {args.notional:,.0f} | "
          f"5 bps per side | RSI exit > {RSI_EXIT:.0f}")

    print(f"\nSIGNALS GENERATED ON {args.date}: {len(signals_on)}")
    print("=" * 118)
    if signals_on:
        header = (f"{'Symbol':<13}{'Close':>11}{'RSI3':>7}{'vs VWAP':>9}"
                  f"{'Fill':>11}{'Qty':>10}{'Mark':>11}{'Unreal Rs':>12}{'%':>8}"
                  f"{'Exit >':>11}")
        print(header)
        print("-" * len(header))
        for row in sorted(signals_on, key=lambda item: item["rsi3"]):
            fill = row["fill_price"]
            fill_text = f"{fill:,.2f}" if fill else "pending"
            qty_text = f"{row['qty']:,.1f}" if row["qty"] else "-"
            pnl = row["unrealised_pnl"]
            pct = row["unrealised_pct"]
            pnl_text = f"{pnl:+,.0f}" if pnl is not None else "-"
            pct_text = f"{pct:+.2f}" if pct is not None else "-"
            trigger = row["exit_trigger"]
            trigger_text = f"{trigger:,.2f}" if trigger else "-"
            print(f"{row['symbol']:<13}{row['close']:>11,.2f}{row['rsi3']:>7.1f}"
                  f"{row['vs_vwap_pct']:>8.1f}%{fill_text:>11}{qty_text:>10}"
                  f"{row['mark']:>11,.2f}{pnl_text:>12}{pct_text:>8}"
                  f"{trigger_text:>11}")
    else:
        print("none")

    print(f"\nENTRIES FILLED ON {args.date} (from the prior session's signal): "
          f"{len(fills_on)}")
    print("=" * 118)
    if fills_on:
        header = (f"{'Symbol':<13}{'Signal':<12}{'Fill':>11}{'Qty':>10}"
                  f"{'Mark':>11}{'Unreal Rs':>12}{'%':>8}{'RSI now':>9}"
                  f"{'Exit >':>11}")
        print(header)
        print("-" * len(header))
        for row in sorted(fills_on, key=lambda item: item["unrealised_pnl"]):
            print(f"{row['symbol']:<13}{row['signal_date']:<12}"
                  f"{row['fill_price']:>11,.2f}{row['qty']:>10,.1f}"
                  f"{row['mark']:>11,.2f}{row['unrealised_pnl']:>+12,.0f}"
                  f"{row['unrealised_pct']:>+8.2f}{row['rsi_now']:>9.1f}"
                  f"{row['exit_trigger']:>11,.2f}")
        total = sum(row["unrealised_pnl"] for row in fills_on)
        deployed = args.notional * len(fills_on)
        print(f"\nTotal unrealised Rs {total:+,.0f} on Rs {deployed:,.0f} deployed "
              f"({total / deployed * 100:+.2f}%)")
    else:
        print("none")

    # ------------------------------------------------- per-symbol deep dive
    focus = sorted({row["symbol"] for row in signals_on} |
                   {row["symbol"] for row in fills_on})
    payload: Dict[str, Any] = {
        "date": args.date,
        "notional": args.notional,
        "signals": signals_on,
        "fills": fills_on,
        "symbols": {},
    }
    for name in focus:
        state = details.get(name)
        if state is None:
            continue
        bars, signals = state["bars"], state["signals"]
        print(f"\n{name} — last {args.bars} sessions")
        print("-" * 118)
        header = (f"{'Date':<12}{'Close':>11}{'RSI3':>7}{'EMA up':>8}"
                  f"{'VWAP10':>11}{'vs VWAP':>9}{'Entry':>7}{'Exit':>6}")
        print(header)
        print("-" * len(header))
        view = pd.DataFrame({
            "close": bars["Close"].tail(args.bars),
            "rsi3": signals["rsi"].tail(args.bars),
            "ema_rising": signals["ema_rising"].tail(args.bars),
            "vwap10": signals["vwap"].tail(args.bars),
            "entry": signals["entry"].tail(args.bars),
            "exit": signals["exit"].tail(args.bars),
        })
        view["vs_vwap_pct"] = (view["close"] / view["vwap10"] - 1.0) * 100.0
        for stamp, row in view.iterrows():
            print(f"{stamp.date().isoformat():<12}{row['close']:>11,.2f}"
                  f"{row['rsi3']:>7.1f}{'yes' if row['ema_rising'] else 'no':>8}"
                  f"{row['vwap10']:>11,.2f}{row['vs_vwap_pct']:>8.1f}%"
                  f"{'YES' if row['entry'] else '-':>7}{'YES' if row['exit'] else '-':>6}")

        trigger = exit_trigger_price(bars["Close"])
        close_now = float(bars["Close"].iloc[-1])
        if trigger:
            gap = (trigger / close_now - 1.0) * 100.0
            print(f"  exit trigger: RSI(3) > 55 needs a close above "
                  f"{trigger:,.2f} ({gap:+.2f}% from {close_now:,.2f})")

        signal_row = next((row for row in signals_on if row["symbol"] == name), None)
        analog_rsi = (signal_row["rsi3"] if signal_row else
                      float(signals["rsi"].iloc[-1]))
        stats = analog_stats(state["trades"], entry_rsi=analog_rsi,
                             notional=args.notional)
        if stats["trades"]:
            print(f"  history at entry RSI {analog_rsi:.1f} +/- 2: "
                  f"{stats['trades']} trades, win {stats['win_rate']:.1f}%, "
                  f"mean {stats['mean_pct']:+.2f}% (Rs {stats['mean_pnl']:+,.0f}), "
                  f"median {stats['median_pct']:+.2f}%, "
                  f"worst {stats['worst_pct']:+.2f}%, "
                  f"median hold {stats['median_bars']:.0f} bars")
        payload["symbols"][name] = {
            "analog": stats,
            "exit_trigger": round(trigger, 2) if trigger else None,
            "close": round(close_now, 2),
        }

    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(payload, indent=2, default=str),
                         encoding="utf-8")
    rows = signals_on + fills_on
    if rows:
        pd.DataFrame(rows).to_csv(args.csv, index=False)
    print(f"\nSaved JSON: {args.json}")
    if rows:
        print(f"Saved CSV : {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
