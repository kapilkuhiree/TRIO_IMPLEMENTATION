"""
TRIO — Intraday session replay with end-of-day P&L.

What this does
--------------
Replays one full NSE session bar-by-bar (15m or 5m), takes every signal the
engine fires, manages stop/target on subsequent bars, force-closes anything
still open at 15:15 IST, and prints trade-by-trade P&L plus the day total.

Why this exists
---------------
The user asked: "trade every 15m/5m candle, then show me the day's P&L."
This script answers exactly that — on paper, on historical bars, with the
app's REAL signal path (compute_indicators -> generate_signal ->
apply_risk_management), not a simplified copy.

Honesty notice
--------------
The 15m entry variant FAILED out-of-sample validation (test PF 0.04-0.55,
see scripts/validation_intraday.json). So this tool runs BOTH window sets
and labels them:
  - "daily-windows": the validated 200/50 setup (expects ~zero intraday
    trades — there are not enough 15m bars for a 200 SMA).
  - "intraday-windows": the experimental 50/20 setup (trades, but
    unvalidated — treat its P&L as a measurement, not a promise).

Run:
  python scripts/day_session.py [--symbol RELIANCE.NS] [--tf 15m|5m] [--days 5]
"""

import argparse
import sys
from datetime import time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import logging
logging.disable(logging.CRITICAL)

from src.data_fetcher import fetch_market_data  # noqa: E402
from src.indicators import compute_indicators  # noqa: E402
from src.signal_engine import generate_signal  # noqa: E402
from src.risk_manager import (  # noqa: E402
    apply_risk_management,
    calculate_position_size,
    reset_risk_state,
)

CAPITAL = 100000.0
SESSION_END = dtime(15, 15)

WINDOW_SETS = {
    "daily-windows": {"trend_sma": 200, "pullback_sma": 50},
    "intraday-windows": {"trend_sma": 50, "pullback_sma": 20},
}


def session_slices(df: pd.DataFrame) -> List[pd.DataFrame]:
    """Split 15m/5m bars into per-day NSE sessions.

    Keeps only FULL sessions (first bar 09:15, last bar 15:15/15:30).
    Today's still-open session is excluded — its P&L would be partial.
    """
    df = df.copy()
    df.index = pd.to_datetime(df.index)
    days = []
    for day, grp in df.groupby(df.index.date):
        grp = grp.between_time("09:15", "15:30")
        if len(grp) < 20:
            continue
        t0 = grp.index[0].time()
        t1 = grp.index[-1].time()
        if t0.hour != 9 or t0.minute != 15:
            continue  # session missing its open
        if not (t1.hour == 15 and t1.minute >= 15):
            continue  # session not closed yet
        days.append(grp)
    return days


_DBG: Dict[str, Any] = {"n": 0, "lines": []}

# Time stop: exit a day-trade that has gone nowhere after this many bars.
# Measured on the 21-day NIFTY 15m replay (scripts/sweep_timestop.py):
#   8 bars -> 1704.93 | 12 -> 2254.82 | 16 -> 2370.81
#   20 -> 2742.40 | 24/off -> 2963.61
# VERDICT: every tighter setting lost money vs holding into close on this
# window. The time stop stays OFF (a very large value = effectively the
# 15:15 squareoff). Revisit only with new data, not new opinions.
TIME_STOP_BARS = 10 ** 9  # off; see sweep_timestop.py evidence above


