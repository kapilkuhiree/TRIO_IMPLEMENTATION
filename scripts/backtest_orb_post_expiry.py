"""Post-expiry ORB / ORB-Retest study (NIFTY weekly calendar).

For every NIFTY weekly expiry in the window, take the next TWO trading
sessions (E+1 and E+2) and run the live ORB and ORB-Retest strategies on
5-minute index bars.  Options are priced with Black-Scholes at the *actual*
calendar days to the following expiry, with an IV sweep, plus a fixed-5-DTE
control that reproduces the existing ORB research calibration.

Why this matters: weekly expiries reset the option book, so E+1 / E+2 carry
5-6 DTE -- exactly the horizon the ORB premium rules (stop -20%, target +30%)
were calibrated on, unlike expiry day itself at 0 DTE.

Data source
-----------
Zerodha Kite historical 5-minute candles (index tokens), because yfinance
caps 5m history at 60 days.  Falls back to any cached pickle offline.

Usage::

    python scripts/backtest_orb_post_expiry.py
    python scripts/backtest_orb_post_expiry.py --include-today
    python scripts/backtest_orb_post_expiry.py --validate --no-cache
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import backtest_orb_retest_v2 as engine  # noqa: E402
from src.options_pricing import bs_price  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
OUT_DIR = ROOT / "output"

# NSE index instrument tokens (stable for years).
KITE_TOKENS = {"NIFTY": 256265, "BANKNIFTY": 260105}

# Live strategy gates, mirroring config/options_intraday.yaml.
ORB_GATE = dict(earliest_min=45, cooldown=300, cooldown_rule="loss")   # 10:00, 300s loss-only
RETEST_GATE = dict(earliest_min=30, cooldown=0)                        # 09:45, no cooldown

RETEST_PARAMS = dict(
    or_min_width_atr=3.0,
    max_breakout_body=0.90,
    resumption_buffer_atr=0.20,
    atr_pct_max=0.30,
)

IVS = (0.14, 0.18, 0.22)
BASE_IV = 0.18
FIXED_DTE = 5.0
MIN_BARS_PER_SESSION = 70


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _kite_client():
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from kiteconnect import KiteConnect
    key = os.getenv("ZERODHA_API_KEY")
    token = os.getenv("ZERODHA_ACCESS_TOKEN")
    if not key or not token:
        return None
    kite = KiteConnect(api_key=key)
    kite.set_access_token(token)
    return kite


def _normalise(frame: pd.DataFrame) -> pd.DataFrame:
    """Kite returns lowercase OHLCV columns; the shared engine wants TitleCase."""
    frame = frame.rename(columns={
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
        "date": "Date",
    })
    frame = frame.loc[:, ~frame.columns.duplicated(keep="first")]
    frame["Volume"] = 0.0
    if not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index)
    if frame.index.tz is None:
        frame.index = frame.index.tz_localize(IST)
    else:
        frame.index = frame.index.tz_convert(IST)
    return frame.sort_index()


def fetch_index_5m(
    name: str,
    start: date,
    end: date,
    *,
    use_cache: bool = True,
) -> pd.DataFrame:
    """5-minute OHLC for one index, cached per (name, window)."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cache = OUT_DIR / f"post_expiry_5m_{name}_{start:%Y%m%d}_{end:%Y%m%d}.pkl"
    if use_cache and cache.exists():
        try:
            return _normalise(pd.read_pickle(cache))
        except Exception:
            pass

    kite = _kite_client()
    if kite is None:
        raise RuntimeError("no Zerodha credentials in .env; cannot fetch 5m history")

    token = KITE_TOKENS[name]
    rows: List[Dict[str, Any]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=80), end)
        rows.extend(kite.historical_data(
            token,
            datetime.combine(cursor, datetime.min.time()),
            datetime.combine(chunk_end, datetime.max.time()),
            "5minute",
        ))
        cursor = chunk_end + timedelta(days=1)

    if not rows:
        raise RuntimeError(f"Kite returned no candles for {name}")

    frame = pd.DataFrame(rows)
    frame["date"] = pd.to_datetime(frame["date"])
    if frame["date"].dt.tz is None:
        frame["date"] = frame["date"].dt.tz_localize(IST)
    else:
        frame["date"] = frame["date"].dt.tz_convert(IST)
    frame = frame.drop_duplicates(subset=["date"]).sort_values("date")
    frame = frame.set_index("date")
    frame = frame[(frame.index.time >= pd.Timestamp("09:15").time()) &
                  (frame.index.time <= pd.Timestamp("15:30").time())]
    frame = _normalise(frame)
    try:
        frame.to_pickle(cache)
    except Exception:
        pass
    return frame


