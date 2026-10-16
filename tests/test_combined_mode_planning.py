"""Focused acceptance tests for the combined production planning mode."""

from pathlib import Path

import pytest


def _screen_candidate(symbol="RELIANCE.NS", rank=88, confidence=84):
    class Signal:
        pass

    signal = Signal()
    signal.confidence = confidence

    class Candidate:
        pass

    candidate = Candidate()
    candidate.rank = rank
    candidate.signal = signal
    candidate.to_dict = lambda: {
        "symbol": symbol,
        "action": "BUY",
        "rank": rank,
        "confidence": confidence,
        "entry_price": 100.0,
        "stop_loss": 98.0,
        "target": 104.0,
        "position_size": 1,
    }
    return candidate


def _screen_cfg():
    return {
        "options": {
            "enabled": True,
            "underlying": "NIFTY",
        },
        "screener": {
            "min_rank": 55,
            "min_confidence": 70,
            "options_max_candidates": 6,
        },
    }


def test_mode_flags_keep_options_only_exact_and_cover_combined(monkeypatch):
    from src.options_intraday import (
        combined_mode_enabled,
        options_intraday_enabled,
        resolve_trading_mode,
        strict_options_enabled,
    )

    monkeypatch.delenv("TRIO_TRADING_MODE", raising=False)

    assert resolve_trading_mode({}) == "production"
    assert not options_intraday_enabled({})
    assert not strict_options_enabled({})

    assert options_intraday_enabled({}, "options_intraday")
    assert strict_options_enabled({}, "options_intraday")
    assert not combined_mode_enabled({}, "options_intraday")

    assert combined_mode_enabled({}, "combined")
    assert strict_options_enabled({}, "combined")
    assert not options_intraday_enabled({}, "combined")


def test_default_production_keeps_existing_optional_overlay_shape(monkeypatch):
    from scripts import premarket

    calls = {}
    monkeypatch.delenv("TRIO_TRADING_MODE", raising=False)
    monkeypatch.setattr(premarket, "load_config", _screen_cfg)
    monkeypatch.setattr(premarket, "resolve_basket",
                        lambda universe: ["RELIANCE.NS"])
    monkeypatch.setattr(premarket, "screen",
                        lambda **kwargs: (calls.update(kwargs) or
                                          [_screen_candidate()]))
    monkeypatch.setattr("src.options_chain.fetch_chain",
                        lambda symbol: None)
    monkeypatch.setattr(
        premarket,
        "build_intraday_plan",
        lambda cfg: pytest.fail("strict options plan is not production default"),
    )

    plan = premarket.build_plan("nifty100", "1d", 1)

    assert calls["top_n"] == 6  # existing production overlay scan breadth
    assert plan["trading_mode"] == "production"
    assert plan["candidates"]
    assert plan["option_candidates"]
    assert "options_intraday_plan" not in plan
    assert "legacy_options_overlay" not in plan


def test_combined_plan_keeps_equity_and_nests_strict_options_without_overlay(
    monkeypatch,
):
    from scripts import premarket

    calls = {}
    monkeypatch.delenv("TRIO_TRADING_MODE", raising=False)
    monkeypatch.setattr(premarket, "load_config", _screen_cfg)
    monkeypatch.setattr(premarket, "resolve_basket",
                        lambda universe: ["RELIANCE.NS"])
    monkeypatch.setattr(premarket, "screen",
                        lambda **kwargs: (calls.update(kwargs) or
                                          [_screen_candidate()]))
    monkeypatch.setattr("src.options_chain.fetch_chain",
                        lambda symbol: pytest.fail("legacy overlay was called"))

    nested = {
        "date": "2026-10-15",
        "generated_at": "2026-10-15T09:35:00+05:30",
        "trading_mode": "options_intraday",
        "asset": "options",
        "candidates": [],
        "option_candidates": [{
            "asset": "options",
            "signal_timestamp": "2026-10-15T09:35:00+05:30",
            "option_legs": {
                "legs": [{
                    "kind": "CE",
                    "quote_timestamp": "2026-10-15T09:34:00+05:30",
                }],
            },
        }],
        "option_chain": {
            "symbol": "NIFTY",
            "quote_timestamp": "2026-10-15T09:34:00+05:30",
        },
        "intraday_data_diagnostic": {"valid": True},
        "option_chain_diagnostic": {"valid": True},
        "execution_ready": False,
    }
    monkeypatch.setattr(premarket, "build_intraday_plan",
                        lambda cfg: nested)

    plan = premarket.build_plan("nifty100", "1d", 1,
                                trading_mode="combined")

    assert calls["top_n"] == 1
    assert calls["min_rank"] == 55
    assert calls["min_confidence"] == 70
    assert plan["trading_mode"] == "combined"
    assert plan["asset"] == "equity+options"
    assert len(plan["candidates"]) == 1
    assert "option_legs" not in plan["candidates"][0]
    assert plan["option_candidates"] == []
    assert plan["legacy_options_overlay"] is False

    options_plan = plan["options_intraday_plan"]
    assert options_plan["trading_mode"] == "options_intraday"
    assert options_plan["option_chain"]["symbol"] == "NIFTY"
    assert options_plan["generated_at"]
    assert options_plan["timestamps"]["generated_at"]
    assert options_plan["timestamps"]["option_chain_quote"]
    assert options_plan["option_candidates"][0]["signal_timestamp"]
    assert options_plan["option_candidates"][0]["option_legs"]["legs"][0][
        "quote_timestamp"
    ]
    assert options_plan["intraday_data_diagnostic"]["valid"] is True
    assert options_plan["option_chain_diagnostic"]["valid"] is True
    assert options_plan["execution_ready"] is False


def test_combined_premarket_preview_preserves_equity_when_options_builder_fails(
    monkeypatch,
):
    from scripts import premarket

    monkeypatch.delenv("TRIO_TRADING_MODE", raising=False)
    monkeypatch.setattr(premarket, "load_config", _screen_cfg)
    monkeypatch.setattr(premarket, "resolve_basket",
                        lambda universe: ["RELIANCE.NS"])
    monkeypatch.setattr(premarket, "screen",
                        lambda **kwargs: [_screen_candidate()])
    monkeypatch.setattr(premarket, "build_intraday_plan",
                        lambda cfg: (_ for _ in ()).throw(
                            RuntimeError("stale option preview")))

    plan = premarket.build_plan("nifty100", "1d", 1,
                                trading_mode="combined")

    assert len(plan["candidates"]) == 1
    assert plan["options_intraday_plan"]["execution_ready"] is False
    assert plan["options_intraday_plan"]["option_chain"] is None
    assert "options_plan_build_exception" in plan["options_intraday_plan"][
        "option_chain_diagnostic"]["failure_reason"]


def test_cli_and_workflows_declare_all_modes(monkeypatch, capsys):
    import main as cli

    monkeypatch.setattr("sys.argv", ["main.py", "paper", "--help"])
    with pytest.raises(SystemExit):
        cli.main()
    help_text = capsys.readouterr().out
    assert "production" in help_text
    assert "combined" in help_text
    assert "options_intraday" in help_text

    root = Path(__file__).resolve().parents[1]
    premarket_workflow = (root / ".github" / "workflows" /
                          "premarket.yml").read_text(encoding="utf-8")
    session_workflow = (root / ".github" / "workflows" /
                        "session.yml").read_text(encoding="utf-8")
    for workflow in (premarket_workflow, session_workflow):
        assert "TRIO_TRADING_MODE: options_intraday" in workflow
        assert "--trading-mode options_intraday" in workflow
        assert "--trading-mode combined" not in workflow
