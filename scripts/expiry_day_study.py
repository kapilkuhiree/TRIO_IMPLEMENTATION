"""Expiry-day study for NIFTY (corrected calendar + 0-DTE backtest).

Why this exists
---------------
`C:\\Users\\Kapil Kuhire\\Desktop\\nifty_charts.py` built its expiry list from
``weekday() == 3`` (Thursday).  NIFTY weekly options expire on **Tuesday**
(holiday-shifted), which `scripts/backtest_orb_post_expiry.py` already
encodes and which the live chain snapshot confirms:

    output/chain_cache/chain_NIFTY.json -> 23-Nov-2026, 01-Dec-2026, ...

Every chart that script produced was therefore two days after the real
expiry -- an E+2 session, not an expiry session.  This module redoes the work
on the real calendar and then asks the only question that matters: is there an
expiry-day regularity we can turn into a rule?

What is measured
----------------
1. Calendar audit against the live chain snapshot.
2. Expiry day vs the *mean of the same week's other sessions* (paired control,
   so a bull/bear drift cannot masquerade as an expiry effect).
3. Pin test: does price converge on the nearest 50-point strike into the close?
4. Opening-range breakouts: do they hold to the close on expiry day?
5. 0-DTE option backtest: the live 20% stop / 30% target rules, replayed on the
   real 13:00-15:10 index paths at DTE 0 / 1 / 5 with an IV sweep.

Data: Zerodha Kite 5-minute NIFTY index candles, cached under
``output/expiry_study/``.

Usage::

    python scripts/expiry_day_study.py
    python scripts/expiry_day_study.py --refresh
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from backtest_orb_post_expiry import KITE_TOKENS, _kite_client, _normalise  # noqa: E402
from src.options_pricing import bs_price  # noqa: E402

OUT_DIR = ROOT / "output" / "expiry_study"
CACHE = OUT_DIR / "nifty_5m_2019_2026.pkl"
DEFAULT_START = date(2019, 1, 1)

# NSE moved the NIFTY weekly expiry from Thursday to Tuesday in 2025.  The
# live chain snapshot only covers future dates, so the boundary itself cannot
# be read from local data -- it is stated here and every table is produced
# per-era, so no conclusion depends on pooling across it.
EXPIRY_SWITCH = date(2025, 9, 1)

SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
ENTRY_TIME = time(13, 0)
# The live book squares off with ``min_entry_runway_minutes: 10``.
SQUAREOFF_TIME = time(15, 10)

MIN_BARS_PER_SESSION = 60
LOT_SIZE = 65
STOP_FRACTION = 0.20
TARGET_FRACTION = 0.30
IVS = (0.10, 0.14, 0.18)
DTE_CASES = ((0.0, "0 DTE (expiry day)"), (1.0, "1 DTE"), (5.0, "5 DTE"))


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def load_frame(start: date, end: date, *, refresh: bool = False) -> pd.DataFrame:
    """5-minute NIFTY index OHLC in IST, cached at ``output/expiry_study``."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if CACHE.exists() and not refresh:
        return _normalise(pd.read_pickle(CACHE))

    kite = _kite_client()
    if kite is None:
        raise RuntimeError("no Zerodha credentials in .env; cannot fetch 5m history")

    rows: List[Dict[str, object]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=80), end)
        rows.extend(kite.historical_data(
            KITE_TOKENS["NIFTY"],
            datetime.combine(cursor, datetime.min.time()),
            datetime.combine(chunk_end, datetime.max.time()),
            "5minute",
        ))
        cursor = chunk_end + timedelta(days=1)

    frame = pd.DataFrame(rows)
    if frame.empty:
        raise RuntimeError("Kite returned no NIFTY candles")
    frame["date"] = pd.to_datetime(frame["date"])
    if frame["date"].dt.tz is None:
        frame["date"] = frame["date"].dt.tz_localize("Asia/Kolkata")
    else:
        frame["date"] = frame["date"].dt.tz_convert("Asia/Kolkata")
    frame = (frame.drop_duplicates(subset=["date"])
                  .sort_values("date")
                  .set_index("date"))
    frame = frame[(frame.index.time >= SESSION_OPEN)
                  & (frame.index.time <= SESSION_CLOSE)]
    frame = _normalise(frame)
    frame.to_pickle(CACHE)
    return frame