def session_dates(frame: pd.DataFrame) -> List[date]:
    counts = frame.groupby(frame.index.date).size()
    return sorted(d for d, n in counts.items() if n >= MIN_BARS_PER_SESSION)


# ---------------------------------------------------------------------------
# Expiry calendar
# ---------------------------------------------------------------------------

def weekly_expiries(sessions: Sequence[date], start: date, end: date) -> List[date]:
    """NIFTY weekly expiry: every Tuesday, shifted back to the prior session
    when the Tuesday itself is a holiday.

    Validated against the options archive: 2026-10-27 / 11-03 / 11-17 were
    Tuesdays, and 2026-11-24 became Monday 2026-11-23.
    """
    session_set = set(sessions)
    out: List[date] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() == 1:  # Tuesday
            shifted = None
            for back in range(0, 5):
                candidate = cursor - timedelta(days=back)
                if candidate in session_set:
                    shifted = candidate
                    break
            if shifted is not None and shifted not in out:
                out.append(shifted)
        cursor += timedelta(days=1)
    return sorted(out)


def expiry_cycles(
    sessions: Sequence[date], expiries: Sequence[date],
) -> List[Dict[str, Any]]:
    """Pair each expiry with the two following trading sessions."""
    index = {d: i for i, d in enumerate(sessions)}
    cycles: List[Dict[str, Any]] = []
    for expiry in expiries:
        pos = index.get(expiry)
        if pos is None:
            continue
        cycle = {"expiry": expiry, "e1": None, "e2": None}
        if pos + 1 < len(sessions):
            cycle["e1"] = sessions[pos + 1]
        if pos + 2 < len(sessions):
            cycle["e2"] = sessions[pos + 2]
        if cycle["e1"] is None:
            continue
        cycles.append(cycle)
    return cycles


def dte_by_day(sessions: Sequence[date], expiries: Sequence[date]) -> Dict[str, float]:
    """Calendar days from each session to the front contract's expiry.

    An expiry-day session trades the contract that dies that afternoon, so its
    DTE is 0 and gets clamped to 1 -- the model never prices a zero-time option.
    """
    out: Dict[str, float] = {}
    for day in sessions:
        nxt = next((e for e in expiries if e >= day), None)
        out[str(day)] = float(max(1, (nxt - day).days)) if nxt else FIXED_DTE
    return out


def listed_future_expiries(after: date) -> List[date]:
    """Forward expiries the exchange has already listed (holiday-shifted).

    The historical window can only reveal expiries that have sessions on both
    sides.  The last cycle's E+1/E+2 sit past that, so read the live chain
    snapshot for the real dates instead of guessing "next Tuesday".
    """
    path = OUT_DIR / "chain_cache" / "chain_NIFTY.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        chain = payload.get("chain") if isinstance(payload.get("chain"), dict) else payload
        raw_list = chain.get("expiries") or []
    except Exception:
        return []
    out: List[date] = []
    for raw in raw_list:
        # The chain snapshot writes "23-Nov-2026".
        stamp = pd.to_datetime(str(raw), format="%d-%b-%Y", errors="coerce")
        if pd.isna(stamp):
            stamp = pd.to_datetime(str(raw), errors="coerce")
        if pd.isna(stamp):
            continue
        if stamp.date() > after:
            out.append(stamp.date())
    return sorted(set(out))


# ---------------------------------------------------------------------------
# Pricing (patched into the shared engine so run_variant stays identical)
# ---------------------------------------------------------------------------

