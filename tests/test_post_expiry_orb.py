"""Offline tests for the post-expiry ORB study helpers."""

from datetime import date, timedelta

import pandas as pd

from scripts.backtest_orb_post_expiry import (
    FIXED_DTE,
    build_candidate_dte,
    dte_by_day,
    expiry_cycles,
    session_dates,
    weekly_expiries,
)

IST = "Asia/Kolkata"


def _weekdays(start: date, weeks: int) -> list:
    out, cursor = [], start
    for _ in range(weeks * 7):
        if cursor.weekday() < 5:
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# Expiry calendar
# ---------------------------------------------------------------------------

def test_weekly_expiry_is_tuesday_when_tuesday_trades():
    sessions = _weekdays(date(2026, 10, 5), 4)
    expiries = weekly_expiries(sessions, date(2026, 10, 5), date(2026, 10, 30))
    assert date(2026, 10, 13) in expiries
    assert date(2026, 10, 27) in expiries
    assert all(e.weekday() == 1 for e in expiries)


def test_weekly_expiry_shifts_back_when_tuesday_is_a_holiday():
    # 2026-11-24 is a holiday in this dataset; expiry moves to Monday 23rd.
    sessions = [d for d in _weekdays(date(2026, 11, 16), 3) if d != date(2026, 11, 24)]
    expiries = weekly_expiries(sessions, date(2026, 11, 16), date(2026, 11, 30))
    assert date(2026, 11, 23) in expiries
    assert date(2026, 11, 24) not in expiries


def test_weekly_expiry_walks_back_past_multi_day_closure():
    sessions = [
        d for d in _weekdays(date(2026, 9, 14), 3)
        if d not in {date(2026, 9, 21), date(2026, 9, 22)}
    ]
    expiries = weekly_expiries(sessions, date(2026, 9, 14), date(2026, 9, 30))
    # Tuesday 22nd is closed, Monday 21st is closed -> Friday 18th.
    assert date(2026, 9, 18) in expiries


# ---------------------------------------------------------------------------
# Cycles and DTE
# ---------------------------------------------------------------------------

def test_expiry_cycles_take_the_next_two_sessions():
    sessions = [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4)]
    cycles = expiry_cycles(sessions, [date(2026, 9, 1)])
    assert len(cycles) == 1
    assert cycles[0]["e1"] == date(2026, 9, 2)
    assert cycles[0]["e2"] == date(2026, 9, 3)


def test_expiry_cycle_without_following_session_is_dropped():
    sessions = [date(2026, 9, 1)]
    assert expiry_cycles(sessions, [date(2026, 9, 1)]) == []


def test_expiry_cycles_keep_a_pending_second_day():
    sessions = [date(2026, 9, 1), date(2026, 9, 2)]
    cycles = expiry_cycles(sessions, [date(2026, 9, 1)])
    assert cycles[0]["e1"] == date(2026, 9, 2)
    assert cycles[0]["e2"] is None


def test_dte_is_calendar_days_to_the_next_expiry():
    sessions = [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 8)]
    expiries = [date(2026, 9, 1), date(2026, 9, 8)]
    mapping = dte_by_day(sessions, expiries)
    assert mapping["2026-09-02"] == 6.0
    assert mapping["2026-09-03"] == 5.0


def test_expiry_day_is_priced_as_one_dte_not_the_next_cycle():
    sessions = [date(2026, 9, 1), date(2026, 9, 8)]
    expiries = [date(2026, 9, 1), date(2026, 9, 8)]
    mapping = dte_by_day(sessions, expiries)
    # 8 Sep is itself an expiry: its front contract dies that afternoon.
    assert mapping["2026-09-08"] == 1.0
    # Without any later expiry the fallback keeps the research calibration.
    assert dte_by_day([date(2026, 9, 8)], [date(2026, 9, 1)])["2026-09-08"] == FIXED_DTE


# ---------------------------------------------------------------------------
# Session detection
# ---------------------------------------------------------------------------

def test_session_dates_drop_incomplete_sessions():
    frame = pd.DataFrame(
        {"Close": [100.0] * 150},
        index=pd.DatetimeIndex(
            [pd.Timestamp("2026-09-01 09:15", tz=IST)] * 75
            + [pd.Timestamp("2026-09-02 09:15", tz=IST)] * 75),
    )
    frame.index = frame.index + [pd.Timedelta(minutes=m) for m in range(75)] * 2
    days = session_dates(frame)
    assert date(2026, 9, 1) in days
    assert date(2026, 9, 2) in days

    truncated = frame[frame.index.date == date(2026, 9, 2)].head(20)
    assert session_dates(truncated) == []


# ---------------------------------------------------------------------------
# Pricing with a per-session DTE
# ---------------------------------------------------------------------------

def _candles(day="2026-09-02"):
    rows = [
        ("09:15", 25000, 25020, 24990, 25010),
        ("09:20", 25010, 25040, 25000, 25030),
        ("09:25", 25030, 25060, 25020, 25050),
        ("09:30", 25050, 25080, 25040, 25070),
        ("09:35", 25070, 25100, 25060, 25090),
        ("09:40", 25090, 25120, 25080, 25110),
    ]
    return [{
        "timestamp": pd.Timestamp(f"{day} {clock}", tz=IST).isoformat(),
        "open": float(o), "high": float(h), "low": float(l), "close": float(c),
        "volume": 0.0, "underlying_symbol": "NIFTY",
        "exchange": "NSE", "instrument_type": "index",
    } for clock, o, h, l, c in rows]


def _meta(dte, *, actual=True):
    return {
        "name": "NIFTY", "lot": 65, "step": 50, "vol": 0.18,
        "dte_by_day": {"2026-09-02": dte}, "actual_dte": actual,
    }


def test_candidate_uses_the_session_dte():
    trade = build_candidate_dte(_candles(), 3, "CE", _meta(6.0))
    assert trade is not None
    assert trade["dte"] == 6.0
    assert trade["strike"] == 25050
    assert trade["qty"] == trade["lots"] * 65
    assert trade["reason"] in {"eod", "stop", "target"}


def test_extra_dte_is_worth_more_premium():
    short = build_candidate_dte(_candles(), 3, "CE", _meta(2.0))
    long = build_candidate_dte(_candles(), 3, "CE", _meta(6.0))
    assert long["entry_premium"] > short["entry_premium"]


def test_fixed_dte_mode_ignores_the_calendar():
    trade = build_candidate_dte(_candles(), 3, "CE", _meta(2.0, actual=False))
    assert trade["dte"] == FIXED_DTE


def test_candidate_returns_none_without_a_next_bar():
    assert build_candidate_dte(_candles()[:4], 3, "CE", _meta(5.0)) is None
