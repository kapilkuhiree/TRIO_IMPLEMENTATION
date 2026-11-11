"""Date-locked live gate tests on the normal weekday options book (offline)."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

import src.options_intraday as oi
from src.options_intraday import (
    load_options_intraday_config,
    options_live_requested,
    validate_options_mode_environment,
)


IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent.parent
PAPER = ROOT / "config" / "options_intraday.yaml"
SESSION_DATE = "2026-11-09"


def _yaml_mode(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    return deepcopy(loaded.get("options_intraday", loaded))


def _live_mode():
    """Normal weekday book promoted to the explicit live path."""
    mode = _yaml_mode(PAPER)
    mode["execution_class"] = "live"
    mode["PAPER_TRADING"] = False
    mode["live_session_date"] = SESSION_DATE
    return mode


def _freeze(monkeypatch, when: datetime):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return when.replace(tzinfo=None)
            return when.astimezone(tz)

    monkeypatch.setattr(oi, "datetime", FixedDateTime)


@pytest.fixture
def live_env(monkeypatch):
    monkeypatch.setenv("TRIO_OPTIONS_LIVE", "1")
    monkeypatch.setenv("TRIO_BROKER_PROVIDER", "zerodha")
    monkeypatch.setenv("PAPER_TRADING", "false")
    monkeypatch.delenv("TRIO_ALLOW_LOCAL_OPTION_SHADOW", raising=False)
    return _live_mode()


def test_options_live_requested_is_truthy_only(monkeypatch):
    monkeypatch.setenv("TRIO_OPTIONS_LIVE", "0")
    assert options_live_requested() is False
    monkeypatch.setenv("TRIO_OPTIONS_LIVE", "yes")
    assert options_live_requested() is True


def test_live_gate_accepts_only_the_declared_date(live_env, monkeypatch):
    _freeze(monkeypatch, datetime(2026, 11, 9, 10, 30, tzinfo=IST))
    ok, reason = validate_options_mode_environment({"options_intraday": live_env})
    assert ok, reason

    _freeze(monkeypatch, datetime(2026, 11, 10, 10, 30, tzinfo=IST))
    ok, reason = validate_options_mode_environment({"options_intraday": live_env})
    assert ok is False
    assert "session date" in reason


def test_live_gate_requires_zerodha(live_env, monkeypatch):
    _freeze(monkeypatch, datetime(2026, 11, 9, 10, 30, tzinfo=IST))
    monkeypatch.setenv("TRIO_BROKER_PROVIDER", "megabull")
    ok, reason = validate_options_mode_environment({"options_intraday": live_env})
    assert ok is False
    assert "zerodha" in reason


def test_live_gate_requires_live_execution_class(live_env, monkeypatch):
    _freeze(monkeypatch, datetime(2026, 11, 9, 10, 30, tzinfo=IST))
    mode = deepcopy(live_env)
    mode["execution_class"] = "remote_paper"
    ok, reason = validate_options_mode_environment({"options_intraday": mode})
    assert ok is False
    assert "execution_class=live" in reason


def test_live_gate_rejects_paper_env(live_env, monkeypatch):
    _freeze(monkeypatch, datetime(2026, 11, 9, 10, 30, tzinfo=IST))
    monkeypatch.setenv("PAPER_TRADING", "true")
    ok, reason = validate_options_mode_environment({"options_intraday": live_env})
    assert ok is False
    assert "PAPER_TRADING=false" in reason


def test_live_gate_rejects_paper_flag_in_config(live_env, monkeypatch):
    _freeze(monkeypatch, datetime(2026, 11, 9, 10, 30, tzinfo=IST))
    mode = deepcopy(live_env)
    mode["PAPER_TRADING"] = True
    ok, reason = validate_options_mode_environment({"options_intraday": mode})
    assert ok is False
    assert "PAPER_TRADING=false" in reason


def test_live_mode_keeps_the_weekday_book(monkeypatch):
    monkeypatch.setenv("TRIO_OPTIONS_LIVE", "1")
    loaded = load_options_intraday_config({"options_intraday": _live_mode()})
    effective = loaded["options_intraday"]
    assert effective["execution_class"] == "live"
    assert effective["PAPER_TRADING"] is False
    assert effective["live_session_date"] == SESSION_DATE
    assert effective["strategies"] == [
        "orb", "orb_retest", "mean_reversion", "opening_range_reversion",
        "smc_liquidity",
    ]
    assert effective["max_open_positions"] == 2
    assert effective["max_premium_per_position"] == 18000
    assert effective["min_wallet"] == 40000
    assert effective["contract_specs"]["NIFTY"]["lot_size"] == 65
    assert effective["contract_specs"]["BANKNIFTY"]["lot_size"] == 30


def test_without_live_flag_normal_paper_mode_is_unchanged(monkeypatch):
    monkeypatch.setenv("TRIO_OPTIONS_LIVE", "0")
    monkeypatch.setenv("TRIO_BROKER_PROVIDER", "megabull")
    monkeypatch.setenv("TRIO_REMOTE_REQUIRED", "1")
    monkeypatch.setenv("PAPER_TRADING", "true")
    mode = _yaml_mode(PAPER)
    cfg = {"options_intraday": mode,
           "execution": {"remote_paper_required": True},
           "broker": {"provider": "megabull"}}
    ok, reason = validate_options_mode_environment(cfg)
    assert ok, reason
    assert mode["strategies"] == [
        "orb", "orb_retest", "mean_reversion", "opening_range_reversion",
        "smc_liquidity",
    ]
    assert mode["max_open_positions"] == 2
    assert mode["PAPER_TRADING"] is True


def test_without_live_flag_zerodha_paper_is_rejected(monkeypatch):
    monkeypatch.setenv("TRIO_OPTIONS_LIVE", "0")
    monkeypatch.setenv("TRIO_BROKER_PROVIDER", "zerodha")
    monkeypatch.setenv("PAPER_TRADING", "true")
    mode = _yaml_mode(PAPER)
    ok, reason = validate_options_mode_environment({"options_intraday": mode})
    assert ok is False
    assert "megabull" in reason


def test_shipped_paper_config_shadows_only_the_research_sleeves():
    """Stage 1 must never put ORB or ORB-Retest into the log-only path."""
    mode = _yaml_mode(PAPER)
    assert mode["shadow_strategies"] == [
        "mean_reversion", "opening_range_reversion", "smc_liquidity",
    ]
    assert "orb" not in mode["shadow_strategies"]
    assert "orb_retest" not in mode["shadow_strategies"]
    assert "smc_liquidity" in mode["strategies"]
    assert mode["earliest_entry_time_by_strategy"]["opening_range_reversion"] == "09:35"
    assert mode["earliest_entry_time_by_strategy"]["smc_liquidity"] == "09:45"