def build_candidate_dte(
    candles: List[Dict[str, Any]],
    index: int,
    kind: str,
    meta: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Copy of engine.build_candidate with a per-session DTE."""
    if index + 1 >= len(candles):
        return None
    entry_bar = candles[index + 1]
    day = entry_bar["timestamp"][:10]
    entry_ts = pd.Timestamp(entry_bar["timestamp"])
    spot = float(entry_bar["open"])
    if spot <= 0:
        return None

    lot, step, vol = meta["lot"], meta["step"], meta["vol"]
    if meta.get("actual_dte", True):
        dte = float(meta["dte_by_day"].get(day, FIXED_DTE))
    else:
        dte = FIXED_DTE

    strike = round(spot / step) * step
    entry_prem = bs_price(spot, strike, max(dte, 1.0) / 365.0, vol, kind)
    lots = engine.lots_for(entry_prem, lot)
    if entry_prem <= 0 or lots <= 0:
        return None

    stop = entry_prem * (1.0 - engine.STOP_FRACTION)
    target = entry_prem * (1.0 + engine.TARGET_FRACTION)
    exit_prem, reason, exit_idx, exit_ts = entry_prem, "eod", index + 1, entry_bar["timestamp"]

    for j in range(index + 2, len(candles)):
        bar = candles[j]
        if bar["timestamp"][:10] != day:
            break
        if pd.Timestamp(bar["timestamp"]).time() > engine.HARD_FLAT:
            break
        px = bs_price(float(bar["close"]), strike, max(dte, 1.0) / 365.0, vol, kind)
        exit_prem, exit_idx, exit_ts = px, j, bar["timestamp"]
        reason = "eod"
        if px <= stop:
            reason = "stop"
            break
        if px >= target:
            reason = "target"
            break

    qty = lot * lots
    gross = (exit_prem - entry_prem) * qty
    costs = engine.option_costs(entry_prem, exit_prem, lot, lots)
    return {
        "day": day,
        "symbol": meta["name"],
        "kind": kind,
        "entry_ts": entry_bar["timestamp"],
        "exit_ts": exit_ts,
        "entry_idx": index + 1,
        "exit_idx": exit_idx,
        "entry_premium": round(entry_prem, 2),
        "exit_premium": round(exit_prem, 2),
        "strike": strike,
        "dte": dte,
        "lots": lots,
        "qty": qty,
        "reason": reason,
        "gross_pnl": round(gross, 2),
        "costs": round(costs, 2),
        "pnl": round(gross - costs, 2),
    }


engine.build_candidate = build_candidate_dte


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------

def cycle_totals(
    trades: Iterable[Dict[str, Any]], cycles: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    by_day: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for t in trades:
        by_day[t["day"]].append(t)

    rows: List[Dict[str, Any]] = []
    for n, cycle in enumerate(cycles, 1):
        row: Dict[str, Any] = {
            "cycle": n,
            "expiry": cycle["expiry"],
            "e1": cycle["e1"],
            "e2": cycle["e2"],
            "e1_trades": 0, "e1_pnl": 0.0,
            "e2_trades": 0, "e2_pnl": 0.0,
            "complete": cycle["e2"] is not None,
        }
        for slot in ("e1", "e2"):
            day = cycle.get(slot)
            if day is None:
                continue
            day_trades = by_day.get(str(day), [])
            row[f"{slot}_trades"] = len(day_trades)
            row[f"{slot}_pnl"] = sum(t["pnl"] for t in day_trades)
        row["total"] = row["e1_pnl"] + row["e2_pnl"]
        rows.append(row)
    return rows


def cycle_stats(rows: Sequence[Dict[str, Any]], label: str) -> Dict[str, Any]:
    complete = [r for r in rows if r["complete"]]
    totals = [r["total"] for r in complete]
    positive = sum(1 for v in totals if v > 0)
    best = max(totals) if totals else 0.0
    worst = min(totals) if totals else 0.0
    return {
        "label": label,
        "cycles": len(complete),
        "positive_cycles": positive,
        "net": sum(totals),
        "avg_cycle": (sum(totals) / len(totals)) if totals else 0.0,
        "best_cycle": best,
        "worst_cycle": worst,
    }


def fmt_inr(value: float) -> str:
    return f"{value:+,.0f}"


def print_cycle_table(rows: Sequence[Dict[str, Any]], label: str) -> None:
    print(f"\n{label}")
    print("-" * 104)
    print(f"{'#':>3} {'expiry':<12} {'E+1':<11} {'trd':>4} {'P&L':>11} "
          f"{'E+2':<11} {'trd':>4} {'P&L':>11} {'cycle':>11}")
    print("-" * 104)
    for r in rows:
        e2 = str(r["e2"]) if r["e2"] else "-- pending --"
        print(f"{r['cycle']:>3} {str(r['expiry']):<12} {str(r['e1']):<11} "
              f"{r['e1_trades']:>4} {fmt_inr(r['e1_pnl']):>11} "
              f"{e2:<11} {r['e2_trades']:>4} {fmt_inr(r['e2_pnl']):>11} "
              f"{fmt_inr(r['total']):>11}")
    print("-" * 104)


def print_month_table(trades: Sequence[Dict[str, Any]], label: str) -> None:
    by_month: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for t in trades:
        by_month[t["day"][:7]].append(t)
    print(f"\n{label}")
    print("-" * 74)
    print(f"{'month':<9}{'trades':>8}{'win%':>8}{'net Rs':>14}{'PF':>7}{'avg':>10}")
    print("-" * 74)
    for month in sorted(by_month):
        ts = by_month[month]
        wins = [t for t in ts if t["pnl"] > 0]
        gw = sum(t["pnl"] for t in wins)
        gl = -sum(t["pnl"] for t in ts if t["pnl"] <= 0)
        pf = gw / gl if gl else float("inf")
        pfs = "inf" if pf == float("inf") else f"{pf:.2f}"
        print(f"{month:<9}{len(ts):>8}{len(wins)/len(ts)*100:>7.1f}%"
              f"{sum(t['pnl'] for t in ts):>14,.0f}{pfs:>7}"
              f"{sum(t['pnl'] for t in ts)/len(ts):>10,.0f}")
    print("-" * 74)


def day_label(day: str, cycles: Sequence[Dict[str, Any]]) -> str:
    for n, c in enumerate(cycles, 1):
        if str(c["e1"]) == day:
            return f"E+1 of #{n}"
        if str(c["e2"]) == day:
            return f"E+2 of #{n}"
    return ""


# ---------------------------------------------------------------------------
# Archive cross-check (synthetic BS vs real traded premium)
# ---------------------------------------------------------------------------

def validate_against_archive(
    frames: Dict[str, pd.DataFrame],
    sessions: Sequence[date],
    expiries: Sequence[date],
    *,
    limit: int = 6000,
) -> Optional[pd.DataFrame]:
    """Compare synthetic ATM BS price with real archived option candles."""
    path = Path(os.path.expandvars(
        r"%LOCALAPPDATA%\TRIO\options\options_archive.sqlite3"))
    if not path.exists():
        return None

    import sqlite3

    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    rows: List[pd.DataFrame] = []
    for name, frame in frames.items():
        step = engine.SYMBOLS["^NSEI" if name == "NIFTY" else "^NSEBANK"]["step"]
        window = [e for e in expiries
                  if str(sessions[0]) <= str(e) <= str(sessions[-1])]
        if not window:
            continue
        sql = """
            select k.expiry_date, k.strike, k.option_type, c.timestamp_utc, c.close
            from candles c
            join contracts k on k.contract_id = c.contract_id
            where k.underlying = ? and k.expiry_date between ? and ?
              and c.interval = '5minute'
        """
        raw = pd.read_sql(sql, con, params=(name, str(window[0]), str(window[-1])))
        if raw.empty:
            continue
        raw["ts"] = (pd.to_datetime(raw["timestamp_utc"], utc=True)
                     .dt.tz_convert(IST))
        raw["close"] = pd.to_numeric(raw["close"], errors="coerce")
        raw = raw.dropna(subset=["close"])
        raw["strike"] = pd.to_numeric(raw["strike"], errors="coerce")

        spot = frame["Close"].copy()
        spot.index = spot.index.tz_convert(IST)
        spot = spot[~spot.index.duplicated(keep="last")]
        spot_map = {ts: float(v) for ts, v in spot.items()}
        session_set = set(sessions)

        for expiry_str, group in raw.groupby("expiry_date"):
            expiry = date.fromisoformat(str(expiry_str))
            prices = {
                (ts, int(strike), str(kind)): float(close)
                for ts, strike, kind, close in zip(
                    group["ts"], group["strike"], group["option_type"], group["close"])
                if pd.notna(strike)
            }
            for ts in sorted(group["ts"].unique())[::17]:
                day = ts.date()
                dte = (expiry - day).days
                if dte < 1 or day not in session_set:
                    continue
                s = spot_map.get(ts)
                if not s:
                    continue
                strike = round(s / step) * step
                for kind in ("CE", "PE"):
                    real = prices.get((ts, int(strike), kind))
                    if not real or real <= 0:
                        continue
                    synth = bs_price(s, strike, dte / 365.0, BASE_IV, kind)
                    if synth <= 0:
                        continue
                    rows.append(pd.DataFrame([{
                        "underlying": name, "dte": dte, "kind": kind,
                        "real": real, "synth": synth,
                    }]))
    con.close()
    if not rows:
        return None
    out = pd.concat(rows, ignore_index=True)
    if len(out) > limit:
        out = out.sample(limit, random_state=7)
    return out


def print_validation(frame: pd.DataFrame) -> None:
    print("\nARCHIVE CROSS-CHECK  (synthetic ATM BS @18% IV vs real traded close)")
    print("-" * 78)
    print(f"{'DTE':<6}{'n':>7}{'median real':>14}{'median synth':>14}"
          f"{'real/synth':>12}{'med |err|':>12}")
    print("-" * 78)
    for dte, group in frame.groupby("dte"):
        ratio = (group["real"] / group["synth"]).median()
        err = ((group["real"] - group["synth"]).abs() / group["synth"]).median() * 100
        print(f"{int(dte):<6}{len(group):>7}{group['real'].median():>14.2f}"
              f"{group['synth'].median():>14.2f}{ratio:>12.2f}{err:>11.1f}%")
    ratio = (frame["real"] / frame["synth"]).median()
    err = ((frame["real"] - frame["synth"]).abs() / frame["synth"]).median() * 100
    print("-" * 78)
    print(f"overall n={len(frame)}  real/synth {ratio:.2f}  "
          f"median absolute error {err:.1f}%")
    print("-" * 78)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-07-06",
                    help="first date to fetch (warmup); analysis starts at the first expiry")
    ap.add_argument("--end", default=None, help="last date to fetch (default: today)")
    ap.add_argument("--include-today", action="store_true",
                    help="include an incomplete final session (today's partial bars)")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--validate", action="store_true",
                    help="cross-check synthetic prices against the option archive")
    ap.add_argument("--target-sessions", type=int, default=2,
                    help="sessions after each expiry to test (default 2)")
    ap.add_argument("--json-out", default=str(OUT_DIR / "post_expiry_orb.json"))
    args = ap.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else datetime.now(IST).date()

    frames: Dict[str, pd.DataFrame] = {}
    for name in KITE_TOKENS:
        frames[name] = fetch_index_5m(name, start, end, use_cache=not args.no_cache)
        print(f"{name}: {len(frames[name]):,} bars "
              f"{frames[name].index.min():%Y-%m-%d} .. {frames[name].index.max():%Y-%m-%d}")

    sessions = session_dates(frames["NIFTY"])
    if not args.include_today:
        yesterday = datetime.now(IST).date()
        sessions = [d for d in sessions if d < yesterday]
    if len(sessions) < 20:
        print("Not enough sessions in the fetched window.")
        return 1

    expiries = weekly_expiries(sessions, sessions[0], sessions[-1])
    future = listed_future_expiries(sessions[-1])
    cycles = expiry_cycles(sessions, expiries)

    print(f"\nSessions  : {sessions[0]} .. {sessions[-1]}  ({len(sessions)})")
    print(f"Expiries  : {len(expiries)}  ({expiries[0]} .. {expiries[-1]})")
    print(f"Cycles    : {len(cycles)}")

    dte_map = dte_by_day(sessions, list(expiries) + future)

    candles_by: Dict[str, List[Dict[str, Any]]] = {}
    by_date_by: Dict[str, Dict[str, List[int]]] = {}
    meta_by: Dict[str, Dict[str, Any]] = {}
    signals: Dict[str, Dict[str, Dict[int, str]]] = {"orb": {}, "orb_retest": {}}

    for key, spec in engine.SYMBOLS.items():
        name = spec["name"]
        candles = engine.to_candles(frames[name], name)
        candles_by[name] = candles
        by_date: Dict[str, List[int]] = defaultdict(list)
        for i, c in enumerate(candles):
            by_date[c["timestamp"][:10]].append(i)
        by_date_by[name] = by_date
        meta_by[name] = dict(spec, vol=BASE_IV, dte_by_day=dte_map, actual_dte=True)
        signals["orb"][name] = engine.collect_orb_signals(
            candles, name, spec["lot"], spec["step"])
        signals["orb_retest"][name] = engine.collect_retest_signals(
            candles, name, spec["lot"], spec["step"], **RETEST_PARAMS)
        print(f"{name}: ORB signals {len(signals['orb'][name])}, "
              f"retest signals {len(signals['orb_retest'][name])}")

    post_days = {str(d) for c in cycles for d in (c["e1"], c["e2"]) if d}
    all_days = {str(d) for d in sessions if str(d) >= str(cycles[0]["expiry"])}

    def run(strategy: str, days: set, vol: float, actual_dte: bool) -> List[Dict[str, Any]]:
        gate = ORB_GATE if strategy == "orb" else RETEST_GATE
        out: List[Dict[str, Any]] = []
        for name, candles in candles_by.items():
            meta = dict(meta_by[name], vol=vol, actual_dte=actual_dte)
            subset = {d: idx for d, idx in by_date_by[name].items() if d in days}
            out += engine.run_variant(
                candles, subset, signals[strategy][name], meta,
                strategy=strategy, max_trades_per_day=10, **gate)
        return out

    results: Dict[str, Dict[str, Any]] = {}
    for strategy in ("orb", "orb_retest"):
        for iv in IVS:
            results[f"{strategy}|post|iv{int(iv*100)}"] = run(strategy, post_days, iv, True)
        results[f"{strategy}|post|fixed5"] = run(strategy, post_days, BASE_IV, False)
        results[f"{strategy}|all|iv{int(BASE_IV*100)}"] = run(
            strategy, all_days, BASE_IV, True)

    # ---------------- per-cycle tables ----------------
    for strategy, title in (("orb", "ORB"), ("orb_retest", "ORB-RETEST")):
        trades = results[f"{strategy}|post|iv{int(BASE_IV*100)}"]
        print_cycle_table(cycle_totals(trades, cycles), f"{title}  post-expiry cycles (18% IV)")

    # ---------------- aggregate ----------------
    print("\nPOST-EXPIRY AGGREGATE  (after real costs)")
    print("-" * 118)
    print(f"{'Variant':<30}{'Trades':>7}{'Win%':>7}{'Net Rs':>13}{'PF':>7}"
          f"{'MaxDD':>11}{'Avg':>9}{'t':>7}{'WinDays':>10}")
    print("-" * 118)
    for key in sorted(results):
        strategy, scope, variant = key.split("|")
        r = engine.stats(results[key], f"{strategy} {scope} {variant}")
        pf = "inf" if r["profit_factor"] == float("inf") else f"{r['profit_factor']:.2f}"
        print(f"{r['label']:<30}{r['trades']:>7}{r['win_rate']:>6.1f}%"
              f"{r['net']:>13,.0f}{pf:>7}{r['max_dd']:>11,.0f}{r['avg']:>9,.0f}"
              f"{r['t_stat']:>7.2f}{r['win_days']:>4}/{r['sessions']:<5}")
    print("-" * 118)

    # ---------------- cycle-level ----------------
    print("\nCYCLE-LEVEL  (E+1 + E+2 combined, 18% IV)")
    print("-" * 76)
    print(f"{'Variant':<26}{'cycles':>8}{'positive':>10}{'net Rs':>14}"
          f"{'avg/cycle':>12}{'worst':>11}")
    print("-" * 76)
    cycle_rows: Dict[str, List[Dict[str, Any]]] = {}
    for strategy in ("orb", "orb_retest"):
        rows = cycle_totals(results[f"{strategy}|post|iv{int(BASE_IV*100)}"], cycles)
        cycle_rows[strategy] = rows
        s = cycle_stats(rows, strategy)
        print(f"{s['label']:<26}{s['cycles']:>8}{s['positive_cycles']:>6}/{s['cycles']:<3}"
              f"{s['net']:>14,.0f}{s['avg_cycle']:>12,.0f}{s['worst_cycle']:>11,.0f}")
    print("-" * 76)

    # ---------------- month + day ----------------
    for strategy, title in (("orb", "ORB"), ("orb_retest", "ORB-RETEST")):
        print_month_table(results[f"{strategy}|post|iv{int(BASE_IV*100)}"],
                          f"{title} month-wise (post-expiry days, 18% IV)")

    for strategy, title in (("orb", "ORB"), ("orb_retest", "ORB-RETEST")):
        trades = results[f"{strategy}|post|iv{int(BASE_IV*100)}"]
        by_day: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for t in trades:
            by_day[t["day"]].append(t)
        print(f"\n{title} day-wise (post-expiry sessions, 18% IV)")
        print("-" * 70)
        print(f"{'day':<12}{'slot':<11}{'trades':>7}{'win':>5}{'net Rs':>14}{'DTE':>6}")
        print("-" * 70)
        for c in cycles:
            for slot in ("e1", "e2"):
                day = c.get(slot)
                if not day:
                    continue
                ts = by_day.get(str(day), [])
                wins = sum(1 for t in ts if t["pnl"] > 0)
                print(f"{str(day):<12}{day_label(str(day), cycles):<11}"
                      f"{len(ts):>7}{wins:>5}{sum(t['pnl'] for t in ts):>14,.0f}"
                      f"{dte_map.get(str(day), 0):>6.0f}")
        print("-" * 70)

    # ---------------- diagnostics ----------------
    base = results[f"orb|post|iv{int(BASE_IV*100)}"]
    if base:
        dtes = sorted({t["dte"] for t in base})
        print("\nDTE SPLIT  (ORB, 18% IV)")
        print("-" * 66)
        print(f"{'DTE':<6}{'trades':>8}{'win%':>8}{'net Rs':>14}{'avg':>11}{'stop%':>8}")
        print("-" * 66)
        for dte in dtes:
            ts = [t for t in base if t["dte"] == dte]
            wins = sum(1 for t in ts if t["pnl"] > 0)
            stops = sum(1 for t in ts if t["reason"] == "stop")
            print(f"{int(dte):<6}{len(ts):>8}{wins/len(ts)*100:>7.1f}%"
                  f"{sum(t['pnl'] for t in ts):>14,.0f}"
                  f"{sum(t['pnl'] for t in ts)/len(ts):>11,.0f}{stops/len(ts)*100:>7.0f}%")
        print("-" * 66)
        reasons = defaultdict(int)
        for t in base:
            reasons[t["reason"]] += 1
        print("exit mix:", dict(reasons))

    # ---------------- archive validation ----------------
    if args.validate:
        frame = validate_against_archive(frames, sessions, expiries)
        if frame is not None:
            print_validation(frame)

    # ---------------- JSON ----------------
    payload = {
        "window": {"start": str(sessions[0]), "end": str(sessions[-1])},
        "sessions": len(sessions),
        "expiries": [str(e) for e in expiries],
        "cycles": [
            {"cycle": c["cycle"], "expiry": str(c["expiry"]),
             "e1": str(c["e1"]) if c["e1"] else None,
             "e2": str(c["e2"]) if c["e2"] else None,
             "e1_pnl": round(c["e1_pnl"], 2), "e2_pnl": round(c["e2_pnl"], 2),
             "total": round(c["total"], 2), "complete": c["complete"]}
            for c in cycle_rows["orb"]
        ],
        "aggregate": {
            key: {k: (None if isinstance(v, float) and v == float("inf") else v)
                  for k, v in engine.stats(trades, key).items()}
            for key, trades in results.items()
        },
        "trades": {
            key: [
                {
                    "day": t["day"], "symbol": t["symbol"], "kind": t["kind"],
                    "entry_ts": str(t["entry_ts"]), "exit_ts": str(t["exit_ts"]),
                    "dte": t["dte"], "entry_premium": t["entry_premium"],
                    "exit_premium": t["exit_premium"], "lots": t["lots"],
                    "qty": t["qty"], "reason": t["reason"], "pnl": t["pnl"],
                    "costs": t["costs"],
                }
                for t in trades
            ]
            for key, trades in results.items()
        },
        "settings": {
            "pricing": "Black-Scholes ATM, actual calendar DTE, IV sweep 14/18/22%",
            "stop": engine.STOP_FRACTION, "target": engine.TARGET_FRACTION,
            "hard_flat": str(engine.HARD_FLAT), "sizing": "risk 0.7% / max Rs 18k premium",
            "orb_gate": ORB_GATE, "retest_gate": RETEST_GATE,
        },
    }
    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json_out).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nJSON written to {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