def replay_day(session: pd.DataFrame, symbol: str, tf: str,
               windows: Dict[str, int],
               warmup: Optional[pd.DataFrame] = None,
               debug_empty: bool = True) -> Dict[str, Any]:
    """Walk one session bar-by-bar through the real app signal path.

    `warmup` is history BEFORE the session open. Indicator windows are
    computed over warmup+session-so-far, so the gate has full SMAs from the
    first bar of the day. Signals, entries and P&L are recorded for session
    bars only.
    """
    import src.utils as _u

    cfg = _u.load_config()
    cfg["signal_engine"]["pullback"]["trend_sma"] = windows["trend_sma"]
    cfg["signal_engine"]["pullback"]["pullback_sma"] = windows["pullback_sma"]
    # Ensure the gate's windows are computable.
    ind = cfg["indicators"]["trend"]
    for p in (windows["trend_sma"], windows["pullback_sma"]):
        if p not in ind["sma_periods"]:
            ind["sma_periods"] = sorted(set(ind["sma_periods"]) | {p})

    reset_risk_state()
    trades: List[Dict[str, Any]] = []
    pos: Optional[Dict[str, Any]] = None
    _DBG.update({"n": 0, "lines": []})

    base = warmup if warmup is not None and len(warmup) else None

    for i in range(len(session)):
        # Window = warmup history + session bars so far (cap 250 for speed:
        # 250 x 15m bars ≈ 10 sessions, more than enough for 200-SMA math).
        hist = session.iloc[:i + 1]
        if base is not None:
            window = pd.concat([base, hist]).tail(250)
        else:
            window = hist.tail(250)
        if len(window) < 30:
            continue
        row = session.iloc[i]
        close = float(row["Close"])
        high = float(row["High"])
        low = float(row["Low"])

        # 1) Manage the open position first. Two exits, in order:
        #    (a) time stop — flat after N bars with no touch, because the
        #        21-day replay shows drift-into-close does the real work and
        #        overstay turns winners into squareoff coin-flips;
        #    (b) stop/target on this bar's range.
        if pos is not None:
            held = i - pos["entry_bar"]
            if held >= TIME_STOP_BARS:
                trades.append(_close(pos, close, i, "time-stop"))
                pos = None
                continue
            if pos["action"] == "BUY":
                if low <= pos["stop"]:
                    trades.append(_close(pos, pos["stop"], i, "stop"))
                    pos = None
                    continue
                if high >= pos["target"]:
                    trades.append(_close(pos, pos["target"], i, "target"))
                    pos = None
                    continue
            else:  # SELL
                if high >= pos["stop"]:
                    trades.append(_close(pos, pos["stop"], i, "stop"))
                    pos = None
                    continue
                if low <= pos["target"]:
                    trades.append(_close(pos, pos["target"], i, "target"))
                    pos = None
                    continue

        # 2) Ask the engine for a fresh signal at this bar's close.
        try:
            readings = compute_indicators(window, symbol, tf)
        except Exception:
            continue
        sig = generate_signal(symbol=symbol, latest_price=close,
                              technical_readings=readings)
        if sig.action not in ("BUY", "SELL"):
            continue

        atr_tup = next((v.value for k, v in readings.indicators.items()
                        if k.startswith("atr_")), None)
        swing = (readings.swing_low if sig.action == "BUY"
                 else readings.swing_high)
        sig = apply_risk_management(sig, atr_tup, swing_level=swing)
        if sig.action not in ("BUY", "SELL") or not sig.position_size:
            if _DBG["n"] < 3:
                _DBG["n"] += 1
                _DBG["lines"].append(
                    f"sized-out: action={sig.action} size={sig.position_size} "
                    f"stop={sig.stop_loss} entry={close:.1f}")
            continue
        if pos is not None:
            continue  # one position at a time

        pos = {
            "action": sig.action,
            "entry": close,
            "stop": sig.stop_loss,
            "target": sig.target,
            "qty": sig.position_size,
            "entry_bar": i,
        }

    # 3) Square off anything left at the closing print.
    if pos is not None:
        last = float(session["Close"].iloc[-1])
        trades.append(_close(pos, last, len(session) - 1, "squareoff"))

    wins = [t for t in trades if t["pnl"] > 0]
    total = round(sum(t["pnl"] for t in trades), 2)
    if trades:
        return {
            "date": str(session.index[0].date()),
            "trades": len(trades),
            "wins": len(wins),
            "pnl": total,
            "win_rate": round(len(wins) / len(trades) * 100, 1),
            "details": trades,
        }

    if not debug_empty:
        # Fast path for multi-symbol runs: skip the expensive no-trade
        # double-pass (it re-runs the whole session a second time).
        return {
            "date": str(session.index[0].date()),
            "trades": 0,
            "wins": 0,
            "pnl": 0.0,
            "win_rate": 0.0,
            "details": [],
            "debug": "",
            "last_reasons": [],
        }

    # No trades: report which stage filtered everything out, including the
    # engine's own refusal reasons on the last bar.
    raw_buy = raw_sell = sized_ok = 0
    last_reasons: list = []
    for j in range(len(session)):
        hist_j = session.iloc[:j + 1]
        if base is not None:
            window = pd.concat([base, hist_j]).tail(250)
        else:
            window = hist_j.tail(250)
        if len(window) < 30:
            continue
        try:
            readings = compute_indicators(window, symbol, tf)
        except Exception:
            continue
        sig = generate_signal(symbol=symbol,
                              latest_price=float(session["Close"].iloc[j]),
                              technical_readings=readings)
        if j == len(session) - 1:
            last_reasons = list(sig.reasoning or [])[:6]
        if sig.action == "BUY":
            raw_buy += 1
        elif sig.action == "SELL":
            raw_sell += 1
        atr_v = next((v.value for k, v in readings.indicators.items()
                      if k.startswith("atr_")), None)
        swing = (readings.swing_low if sig.action == "BUY"
                 else readings.swing_high)
        sig2 = apply_risk_management(sig, atr_v, swing_level=swing)
        if sig2.action in ("BUY", "SELL") and sig2.position_size:
            sized_ok += 1
    dbg = (f"raw BUY={raw_buy} SELL={raw_sell}, "
           f"survived sizing={sized_ok}")
    if _DBG["lines"]:
        dbg += " | sized-out samples: " + " // ".join(_DBG["lines"][:3])
    return {
        "date": str(session.index[0].date()),
        "trades": 0,
        "wins": 0,
        "pnl": 0.0,
        "win_rate": 0.0,
        "details": [],
        "debug": dbg,
        "last_reasons": last_reasons,
    }


