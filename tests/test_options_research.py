"""Tests for the no-fabrication options research gate."""

from src.options_research import (
    STRATEGY_CANDIDATES,
    assess_historical_option_rows,
    build_research_manifest,
    comparison_plan,
)


def _row():
    return {
        "timestamp": "2026-10-15T09:20:00+05:30",
        "underlying": "NIFTY",
        "trading_symbol": "NIFTY26OCT25000CE",
        "expiry": "2026-10-29",
        "strike": 25000,
        "option_type": "CE",
        "open": 100,
        "high": 105,
        "low": 99,
        "close": 103,
        "bid": 102,
        "ask": 104,
    }


def test_comparison_plan_has_all_candidates_and_no_claim():
    plan = comparison_plan()
    assert tuple(item["id"] for item in plan["strategies"]) == STRATEGY_CANDIDATES
    assert plan["options_pnl_status"] == "NOT_READY_FOR_OPTIONS_PNL"
    assert plan["profitability_claim_allowed"] is False


def test_missing_or_synthetic_premium_data_keeps_gate_closed():
    empty = assess_historical_option_rows(None)
    synthetic = assess_historical_option_rows([_row()], provenance="bs_model")
    assert empty["options_pnl_status"] == "NOT_READY_FOR_OPTIONS_PNL"
    assert synthetic["options_pnl_status"] == "NOT_READY_FOR_OPTIONS_PNL"
    assert synthetic["profitability_claim_allowed"] is False


def test_validated_rows_can_open_evidence_gate():
    result = assess_historical_option_rows([_row()],
                                           provenance="validated_historical")
    assert result["options_pnl_status"] == "READY_FOR_OPTIONS_PNL"
    assert result["profitability_claim_allowed"] is True


def test_manifest_hashes_inputs_and_records_policy(tmp_path):
    source = tmp_path / "premium.json"
    source.write_text("[]", encoding="utf-8")
    manifest = build_research_manifest(
        input_paths=[source], parameters={"seed": 0}, provenance="bs_model")
    assert len(manifest["inputs"][0]["sha256"]) == 64
    assert manifest["comparison"]["lookahead_policy"] == (
        "no future candles, quotes, or contract rows")
    assert manifest["evidence"]["profitability_claim_allowed"] is False