def clean_sessions(frame: pd.DataFrame) -> List[date]:
    counts = frame.groupby(frame.index.date).size()
    return sorted(d for d, n in counts.items() if n >= MIN_BARS_PER_SESSION)


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def expiry_weekday(day: date) -> int:
    """NIFTY weekly expiry weekday in force on ``day`` (Mon=0)."""
    return 1 if day >= EXPIRY_SWITCH else 3   # Tuesday / Thursday


def weekly_expiries(
    sessions: Sequence[date], start: date, end: date,
) -> List[Tuple[date, date]]:
    """``(traded_expiry, nominal_weekday)`` pairs, holiday-shifted backwards."""
    session_set = set(sessions)
    out: List[Tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() == expiry_weekday(cursor):
            for back in range(0, 6):
                candidate = cursor - timedelta(days=back)
                if candidate in session_set:
                    if not out or out[-1][0] != candidate:
                        out.append((candidate, cursor))
                    break
        cursor += timedelta(days=1)
    return out


def monthly_expiries(
    sessions: Sequence[date], start: date, end: date,
) -> List[Tuple[date, date]]:
    """Last expiry-weekday of each month, holiday-shifted like the weekly."""
    session_set = set(sessions)
    out: List[Tuple[date, date]] = []
    for year in range(start.year, end.year + 1):
        for month in range(1, 13):
            last_day = (date(year + (month == 12), month % 12 + 1, 1)
                        - timedelta(days=1))
            nominal = last_day
            while nominal.weekday() != expiry_weekday(nominal):
                nominal -= timedelta(days=1)
            if not (start <= nominal <= end):
                continue
            for back in range(0, 6):
                candidate = nominal - timedelta(days=back)
                if candidate in session_set:
                    out.append((candidate, nominal))
                    break
    return out


def chain_snapshot_expiries() -> List[date]:
    path = ROOT / "output" / "chain_cache" / "chain_NIFTY.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        chain = payload.get("chain") if isinstance(payload.get("chain"), dict) else payload
        raw = chain.get("expiries") or []
    except Exception:
        return []
    out: List[date] = []
    for item in raw:
        stamp = pd.to_datetime(str(item), format="%d-%b-%Y", errors="coerce")
        if not pd.isna(stamp):
            out.append(stamp.date())
    return sorted(out)


# ---------------------------------------------------------------------------
# Session features
# ---------------------------------------------------------------------------

def session_features(frame: pd.DataFrame) -> pd.DataFrame:
    """One row per session: shape, afternoon split, and late-session drift."""
    rows: List[Dict[str, object]] = []
    prev_close: Optional[float] = None
    for day, group in frame.groupby(frame.index.date):
        if len(group) < MIN_BARS_PER_SESSION:
            continue
        times = np.array([stamp.time() for stamp in group.index])
        close_vals = group["Close"].to_numpy(dtype=float)

        def close_at(mark: time) -> float:
            mask = times <= mark
            return float(close_vals[mask][-1]) if mask.any() else np.nan

        open_px = float(group["Open"].iloc[0])
        close_px = float(close_vals[-1])
        high, low = float(group["High"].max()), float(group["Low"].min())
        span = high - low
        c13, c1430, c15 = close_at(ENTRY_TIME), close_at(time(14, 30)), close_at(time(15, 0))
        am_mask = (times > SESSION_OPEN) & (times <= ENTRY_TIME)
        pm_mask = times > ENTRY_TIME
        am_range = ((float(group["High"][am_mask].max()) - float(group["Low"][am_mask].min()))
                    / open_px * 100) if am_mask.any() else np.nan
        pm_range = ((float(group["High"][pm_mask].max()) - float(group["Low"][pm_mask].min()))
                    / open_px * 100) if pm_mask.any() else np.nan
        rows.append(dict(
            date=day,
            dow=pd.Timestamp(day).dayofweek,
            open=open_px, close=close_px, high=high, low=low,
            range_pct=span / open_px * 100,
            ret_pct=(close_px - open_px) / open_px * 100,
            gap_pct=(open_px / prev_close - 1) * 100 if prev_close else np.nan,
            morning_pct=(c13 - open_px) / open_px * 100,
            afternoon_pct=(close_px - c13) / c13 * 100,
            late_pct=(close_px - c1430) / c1430 * 100,
            last30_pct=(close_px - c15) / c15 * 100,
            close_pos=(close_px - low) / span if span > 0 else np.nan,
            am_range_pct=am_range, pm_range_pct=pm_range,
            pm_share=pm_range / (am_range + pm_range) if (am_range + pm_range) > 0 else np.nan,
            pin_1300=abs(c13 - 50 * round(c13 / 50)) / span if span > 0 else np.nan,
            pin_1500=abs(c15 - 50 * round(c15 / 50)) / span if span > 0 else np.nan,
            pin_1525=abs(close_px - 50 * round(close_px / 50)) / span if span > 0 else np.nan,
        ))
        prev_close = close_px
    out = pd.DataFrame(rows).set_index("date")
    out.index = pd.to_datetime(out.index)
    out["era"] = np.where(out.index < pd.Timestamp(EXPIRY_SWITCH), "Thu", "Tue")
    return out


def tag_expiry(features: pd.DataFrame, weekly: Sequence[Tuple[date, date]]) -> pd.DataFrame:
    traded = {pd.Timestamp(day) for day, _ in weekly}
    out = features.copy()
    out["is_expiry"] = out.index.isin(traded)
    # Inside one era every expiry weekday is the same, which is what makes the
    # weekday comparison equivalent to an expiry-day comparison.
    return out


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def paired_table(features: pd.DataFrame) -> pd.DataFrame:
    """Expiry day minus the mean of the other sessions in the same week."""
    metrics = ["range_pct", "ret_pct", "close_pos", "last30_pct",
               "pm_share", "morning_pct", "afternoon_pct"]
    out = features.copy()
    out["abs_ret"] = out["ret_pct"].abs()
    metrics.append("abs_ret")
    out["week"] = out.index.to_period("W")
    rows = []
    for era, group in out.groupby("era"):
        exp = group[group.is_expiry]
        control = group[~group.is_expiry].groupby("week")[metrics].mean()
        joined = exp.join(control, on="week", rsuffix="_ctl")
        for metric in metrics:
            diff = (joined[metric] - joined[f"{metric}_ctl"]).dropna()
            if len(diff) < 20:
                continue
            t = diff.mean() / (diff.std(ddof=1) / np.sqrt(len(diff)))
            rows.append(dict(era=era, metric=metric, n=len(diff),
                             expiry=joined[metric].mean(),
                             week_other=joined[f"{metric}_ctl"].mean(),
                             diff=diff.mean(), t=t))
    return pd.DataFrame(rows)


def orb_table(frame: pd.DataFrame, features: pd.DataFrame) -> pd.DataFrame:
    """Opening-range breakouts: did the break hold to the close?"""
    rows = []
    for day, group in frame.groupby(frame.index.date):
        if len(group) < MIN_BARS_PER_SESSION:
            continue
        times = np.array([stamp.time() for stamp in group.index])
        or_mask = (times >= SESSION_OPEN) & (times <= time(9, 45))
        if not or_mask.any():
            continue
        or_hi = float(group["High"][or_mask].max())
        or_lo = float(group["Low"][or_mask].min())
        if or_hi <= or_lo:
            continue
        after = group[times > time(9, 45)]
        if after.empty:
            continue
        a_times = np.array([stamp.time() for stamp in after.index])
        eligible = a_times >= time(10, 0)
        closes = after["Close"].to_numpy(dtype=float)
        up_idx = np.argmax((closes > or_hi) & eligible) if ((closes > or_hi) & eligible).any() else -1
        dn_idx = np.argmax((closes < or_lo) & eligible) if ((closes < or_lo) & eligible).any() else -1
        if up_idx < 0 and dn_idx < 0:
            continue
        if up_idx < 0:
            side, k = -1, dn_idx
        elif dn_idx < 0:
            side, k = 1, up_idx
        else:
            side, k = (1, up_idx) if up_idx < dn_idx else (-1, dn_idx)
        level = or_hi if side > 0 else or_lo
        rest = after.iloc[k:]
        close_px = float(closes[-1])
        held = close_px > level if side > 0 else close_px < level
        rows.append(dict(date=pd.Timestamp(day), side=side, held=bool(held),
                         edge_pct=((close_px - level) if side > 0 else (level - close_px))
                         / level * 100))
    out = pd.DataFrame(rows).set_index("date")
    out["era"] = np.where(out.index < pd.Timestamp(EXPIRY_SWITCH), "Thu", "Tue")
    out = out.join(features[["is_expiry"]])
    return out


def dte_backtest(
    frame: pd.DataFrame,
    features: pd.DataFrame,
    *,
    entry_time: time = ENTRY_TIME,
    dte_cases: Sequence[Tuple[float, str]] = DTE_CASES,
    ivs: Sequence[float] = IVS,
    cost_points: float = 0.0,
) -> pd.DataFrame:
    """Replay the live 20% stop / 30% target on real paths at DTE 0 / 1 / 5.

    Entry is the ``entry_time`` bar close, direction is the move into it,
    contract is the ATM strike, exit is the square-off time.  The index path is
    identical across DTE cases -- only time-to-expiry changes -- so the
    difference in the table is purely the theta/delta profile.

    ``cost_points`` is charged round-trip on the premium (one point is roughly
    one tick of NIFTY ATM bid-ask plus the observed stop slippage on
    2026-11-16).
    """
    session_rows: Dict[pd.Timestamp, pd.DataFrame] = {}
    entry_hour = entry_time.hour + entry_time.minute / 60.0
    for day, group in frame.groupby(frame.index.date):
        if len(group) < MIN_BARS_PER_SESSION:
            continue
        times = np.array([stamp.time() for stamp in group.index])
        window = (times >= entry_time) & (times <= SQUAREOFF_TIME)
        if window.sum() < 5:
            continue
        session_rows[pd.Timestamp(day)] = group[window]

    rows: List[Dict[str, object]] = []
    end_hour = SQUAREOFF_TIME.hour + SQUAREOFF_TIME.minute / 60.0
    for day, window in session_rows.items():
        if day not in features.index:
            continue
        feat = features.loc[day]
        times = np.array([stamp.time() for stamp in window.index])
        pre = window[times <= entry_time]
        spot = float(pre["Close"].iloc[-1])
        reference = float(feat["open"])
        move_pct = (spot - reference) / reference * 100
        if not np.isfinite(move_pct) or move_pct == 0:
            continue
        kind = "CE" if move_pct > 0 else "PE"
        strike = 50 * round(spot / 50)
        marks = [(stamp.hour + stamp.minute / 60.0, float(px))
                 for stamp, px in zip(window.index, window["Close"])]
        for dte, _label in dte_cases:
            for iv in ivs:
                entry_years = ((end_hour - entry_hour) / 24.0 + dte) / 365.0
                entry_px = bs_price(spot, strike, entry_years, iv, kind)
                if entry_px <= 0:
                    continue
                stop = entry_px * (1 - STOP_FRACTION)
                target = entry_px * (1 + TARGET_FRACTION)
                exit_px, reason = None, "square-off"
                for hour, price in marks[1:]:
                    years = ((end_hour - hour) / 24.0 + dte) / 365.0
                    value = bs_price(price, strike, years, iv, kind)
                    if value <= stop:            # pessimistic: stop first
                        exit_px, reason = stop, "stop"
                        break
                    if value >= target:
                        exit_px, reason = target, "target"
                        break
                if exit_px is None:
                    hour, price = marks[-1]
                    years = ((end_hour - hour) / 24.0 + dte) / 365.0
                    exit_px = bs_price(price, strike, years, iv, kind)
                gross = exit_px - entry_px
                rows.append(dict(
                    date=day, era=feat["era"], is_expiry=bool(feat["is_expiry"]),
                    entry_time=entry_time.strftime("%H:%M"),
                    dte=dte, iv=iv, kind=kind, entry=entry_px, exit=exit_px,
                    reason=reason, ret=gross / entry_px,
                    pnl=float(gross * LOT_SIZE),
                    pnl_net=float((gross - cost_points) * LOT_SIZE),
                ))
    return pd.DataFrame(rows)


def entry_time_sweep(frame: pd.DataFrame, features: pd.DataFrame,
                     *, costs: Sequence[float] = (0.0, 1.0, 2.0)) -> pd.DataFrame:
    """0-DTE only: does an earlier entry buy enough time to survive costs?"""
    rows = []
    for mark in ("09:45", "11:00", "13:00"):
        entry = datetime.strptime(mark, "%H:%M").time()
        sim = dte_backtest(frame, features, entry_time=entry,
                          dte_cases=((0.0, "0 DTE"),), ivs=(0.14,), cost_points=0.0)
        sim = sim[sim.is_expiry]
        if sim.empty:
            continue
        row = dict(entry=mark, n=len(sim),
                   win=(sim.ret > 0).mean() * 100,
                   gross_ret=sim.ret.mean() * 100,
                   gross_pnl=sim.pnl.mean(),
                   avg_premium=sim.entry.mean())
        for cost in costs:
            if cost == 0:
                continue
            row[f"net_pnl@{cost:g}pt"] = (sim.pnl - cost * LOT_SIZE).mean()
        rows.append(row)
    return pd.DataFrame(rows)


def five_min_range(frame: pd.DataFrame, features: pd.DataFrame) -> pd.DataFrame:
    """Typical 5-minute bar range, so the stop/target distances can be judged."""
    frame = frame.copy()
    frame["bar_range"] = (frame["High"] - frame["Low"])
    per_session = frame.groupby(frame.index.date)["bar_range"].mean()
    per_session.index = pd.to_datetime(per_session.index)
    out = features[["era", "is_expiry"]].join(per_session.rename("bar_range"))
    return (out.dropna()
               .groupby(["era", "is_expiry"])["bar_range"]
               .agg(n="size", mean="mean", median="median")
               .reset_index())


def points_to_hit(spot: float, dte: float, iv: float) -> Tuple[float, float]:
    """Index move needed to hit -20% / +30% on an ATM option at ``dte``."""
    strike = 50 * round(spot / 50)
    years = (dte + 0.104) / 365.0
    base = bs_price(spot, strike, years, iv, "CE")
    if base <= 0:
        return (np.nan, np.nan)
    out = []
    for mult in (1 - STOP_FRACTION, 1 + TARGET_FRACTION):
        lo, hi = 0.0, spot * 0.05
        for _ in range(60):
            mid = (lo + hi) / 2
            up_down = bs_price(spot + mid, strike, years, iv, "CE") - base if mult > 1 else base - bs_price(spot - mid, strike, years, iv, "CE")
            if up_down >= base * abs(mult - 1):
                hi = mid
            else:
                lo = mid
        out.append(hi)
    return (out[0], out[1])


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _fmt(df: pd.DataFrame, cols: Sequence[str], pct: Sequence[str] = ()) -> str:
    view = df.loc[:, list(cols)].copy()
    for col in pct:
        if col in view:
            view[col] = view[col].map(lambda v: f"{v*100:+.2f}%")
    return view.to_string(index=False)


def build_report(frame: pd.DataFrame, features: pd.DataFrame,
                 weekly: Sequence[Tuple[date, date]],
                 monthly: Sequence[Tuple[date, date]]) -> str:
    lines: List[str] = []
    add = lines.append

    add("# NIFTY expiry-day study")
    add("")
    add(f"Data: {len(frame):,} 5-minute bars, {frame.index[0]:%Y-%m-%d} "
        f"to {frame.index[-1]:%Y-%m-%d}, {len(features)} clean sessions.")
    add("")
    add("## 1. Calendar audit")
    add("")
    add(f"Weekly expiries resolved: {len(weekly)}  |  monthly: {len(monthly)}")
    add("")
    add("Last resolved weekly expiries:")
    add("")
    add("```")
    add("traded      day  nominal")
    for traded, nominal in weekly[-6:]:
        mark = "" if traded == nominal else "  <- holiday-shifted"
        add(f"{traded}  {traded:%a}  {nominal} ({nominal:%a}){mark}")
    add("```")
    snapshot = chain_snapshot_expiries()
    if snapshot:
        add("")
        add("Live chain snapshot (`output/chain_cache/chain_NIFTY.json`):")
        add("")
        add("```")
        for day in snapshot[:6]:
            add(f"{day}  {day:%a}")
        add("```")
        add("")
        add("Every snapshot date is a Tuesday except 2026-11-23, which is a Monday. "
            "The options archive used by `scripts/backtest_orb_post_expiry.py` "
            "records 2026-11-24 as a holiday, so that Tuesday expiry shifts back "
            "one session -- exactly what `weekly_expiries()` resolves above. A "
            "Thursday rule cannot produce any of these dates.")
    add("")
    add("## 2. Expiry day vs the same week's other sessions (paired)")
    add("")
    paired = paired_table(features)
    add("*diff = expiry day minus the mean of the other four sessions that week.*")
    add("*|t| > 2 is the minimum bar for a real effect.*")
    add("")
    add("```")
    add(paired.to_string(index=False, float_format=lambda v: f"{v:+.4f}"))
    add("```")
    add("")
    add("16 tests above: even with no real effect you expect roughly one |t| > 2 "
        "by chance. Judge a row by whether the **sign and size agree across both "
        "eras**, not by a single t-stat. The Tuesday era has only 64 expiry "
        "sessions, so its standard errors are ~2.3x the Thursday era's.")
    add("")
    add("## 3. Pin test (distance to the nearest 50-point strike / day range)")
    add("")
    pin_rows = []
    for era, group in features.groupby("era"):
        for label, sub in (("expiry", group[group.is_expiry]),
                           ("other", group[~group.is_expiry])):
            pin_rows.append(dict(era=era, day=label, n=len(sub),
                                 at_1300=sub.pin_1300.mean(),
                                 at_1500=sub.pin_1500.mean(),
                                 at_1525=sub.pin_1525.mean(),
                                 drift=sub.pin_1525.mean() - sub.pin_1500.mean()))
    add("```")
    add(pd.DataFrame(pin_rows).to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    add("```")
    add("")
    add("## 4. Opening-range breakouts")
    add("")
    orb = orb_table(frame, features)
    orb_rows = []
    for era, group in orb.groupby("era"):
        exp = group[group.is_expiry]
        oth = group[~group.is_expiry]
        pooled = pd.concat([exp.held, oth.held]).mean()
        se = np.sqrt(pooled * (1 - pooled) * (1 / len(exp) + 1 / len(oth)))
        z = (exp.held.mean() - oth.held.mean()) / se if se > 0 else np.nan
        for label, sub, zd in (("expiry", exp, z), ("other", oth, np.nan)):
            orb_rows.append(dict(era=era, day=label, n=len(sub),
                                 held_pct=sub.held.mean() * 100,
                                 edge_pct=sub.edge_pct.mean(),
                                 z_vs_other=zd))
    add("```")
    add(pd.DataFrame(orb_rows).to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    add("```")
    add("")
    add("`z_vs_other` is the expiry-vs-rest difference for that era. Thursday "
        "expiry days held better (+6.2pp, z 2.06); Tuesday expiry days hold "
        "*worse* (-4.5pp, z -0.63). The effect does not survive the calendar "
        "change, so it is not a rule -- and one z of 2.06 out of many tests is "
        "what a null sample produces by chance.")
    add("")
    add("## 5. 0-DTE option backtest (live 20% stop / 30% target)")
    add("")
    sim = dte_backtest(frame, features)
    sim.to_csv(OUT_DIR / "dte_backtest.csv", index=False)
    agg = (sim.groupby(["dte", "iv", "is_expiry"])
              .agg(n=("ret", "size"), win=("ret", lambda s: (s > 0).mean() * 100),
                   mean_ret=("ret", "mean"), mean_pnl=("pnl", "mean"),
                   stops=("reason", lambda s: (s == "stop").mean() * 100))
              .reset_index())
    add("```")
    add(agg.to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    add("```")
    add("")
    add("`mean_ret`/`mean_pnl` are **gross** (no spread, no slippage). The next "
        "two tables add costs and vary the entry time.")
    add("")
    add("### Cost sensitivity (expiry days only, IV 0.14)")
    add("")
    add("```")
    cost_rows = []
    exp = sim[sim.is_expiry & (sim.iv == 0.14)]
    for dte, label in DTE_CASES:
        sub = exp[exp.dte == dte]
        if sub.empty:
            continue
        cost_rows.append(dict(
            dte=label, n=len(sub), win=(sub.ret > 0).mean() * 100,
            gross_pnl=sub.pnl.mean(),
            net_1pt=(sub.pnl - 1.0 * LOT_SIZE).mean(),
            net_2pt=(sub.pnl - 2.0 * LOT_SIZE).mean(),
            avg_premium=sub.entry.mean(),
        ))
    add(pd.DataFrame(cost_rows).to_string(index=False, float_format=lambda v: f"{v:.2f}"))
    add("```")
    add("")
    add("### 0-DTE entry-time sweep (expiry days, IV 0.14)")
    add("")
    add("```")
    add(entry_time_sweep(frame, features).to_string(
        index=False, float_format=lambda v: f"{v:.2f}"))
    add("```")
    add("")
    add("### Typical 5-minute bar range (index points)")
    add("")
    add("```")
    add(five_min_range(frame, features).to_string(
        index=False, float_format=lambda v: f"{v:.1f}"))
    add("```")
    add("")
    add("### Index move needed to hit the rules (ATM, IV 0.14, spot 30,000)")
    add("")
    need = []
    for dte, label in DTE_CASES:
        stop_pts, target_pts = points_to_hit(30_000.0, dte, 0.14)
        need.append(dict(dte=label, stop_points=stop_pts, target_points=target_pts))
    add("```")
    add(pd.DataFrame(need).to_string(index=False, float_format=lambda v: f"{v:.1f}"))
    add("```")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="NIFTY expiry-day study")
    parser.add_argument("--start", default=DEFAULT_START.isoformat())
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--refresh", action="store_true",
                        help="re-download the 5m history instead of using the cache")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    frame = load_frame(start, end, refresh=args.refresh)
    sessions = clean_sessions(frame)
    features = tag_expiry(session_features(frame), weekly_expiries(sessions, start, end))
    weekly = weekly_expiries(sessions, start, end)
    monthly = monthly_expiries(sessions, start, end)

    report = build_report(frame, features, weekly, monthly)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "report.md").write_text(report, encoding="utf-8")
    features.to_csv(OUT_DIR / "session_features.csv")
    print(report)
    print(f"\nwrote {OUT_DIR / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
