"""Price the QQQ-rule signals as NFO options, not spot.

The QQQ rule is a daily mean-reversion trade that holds ~4-5 sessions, so a
5-DTE weekly (the repo's ORB model) would expire mid-trade.  This script uses
an ATM call at a configurable DTE (default 30 calendar days, i.e. roughly the
nearest monthly), flat IV, real NFO lot size and strike step, and the repo's
option cost model.

For each signal it reports, side by side:

* the spot trade (fixed Rs 10L notional),
* the option trade (ATM CE, lots sized to about Rs 10L of underlying),
* the premium P&L marked to the latest close,
* the premium P&L if the underlying reaches its RSI(3) > 55 exit trigger
  after the median holding period.

    python scripts/backtest_nfo_option_signals.py --dates 2026-11-12,2026-11-13
    python scripts/backtest_nfo_option_signals.py --dates 2026-11-13 --dte 45

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

from scripts.backtest_orb_retest_v2 import option_costs  # noqa: E402
from scripts.backtest_qqq_mean_reversion import build_signals  # noqa: E402
from scripts.backtest_qqq_rules_wide import (  # noqa: E402
    DATA_DIR,
    load_bars,
    load_universe,
    split_segments,
)
from scripts.backtest_signal_day import exit_trigger_price  # noqa: E402
from scripts.scan_qqq_rules_latest import prepare  # noqa: E402
from src.options_pricing import bs_price  # noqa: E402

DEFAULT_JSON = ROOT / "output" / "nfo_option_signals.json"
DEFAULT_CSV = ROOT / "output" / "nfo_option_signals.csv"


def _cache(name: str) -> Path:
    return DATA_DIR / f"{name.replace('&', 'AND').replace('/', '_')}.csv"


def atm_strike(spot: float, step: float) -> float:
    if step <= 0:
        return round(spot)
    return round(spot / step) * step


def collect(dates: Sequence[str], *, notional: float, dte: float,
            iv: float) -> List[Dict[str, Any]]:
    universe = load_universe()
    rows: List[Dict[str, Any]] = []
    for name, meta in universe.items():
        if meta.get("kind") == "index":
            continue  # index option lot/premium math is separate; keep it stock-only
        try:
            is_index = False
            bars = prepare(load_bars(str(_cache(name)), is_index=is_index),
                           is_index=is_index)
        except Exception:  # noqa: BLE001
            continue
        segment = split_segments(bars)[-1]
        if len(segment) < 120:
            continue
        signals = build_signals(segment)
        labels = [stamp.date().isoformat() for stamp in segment.index]
        latest_close = float(segment["Close"].iloc[-1])
        lot = int(meta.get("lot") or 0)
        step = float(meta.get("step") or 0)
        if lot <= 0:
            continue

        for date in dates:
            if date not in labels:
                continue
            position = labels.index(date)
            if not bool(signals["entry"].iloc[position]):
                continue

            signal_close = float(segment["Close"].iloc[position])
            vwap = float(signals["vwap"].iloc[position])
            trigger = exit_trigger_price(segment["Close"].iloc[:position + 1])
            filled = position + 1 < len(segment)

            row: Dict[str, Any] = {
                "symbol": name,
                "signal_date": date,
                "signal_close": round(signal_close, 2),
                "rsi3": round(float(signals["rsi"].iloc[position]), 2),
                "vs_vwap_pct": round((signal_close / vwap - 1.0) * 100.0, 2),
                "lot": lot,
                "step": step,
                "spot_fill": None,
                "spot_pnl": None,
                "option_fill_date": None,
                "strike": atm_strike(signal_close, step),
                "lots": None,
                "qty": None,
                "entry_premium": None,
                "mark_premium": None,
                "option_pnl": None,
                "capital": None,
                "trigger": round(trigger, 2) if trigger else None,
                "option_pnl_at_trigger": None,
                "est_premium": None,
                "est_lots": None,
                "est_capital": None,
            }

            if not filled:
                # Provisional sizing for the next open, priced off the signal
                # close.  Not a P&L, just what the position would cost.
                strike = atm_strike(signal_close, step)
                lots = max(1, int(round(notional / (signal_close * lot))))
                prem = bs_price(signal_close, strike, dte / 365.0, iv, "CE")
                row.update({
                    "strike": strike,
                    "est_lots": lots,
                    "est_premium": round(prem, 2),
                    "est_capital": round(prem * lots * lot, 2),
                })

            if filled:
                fill = float(segment["Open"].iloc[position + 1])
                row["spot_fill"] = round(fill, 2)
                row["spot_pnl"] = round((latest_close / fill - 1.0) * notional, 2)
                row["option_fill_date"] = labels[position + 1]

                strike = atm_strike(fill, step)
                row["strike"] = strike
                lots = max(1, int(round(notional / (fill * lot))))
                qty = lots * lot
                entry_premium = bs_price(fill, strike, dte / 365.0, iv, "CE")
                elapsed = len(segment) - (position + 1)
                remaining = max(dte - elapsed, 1.0)
                mark_premium = bs_price(latest_close, strike,
                                        remaining / 365.0, iv, "CE")
                costs = option_costs(entry_premium, mark_premium, lot, lots)
                pnl = (mark_premium - entry_premium) * qty - costs

                row.update({
                    "lots": lots,
                    "qty": qty,
                    "entry_premium": round(entry_premium, 2),
                    "mark_premium": round(mark_premium, 2),
                    "option_pnl": round(pnl, 2),
                    "capital": round(entry_premium * qty, 2),
                })

                if trigger:
                    hold = max(4, 1)
                    t_trigger = max(dte - elapsed - hold, 1.0)
                    trig_premium = bs_price(trigger, strike,
                                            t_trigger / 365.0, iv, "CE")
                    trig_costs = option_costs(entry_premium, trig_premium,
                                              lot, lots)
                    row["option_pnl_at_trigger"] = round(
                        (trig_premium - entry_premium) * qty - trig_costs, 2)

            rows.append(row)
    return rows


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dates", default="2026-11-12,2026-11-13")
    parser.add_argument("--notional", type=float, default=1_000_000.0)
    parser.add_argument("--dte", type=float, default=30.0)
    parser.add_argument("--iv", type=float, default=0.18)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    args = parser.parse_args(argv)

    dates = [item.strip() for item in args.dates.split(",") if item.strip()]
    rows = collect(dates, notional=args.notional, dte=args.dte, iv=args.iv)
    if not rows:
        print("No signals on those dates.")
        return 1

    frame = pd.DataFrame(rows)
    print(f"NFO OPTION EXECUTION | ATM CE, {args.dte:.0f} DTE, IV {args.iv:.0%}, "
          f"underlying notional Rs {args.notional:,.0f}, "
          f"lot sizes from the NFO dump")
    print("=" * 126)

    for date in dates:
        day = frame[frame["signal_date"] == date]
        if not len(day):
            continue
        print(f"\nSIGNALS ON {date}: {len(day)}")
        header = (f"{'Symbol':<12}{'RSI3':>6}{'Signal':>10}{'Strike':>10}"
                  f"{'Lot':>6}{'Lots':>6}{'Qty':>8}{'Prem':>9}{'Mark':>9}"
                  f"{'Cap Rs':>10}{'Opt P&L':>11}{'Spot P&L':>11}"
                  f"{'At trig':>11}")
        print(header)
        print("-" * len(header))
        for row in day.sort_values("rsi3").itertuples():
            def show(value, digits=0):
                if value is None or (isinstance(value, float) and np.isnan(value)):
                    return "-"
                return f"{value:,.{digits}f}"

            filled_row = row.option_pnl is not None and not (
                isinstance(row.option_pnl, float) and np.isnan(row.option_pnl))
            premium = row.entry_premium if filled_row else row.est_premium
            capital = row.capital if filled_row else row.est_capital
            lots = row.lots if filled_row else row.est_lots
            qty = row.qty if filled_row else None
            print(f"{row.symbol:<12}{row.rsi3:>6.1f}{row.signal_close:>10,.2f}"
                  f"{show(row.strike):>10}{row.lot:>6}{show(lots):>6}"
                  f"{show(qty):>8}{show(premium, 2):>9}"
                  f"{show(row.mark_premium, 2) if filled_row else '-':>9}"
                  f"{show(capital):>10}"
                  f"{show(row.option_pnl) if filled_row else 'pending':>11}"
                  f"{show(row.spot_pnl) if filled_row else 'pending':>11}"
                  f"{show(row.option_pnl_at_trigger) if filled_row else '-':>11}")

        filled = day[day["option_pnl"].notna()]
        if len(filled):
            print(f"\n  filled {len(filled)}: option P&L Rs "
                  f"{filled['option_pnl'].sum():+,.0f} on Rs "
                  f"{filled['capital'].sum():,.0f} premium "
                  f"({filled['option_pnl'].sum() / filled['capital'].sum() * 100:+.1f}%); "
                  f"spot P&L Rs {filled['spot_pnl'].sum():+,.0f} on Rs "
                  f"{len(filled) * args.notional:,.0f} "
                  f"({filled['spot_pnl'].sum() / (len(filled) * args.notional) * 100:+.2f}%)")

    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps({"config": {
        "dates": dates, "notional": args.notional, "dte": args.dte,
        "iv": args.iv}, "rows": rows}, indent=2, default=str),
        encoding="utf-8")
    frame.to_csv(args.csv, index=False)
    print(f"\nSaved JSON: {args.json}")
    print(f"Saved CSV : {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