def _close(pos: Dict[str, Any], px: float, bar: int,
           reason: str) -> Dict[str, Any]:
    qty = pos["qty"]
    pnl = (px - pos["entry"]) * qty if pos["action"] == "BUY" \
        else (pos["entry"] - px) * qty
    return {
        "action": pos["action"], "entry": round(pos["entry"], 2),
        "exit": round(px, 2), "qty": qty,
        "pnl": round(pnl, 2), "reason": reason, "exit_bar": bar,
        "entry_bar": pos.get("entry_bar", -1),
    }


def run_symbol(symbol: str, tf: str, days: list, full,
               window_sets=("daily-windows", "intraday-windows"),
               debug_empty: bool = True) -> dict:
    """Replay one symbol over the given sessions; return totals + per-day rows.

    Multi-symbol runs pass window_sets=("daily-windows",) and
    debug_empty=False: the intraday set is experimental (skip it), and the
    no-trade debug double-pass doubles cost on empty days (skip it).
    """
    out = {}
    for name in window_sets:
        tot_pnl, tot_t, tot_w = 0.0, 0, 0
        rows = []
        for sess in days:
            end_pos = full.index.get_loc(sess.index[-1])
            start_hist = max(0, end_pos - len(sess) - 300)
            hist = full.iloc[start_hist:end_pos - len(sess) + 1]
            r = replay_day(sess, symbol, tf, WINDOW_SETS[name],
                           warmup=hist)
            tot_pnl += r["pnl"]
            tot_t += r["trades"]
            tot_w += r["wins"]
            rows.append(r)
        wr = round(tot_w / tot_t * 100, 1) if tot_t else 0.0
        out[name] = {"pnl": round(tot_pnl, 2), "trades": tot_t,
                     "wins": tot_w, "win_rate": wr, "rows": rows}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="RELIANCE.NS")
    ap.add_argument("--symbols", nargs="*", default=None,
                    help="Replay several symbols (whole previous month each)")
    ap.add_argument("--tf", default="15m", choices=["15m", "5m"])
    ap.add_argument("--days", type=int, default=5)
    args = ap.parse_args()

    symbols = args.symbols or [args.symbol]
    multi = len(symbols) > 1

    grand = {}
    # Multi-symbol month runs: daily-windows only (validated path) and no
    # no-trade debug double-pass — roughly 4x faster. Single-symbol runs
    # keep the full verbose output.
    window_sets = ("daily-windows",) if multi else ("daily-windows", "intraday-windows")
    for n, sym in enumerate(symbols, 1):
        period = "60d" if args.tf == "15m" else "30d"
        md = fetch_market_data(sym, args.tf, period)
        full = md.ohlcv
        days = session_slices(full)[-args.days:]
        print(f"[{n}/{len(symbols)}] {sym} {args.tf}: replaying last "
              f"{len(days)} complete sessions "
              f"(indicators warmed from {len(full)} prior bars)\n", flush=True)
        res = run_symbol(sym, args.tf, days, full, window_sets=window_sets,
                         debug_empty=not multi)
        grand[sym] = res

        for name in window_sets:
            print(f"=== {name} (trend={WINDOW_SETS[name]['trend_sma']}, "
                  f"pullback={WINDOW_SETS[name]['pullback_sma']}) ===")
            print(f"{'date':<12} {'trades':>7} {'wins':>5} {'win%':>7} {'day P&L':>10}")
            print("-" * 48)
            for r in res[name]["rows"]:
                print(f"{r['date']:<12} {r['trades']:>7} {r['wins']:>5} "
                      f"{r['win_rate']:>6}% {r['pnl']:>10.2f}")
                if not multi:
                    if r.get("debug"):
                        print(f"    ({r['debug']})")
                    if r.get("last_reasons"):
                        for line in r["last_reasons"]:
                            print(f"    last-bar reason: {line}")
                    for t in r["details"]:
                        print(f"    {t['action']:>4} {t['qty']:>3} @ {t['entry']:<9} "
                              f"-> {t['exit']:<9} {t['pnl']:>+9.2f}  ({t['reason']})")
            tot = res[name]
            print(f"{'TOTAL':<12} {tot['trades']:>7} {tot['wins']:>5} "
                  f"{tot['win_rate']:>6}% {tot['pnl']:>10.2f}\n")

    if multi:
        # Whole-month scoreboard: one row per symbol (daily-windows only —
        # the validated path), sorted by P&L.
        print("=" * 64, flush=True)
        print("WHOLE-MONTH SCOREBOARD (daily-windows, validated path)")
        print(f"{'symbol':<16} {'trades':>7} {'wins':>5} {'win%':>7} {'month P&L':>10}")
        print("-" * 64)
        tot_pnl = tot_t = tot_w = 0
        rows = []
        for sym, res in grand.items():
            d = res["daily-windows"]
            rows.append((sym, d["trades"], d["wins"], d["win_rate"], d["pnl"]))
            tot_pnl += d["pnl"]
            tot_t += d["trades"]
            tot_w += d["wins"]
        for sym, t, w, wr, p in sorted(rows, key=lambda r: r[4], reverse=True):
            print(f"{sym:<16} {t:>7} {w:>5} {wr:>6}% {p:>10.2f}")
        awr = round(tot_w / tot_t * 100, 1) if tot_t else 0.0
        print("-" * 64)
        print(f"{'COMBINED':<16} {tot_t:>7} {tot_w:>5} {awr:>6}% {round(tot_pnl, 2):>10.2f}")
        print("=" * 64)

    print("Note: intraday-windows is EXPERIMENTAL (failed validation). "
          "Its P&L above is a measurement, not a promise.")


if __name__ == "__main__":
    main()
