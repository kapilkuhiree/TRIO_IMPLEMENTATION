"""Weekday live mode on the options live runner.

These tests pin the safety contract: the normal weekday overlay is used, the
date lock must equal today, a typed confirmation is required before real
orders, and a missing NFO segment is a hard refusal.
"""

from __future__ import annotations

import argparse
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
IST = timezone(timedelta(hours=5, minutes=30))


def _load_runner():
    path = ROOT / "scripts" / "weekday_live.py"
    spec = importlib.util.spec_from_file_location("weekday_live_runner", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner():
    return _load_runner()


def _args(**overrides):
    base = dict(
        weekday_live=None, capital=None,
        state=None, underlyings=None, dry_run=False, confirm=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_weekday_overlay_uses_normal_book(runner):
    today = datetime.now(IST).date().isoformat()
    cfg = runner._effective_config(_args(weekday_live=today))
    mode = cfg["options_intraday"]
    assert mode["execution_class"] == "live"
    assert mode["PAPER_TRADING"] is False
    assert mode["live_session_date"] == today
    assert mode["strategies"] == [
        "orb", "orb_retest", "mean_reversion", "opening_range_reversion",
        "smc_liquidity",
    ]
    assert mode["max_open_positions"] == 2
    assert mode["max_premium_per_position"] == 18000
    assert mode["scan_underlyings"] == ["NIFTY"]


def test_default_overlay_is_the_weekday_book(runner):
    today = datetime.now(IST).date().isoformat()
    cfg = runner._effective_config(_args())
    mode = cfg["options_intraday"]
    assert mode["strategies"] == [
        "orb", "orb_retest", "mean_reversion", "opening_range_reversion",
        "smc_liquidity",
    ]
    assert mode["max_open_positions"] == 2
    assert mode["max_premium_per_position"] == 18000
    assert mode["live_session_date"] == today


def test_weekday_state_path_is_per_date(runner):
    args = _args(weekday_live="2026-11-09")
    path = runner._state_path(args)
    assert path.name == "live_2026-11-09.json"


def test_weekday_guard_refuses_wrong_date(runner):
    args = _args(weekday_live="2026-11-07", confirm="WEEKDAY-LIVE")
    with pytest.raises(SystemExit, match="date-locked"):
        runner._weekday_guards(args, _market_data(["NFO"]))


def test_weekday_guard_refuses_without_confirmation(runner):
    today = datetime.now(IST).date().isoformat()
    args = _args(weekday_live=today)
    with pytest.raises(SystemExit, match="WEEKDAY-LIVE"):
        runner._weekday_guards(args, _market_data(["NFO"]))


def test_weekday_guard_refuses_missing_nfo_segment(runner):
    today = datetime.now(IST).date().isoformat()
    args = _args(weekday_live=today, confirm="WEEKDAY-LIVE")
    with pytest.raises(SystemExit, match="no NFO segment"):
        runner._weekday_guards(args, _market_data(["MF", "BSE", "NSE", "BFO"]))


def test_weekday_guard_passes_when_ready(runner):
    today = datetime.now(IST).date().isoformat()
    args = _args(weekday_live=today, confirm="weekday-live")
    assert runner._weekday_guards(args, _market_data(["NSE", "NFO"])) is None


def test_weekday_dry_run_skips_confirmation(runner):
    today = datetime.now(IST).date().isoformat()
    args = _args(weekday_live=today, dry_run=True)
    assert runner._weekday_guards(args, _market_data([])) is None


class _MarketData:
    def __init__(self, exchanges):
        self._exchanges = exchanges

    def profile(self):
        return {"user_id": "TEST", "exchanges": self._exchanges}


def _market_data(exchanges):
    return _MarketData(exchanges)
