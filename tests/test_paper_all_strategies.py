"""Paper-only five-sleeve promotion switch.

No broker, no provider and no network: these tests only prove the launcher
merges the approved paper policy, refuses every non-paper configuration, and
leaves the YAML overlay untouched.
"""

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from scripts import session
from src.options_intraday_runtime import OptionsIntradayPolicyError

ROOT = Path(__file__).resolve().parent.parent

BASE_MODE = {
    "execution_class": "remote_paper",
    "PAPER_TRADING": True,
    "shadow_strategies": [
        "mean_reversion", "opening_range_reversion", "smc_liquidity"],
    "max_open_positions": 2,
    "max_correlated_positions": 2,
    "max_premium_exposure_pct": 5.00,
    "max_consecutive_losses": 2,
    "max_daily_loss_pct": 1.50,
    "max_premium_per_position": 18000,
    "risk_per_trade_pct": 0.70,
}


def _cfg():
    return {"options_intraday": deepcopy(BASE_MODE)}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(session.PAPER_ALL_STRATEGIES_ENV, raising=False)
    monkeypatch.delenv(session.PAPER_LONG_STRUCTURES_ENV, raising=False)
    monkeypatch.delenv("TRIO_OPTIONS_LIVE", raising=False)
    # _run_options_only_session uses os.environ.setdefault for the live policy.
    # Pin these with monkeypatch so this module cannot leak them into siblings.
    for key in (
        "TRIO_OPTIONS_CAPITAL",
        "TRIO_OPTIONS_SCAN_UNDERLYINGS",
        "TRIO_OPTIONS_SCAN_INTERVAL",
        "TRIO_OPTIONS_CHAIN_PROVIDER",
        "TRIO_OPTIONS_TARGET_FRACTION",
    ):
        monkeypatch.setenv(key, "")


def test_switch_absent_is_identity():
    cfg = _cfg()
    before = deepcopy(cfg)

    assert session._apply_paper_all_strategies(cfg) is False
    assert cfg == before


def test_switch_applies_the_approved_table(monkeypatch):
    monkeypatch.setenv(session.PAPER_ALL_STRATEGIES_ENV, "1")
    cfg = _cfg()

    assert session._apply_paper_all_strategies(cfg) is True
    mode = cfg["options_intraday"]

    # Every sleeve tradable, one slot each.
    assert mode["shadow_strategies"] == []
    assert mode["max_open_positions"] == 5
    assert mode["max_correlated_positions"] == 5
    assert mode["max_premium_exposure_pct"] == 9.00
    assert mode["max_consecutive_losses"] == 2
    assert mode["vwap_filter_strategies"] == ["orb", "orb_retest"]

    # Risk envelope deliberately unchanged: one lot per trade, 1.50% daily cap.
    assert mode["max_daily_loss_pct"] == 1.50
    assert mode["max_premium_per_position"] == 18000
    assert mode["risk_per_trade_pct"] == 0.70
    assert mode["PAPER_TRADING"] is True
    assert mode["execution_class"] == "remote_paper"


def test_long_structures_switch_only_enables_the_two_structures(monkeypatch):
    monkeypatch.setenv(session.PAPER_LONG_STRUCTURES_ENV, "1")
    cfg = _cfg()

    assert session._apply_paper_all_strategies(cfg) is True
    mode = cfg["options_intraday"]

    assert mode["strategies"] == ["long_straddle", "long_strangle"]
    assert mode["shadow_strategies"] == []
    assert mode["max_open_positions"] == 2
    assert mode["max_correlated_positions"] == 2
    assert mode["max_premium_exposure_pct"] == 9.00
    assert mode["max_consecutive_losses"] == 2
    assert mode["vwap_filter_strategies"] == []
    # The shared risk envelope is untouched.
    assert mode["max_daily_loss_pct"] == 1.50
    assert mode["max_premium_per_position"] == 18000
    assert mode["risk_per_trade_pct"] == 0.70


def test_long_structures_switch_wins_over_paper_all(monkeypatch):
    monkeypatch.setenv(session.PAPER_ALL_STRATEGIES_ENV, "1")
    monkeypatch.setenv(session.PAPER_LONG_STRUCTURES_ENV, "1")
    cfg = _cfg()

    assert session._apply_paper_all_strategies(cfg) is True
    assert cfg["options_intraday"]["strategies"] == [
        "long_straddle", "long_strangle"]
    assert cfg["options_intraday"]["vwap_filter_strategies"] == []


@pytest.mark.parametrize("cfg_update", [
    {"execution_class": "live"},
    {"PAPER_TRADING": False},
])
def test_long_structures_switch_refuses_outside_remote_paper(
        monkeypatch, cfg_update):
    monkeypatch.setenv(session.PAPER_LONG_STRUCTURES_ENV, "1")
    cfg = _cfg()
    cfg["options_intraday"].update(cfg_update)
    before = deepcopy(cfg)

    with pytest.raises(OptionsIntradayPolicyError, match="remote_paper"):
        session._apply_paper_all_strategies(cfg)

    assert cfg == before


