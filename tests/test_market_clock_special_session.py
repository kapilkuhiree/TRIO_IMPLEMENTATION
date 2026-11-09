"""One-off special-session clock tests (Diwali Muhurat 2026)."""

from datetime import datetime, timedelta, timezone

from src.market_clock import IST, market_clock_from_config
from src.utils import load_config


SPECIAL = {
    "2026-11-08": {
        "open": "14:20",
        "entry_start": "14:25",
        "entry_cutoff": "15:05",
        "hard_flat": "15:15",
        "close": "15:40",
    }
}


def _clock(**overrides):
    market = {
        "timezone": "Asia/Kolkata",
        "open": "09:15",
        "entry_start": "09:20",
        "entry_cutoff": "15:00",
        "hard_flat": "15:10",
        "close": "15:30",
        "holidays": [],
        "special_sessions": SPECIAL,
    }
    market.update(overrides)
    return market_clock_from_config({"market": market})


def _at(hour, minute, day=8):
    return datetime(2026, 11, day, hour, minute, tzinfo=IST)


def test_sunday_special_session_is_a_trading_day():
    clock = _clock()
    assert clock.valid, clock.error
    assert clock.is_trading_day(_at(14, 30)) is True


def test_special_session_window_overrides_the_base_window():
    clock = _clock()
    assert clock.can_enter(_at(14, 24)) is False
    assert clock.can_enter(_at(14, 25)) is True
    assert clock.can_enter(_at(15, 4)) is True
    assert clock.can_enter(_at(15, 5)) is False
    assert clock.is_exits_only(_at(15, 6)) is True
    assert clock.is_hard_flat(_at(15, 15)) is True
    assert clock.is_closed(_at(15, 39)) is False
    assert clock.is_closed(_at(15, 40)) is True
    assert clock.session_state(_at(14, 19)) == "pre"
    assert clock.session_state(_at(14, 25)) == "open"
    assert clock.session_state(_at(15, 6)) == "exits-only"
    assert clock.session_state(_at(15, 15)) == "hard-flat"
    assert clock.session_state(_at(15, 40)) == "closed"


def test_other_days_keep_their_normal_calendar():
    clock = _clock()
    monday = datetime(2026, 11, 9, 10, 0, tzinfo=IST)
    saturday = datetime(2026, 11, 14, 10, 0, tzinfo=IST)
    assert clock.is_trading_day(monday) is True
    assert clock.can_enter(monday) is True
    assert clock.is_trading_day(saturday) is False


def test_holiday_beats_a_special_session():
    clock = _clock(holidays=["2026-11-08"])
    assert clock.is_trading_day(_at(14, 30)) is False
    assert clock.session_state(_at(14, 30)) == "closed"


def test_invalid_special_session_fails_closed():
    clock = _clock(special_sessions={"2026-11-08": {
        "open": "14:20", "entry_start": "15:30",
        "entry_cutoff": "15:05", "hard_flat": "15:15", "close": "15:40"}})
    assert clock.valid is False
    assert "special session" in clock.error


def test_repository_market_config_declares_the_session():
    cfg = load_config()
    special = cfg["market"]["special_sessions"]
    assert special["2026-11-08"] == SPECIAL["2026-11-08"]
