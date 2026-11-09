"""Date-locked live gate and Muhurat overlay tests (offline)."""

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
MUHURAT = ROOT / "config" / "options_intraday_muhurat.yaml"
PAPER = ROOT / "config" / "options_intraday.yaml"
SESSION_DATE = "2026-11-08"


def _yaml_mode(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    return deepcopy(loaded.get("options_intraday", loaded))


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
    return _yaml_mode(MUHURAT)


def test_options_live_requested_is_truthy_only(monkeypatch):
    monkeypatch.setenv("TRIO_OPTIONS_LIVE", "0")
    assert options_live_requested() is False
    monkeypatch.setenv("TRIO_OPTIONS_LIVE", "yes")
    assert options_live_requested() is True


def test_live_gate_accepts_only_the_declared_date(live_env, monkeypatch):
    _freeze(monkeypatch, datetime(2026, 11, 8, 14, 30, tzinfo=IST))
    ok, reason = validate_options_mode_environment({"options_intraday": live_env})
    assert ok, reason

    _freeze(monkeypatch, datetime(2026, 11, 7, 14, 30, tzinfo=IST))
    ok, reason = validate_options_mode_environment({"options_intraday": live_env})
    assert ok is False
    assert "session date" in reason


def test_live_gate_requires_zerodha(live_env, monkeypatch):
    _freeze(monkeypatch, datetime(2026, 11, 8, 14, 30, tzinfo=IST))
    monkeypatch.setenv("TRIO_BROKER_PROVIDER", "megabull")
    ok, reason = validate_options_mode_environment({"options_intraday": live_env})
    assert ok is False
    assert "zerodha" in reason


def test_live_gate_requires_live_execution_class(live_env, monkeypatch):
    _freeze(monkeypatch, datetime(2026, 11, 8, 14, 30, tzinfo=IST))
    mode = deepcopy(live_env)
    mode["execution_class"] = "remote_paper"
    ok, reason = validate_options_mode_environment({"options_intraday": mode})
    assert ok is False
    assert "execution_class=live" in reason


def test_live_gate_rejects_paper_env(live_env, monkeypatch):
    _freeze(monkeypatch, datetime(2026, 11, 8, 14, 30, tzinfo=IST))
    monkeypatch.setenv("PAPER_TRADING", "true")
    ok, reason = validate_options_mode_environment({"options_intraday": live_env})
    assert ok is False
    assert "PAPER_TRADING=false" in reason


def test_live_gate_rejects_paper_flag_in_config(live_env, monkeypatch):
    _freeze(monkeypatch, datetime(2026, 11, 8, 14, 30, tzinfo=IST))
    mode = deepcopy(live_env)
    mode["PAPER_TRADING"] = True
    ok, reason = validate_options_mode_environment({"options_intraday": mode})
    assert ok is False
    assert "PAPER_TRADING=false" in reason


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
    assert mode["strategies"] == ["orb", "mean_reversion"]
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


def test_muhurat_overlay_values(monkeypatch):
    monkeypatch.setenv("TRIO_OPTIONS_LIVE", "1")
    mode = _yaml_mode(MUHURAT)
    loaded = load_options_intraday_config({"options_intraday": mode})
    effective = loaded["options_intraday"]
    assert effective["execution_class"] == "live"
    assert effective["PAPER_TRADING"] is False
    assert effective["live_session_date"] == SESSION_DATE
    assert effective["strategies"] == ["mean_reversion"]
    assert effective["max_open_positions"] == 1
    assert effective["max_premium_per_position"] == 20000
    assert effective["min_wallet"] == 40000
    assert effective["profit_lock_rungs"] == [0.10, 0.15, 0.20, 0.25]
    assert effective["contract_specs"]["NIFTY"]["lot_size"] == 65
    assert effective["contract_specs"]["BANKNIFTY"]["lot_size"] == 30
    # The normal paper overlay is merged underneath and must not leak the
    # paper strategy list into the live session.
    assert "orb" not in effective["strategies"]