def test_switch_is_idempotent(monkeypatch):
    monkeypatch.setenv(session.PAPER_ALL_STRATEGIES_ENV, "1")
    cfg = _cfg()

    assert session._apply_paper_all_strategies(cfg) is True
    first = deepcopy(cfg)
    assert session._apply_paper_all_strategies(cfg) is True

    assert cfg == first


@pytest.mark.parametrize("override", [
    {"execution_class": "live"},
    {"execution_class": "local_paper"},
    {"execution_class": ""},
    {"PAPER_TRADING": False},
    {"PAPER_TRADING": "false"},
    {"PAPER_TRADING": None},
])
def test_switch_refuses_outside_remote_paper(monkeypatch, override):
    monkeypatch.setenv(session.PAPER_ALL_STRATEGIES_ENV, "1")
    cfg = _cfg()
    cfg["options_intraday"].update(override)
    before = deepcopy(cfg)

    with pytest.raises(OptionsIntradayPolicyError, match="remote_paper"):
        session._apply_paper_all_strategies(cfg)

    # A refusal must not partially apply the overrides.
    assert cfg == before


def test_switch_refuses_when_live_opt_in_is_set(monkeypatch):
    monkeypatch.setenv(session.PAPER_ALL_STRATEGIES_ENV, "1")
    monkeypatch.setenv("TRIO_OPTIONS_LIVE", "1")
    cfg = _cfg()
    before = deepcopy(cfg)

    with pytest.raises(OptionsIntradayPolicyError, match="TRIO_OPTIONS_LIVE"):
        session._apply_paper_all_strategies(cfg)

    assert cfg == before


def test_switch_creates_the_section_when_the_base_config_has_none(monkeypatch):
    """``load_config`` has no options section; the YAML overlay is a separate file."""
    monkeypatch.setenv(session.PAPER_ALL_STRATEGIES_ENV, "1")
    cfg = {}

    assert session._apply_paper_all_strategies(cfg) is True
    assert cfg["options_intraday"]["max_open_positions"] == 5
    assert cfg["options_intraday"]["shadow_strategies"] == []


def test_launcher_applies_overrides_before_building_the_trader(monkeypatch):
    monkeypatch.setenv(session.PAPER_ALL_STRATEGIES_ENV, "1")
    cfg = _cfg()
    seen = {}

    class _Stop(RuntimeError):
        pass

    def factory(*args, **kwargs):
        seen.update(deepcopy(cfg["options_intraday"]))
        raise _Stop

    monkeypatch.setattr(session, "PaperTrader", factory)
    monkeypatch.setattr(session, "load_config", lambda: cfg)

    with pytest.raises(_Stop):
        session._run_options_only_session(
            cfg, "options_intraday", interval_seconds=60,
            max_seconds=1, management_only=False)

    assert seen["max_open_positions"] == 5
    assert seen["shadow_strategies"] == []
    assert seen["vwap_filter_strategies"] == ["orb", "orb_retest"]


def test_launcher_applies_long_structures_only(monkeypatch):
    monkeypatch.setenv(session.PAPER_LONG_STRUCTURES_ENV, "1")
    cfg = _cfg()
    seen = {}

    class _Stop(RuntimeError):
        pass

    def factory(*args, **kwargs):
        seen.update(deepcopy(cfg["options_intraday"]))
        raise _Stop

    monkeypatch.setattr(session, "PaperTrader", factory)
    monkeypatch.setattr(session, "load_config", lambda: cfg)

    with pytest.raises(_Stop):
        session._run_options_only_session(
            cfg, "options_intraday", interval_seconds=60,
            max_seconds=1, management_only=False)

    assert seen["strategies"] == ["long_straddle", "long_strangle"]
    assert seen["shadow_strategies"] == []


def test_launcher_without_the_switch_keeps_the_yaml_policy(monkeypatch):
    cfg = _cfg()
    seen = {}

    class _Stop(RuntimeError):
        pass

    def factory(*args, **kwargs):
        seen.update(deepcopy(cfg["options_intraday"]))
        raise _Stop

    monkeypatch.setattr(session, "PaperTrader", factory)
    monkeypatch.setattr(session, "load_config", lambda: cfg)

    with pytest.raises(_Stop):
        session._run_options_only_session(
            cfg, "options_intraday", interval_seconds=60,
            max_seconds=1, management_only=False)

    assert seen == BASE_MODE


def test_yaml_overlay_stays_conservative():
    """The switch is the only way to promote sleeves; the file never changes."""
    path = ROOT / "config" / "options_intraday.yaml"
    mode = (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["options_intraday"]

    assert mode["shadow_strategies"] == [
        "mean_reversion", "opening_range_reversion", "smc_liquidity"]
    assert mode["max_open_positions"] == 2
    assert mode["max_correlated_positions"] == 2
    assert mode["max_premium_exposure_pct"] == 5.00
    assert mode["max_consecutive_losses"] == 2
    assert mode["max_daily_loss_pct"] == 1.50
    assert mode["max_premium_per_position"] == 18000
    assert "vwap_filter_strategies" not in mode
