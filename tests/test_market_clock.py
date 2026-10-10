"""
TRIO — Market clock tests (Phase 2)
Author: Kapil Kuhire <kapilkuhire89@gmail.com>
"""
import sys
from datetime import time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.market_clock import MarketClock, market_clock_from_config


def _valid_clock():
    return MarketClock(
        open="09:15", entry_start="09:20", entry_cutoff="15:00",
        hard_flat="15:10", close="15:30",
        valid=True, timezone_name="Asia/Kolkata",
    )


def test_before_session_start_no_entry():
    c = _valid_clock()
    assert c.can_enter(dtime(9, 10)) is False
    assert c.session_state(dtime(9, 10)) == "pre"


def test_exact_entry_start_allowed():
    c = _valid_clock()
    assert c.can_enter(dtime(9, 20)) is True
    assert c.session_state(dtime(9, 20)) == "open"


def test_during_session_allowed():
    c = _valid_clock()
    assert c.can_enter(dtime(12, 0)) is True
    assert c.session_state(dtime(12, 0)) == "open"


def test_exact_cutoff_blocks_new_entries():
    c = _valid_clock()
    assert c.can_enter(dtime(15, 0)) is False
    assert c.session_state(dtime(15, 0)) == "exits-only"


def test_after_cutoff_no_new_entries():
    c = _valid_clock()
    assert c.can_enter(dtime(15, 5)) is False
    assert c.is_exits_only(dtime(15, 5)) is True


def test_invalid_time_configuration_fails_closed():
    c = MarketClock(entry_start="xx", entry_cutoff="yy",
                     hard_flat="15:10", close="15:30", valid=True)
    # Build-one via factory to exercise parse error -> valid=False
    clk = market_clock_from_config({"market": {"entry_start": "xx",
        "entry_cutoff": "yy", "hard_flat": "15:10", "close": "15:30"}})
    assert clk.valid is False
    assert clk.can_enter(dtime(12, 0)) is False


def test_wrong_timezone_fails_closed():
    clk = market_clock_from_config({"market": {"timezone": "Europe/London",
        "open": "09:15", "entry_start": "09:20", "entry_cutoff": "15:00",
        "hard_flat": "15:10", "close": "15:30"}})
    assert clk.valid is False
    assert clk.can_enter(dtime(10, 0)) is False
    assert clk.session_state(dtime(10, 0)) == "invalid"


def test_hard_flat_time():
    c = _valid_clock()
    assert c.is_hard_flat(dtime(15, 10)) is True
    assert c.is_hard_flat(dtime(15, 20)) is True
    assert c.is_hard_flat(dtime(14, 59)) is False
    assert c.session_state(dtime(15, 10)) == "hard-flat"
    assert c.session_state(dtime(15, 31)) == "closed"
