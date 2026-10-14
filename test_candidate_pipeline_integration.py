"""Executable integration tests for the research-only candidate pipeline.

All option and underlying rows in this file are labelled synthetic fixtures.
They prove software connectivity only; they are not historical performance
data and must never be used as a profitability report.
"""

from datetime import timedelta

from candidate_pipeline import CandidatePipeline, OptionsCandidate
from contract_selector import select_option_contract
from experimental_risk import (
    configure_experimental_risk,
    get_experimental_risk_state,
    init_experimental_risk,
)
from option_data_schema import NOT_READY_FOR_OPTIONS_PNL, options_pnl_readiness


def option_row(timestamp, option_type, bid, ask, *, oi=2500, strike=23500.0):
    return {
        "timestamp": timestamp,
        "strike": strike,
        "expiry": "2026-01-29",
        "option_type": option_type,
        "open": ask,
        "high": ask + 1,
        "low": bid,
        "close": ask,
        "volume": 5000,
        "open_interest": oi,
        "bid": bid,
        "ask": ask,
        "exchange": "NSE",
        "underlying_symbol": "NIFTY",
        "lot_size": 65,
    }


def underlying_row(timestamp, high, low, close):
    return {
        "timestamp": timestamp,
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1000,
        "exchange": "NSE",
        "underlying_symbol": "NIFTY",
        "instrument_type": "index",
    }


def fixture_rows():
    # Synthetic option and underlying candles used exclusively for integration.
    ce = [
        option_row("2026-01-01T09:30:00+05:30", "CE", 10.0, 10.2),
        option_row("2026-01-01T09:35:00+05:30", "CE", 11.8, 12.0),
    ]
    pe = [
        option_row("2026-01-01T09:30:00+05:30", "PE", 12.0, 12.2),
        option_row("2026-01-01T09:35:00+05:30", "PE", 10.0, 10.2),
    ]
    bullish_underlying = [
        underlying_row("2026-01-01T09:35:00+05:30", 23560.0, 23505.0, 23550.0)
    ]
    bearish_underlying = [
        underlying_row("2026-01-01T09:35:00+05:30", 23495.0, 23440.0, 23450.0)
    ]
    return ce, pe, bullish_underlying, bearish_underlying


def make_candidate(direction, option_type, option_rows, underlying_rows, candidate_id):
    return OptionsCandidate(
        candidate_id=candidate_id,
        signal_timestamp="2026-01-01T09:30:00+05:30",
        underlying_symbol="NIFTY",
        strategy_name="fixture-integration",
        direction=direction,
        option_type=option_type,
        entry_price=23500.0,       # underlying signal price
        stop_loss=23450.0 if direction == "bullish" else 23550.0,
        target=23550.0 if direction == "bullish" else 23450.0,
        signal_reason="synthetic fixture signal; not historical data",
        underlying_price_at_signal=23500.0,
        exchange="NSE",
        option_candles=option_rows,
        underlying_candles=underlying_rows,
    )


def new_pipeline():
    return CandidatePipeline(
        csv_candles_path="",
        initial_equity=500000.0,
        risk_per_trade_pct=0.30,
        max_daily_loss_pct=1.0,
        max_trades_per_day=5,
        max_consecutive_losses=2,
        time_cutoff_ist="14:30",
        max_open_positions=5,
    )


def test_bullish_ce_path():
    ce, _, bullish, _ = fixture_rows()
    pipeline = new_pipeline()
    candidate = make_candidate("bullish", "CE", ce, bullish, "FIXTURE_CE_001")
    pipeline.add_candidate(candidate)
    selected = pipeline.select_contracts_for_candidates()
    assert selected[0].contract_eligibility == (True, "Eligible contract selected")
    assert selected[0].selected_contract["option_type"] == "CE"
    ranked = pipeline.rank_candidates()
    approved, rejected = pipeline.approve_candidates_with_risk(ranked)
    assert len(approved) == 1 and not rejected
    result = pipeline.execute_approved_trades(approved)
    assert result["total_trades"] == 1
    trade = result["executed_trades"][0]
    assert trade["candidate_id"] == "FIXTURE_CE_001"
    assert trade["option_type"] == "CE"
    assert trade["exit_reason"] == "target"
    assert trade["option_entry_premium"] == 10.2
    assert trade["option_exit_premium"] == 11.8
    assert trade["lot_size"] == 65
    assert trade["realized_pnl"] < (11.8 - 10.2) * 130
    return trade


def test_bearish_pe_path():
    _, pe, _, bearish = fixture_rows()
    pipeline = new_pipeline()
    candidate = make_candidate("bearish", "PE", pe, bearish, "FIXTURE_PE_001")
    pipeline.add_candidate(candidate)
    pipeline.select_contracts_for_candidates()
    assert candidate.contract_eligibility[0]
    assert candidate.selected_contract["option_type"] == "PE"
    approved, rejected = pipeline.approve_candidates_with_risk(pipeline.rank_candidates())
    assert len(approved) == 1 and not rejected
    result = pipeline.execute_approved_trades(approved)
    assert result["total_trades"] == 1
    trade = result["executed_trades"][0]
    assert trade["candidate_id"] == "FIXTURE_PE_001"
    assert trade["exit_reason"] == "target"
    assert trade["option_exit_premium"] == 10.0
    return trade


def test_direction_and_invalid_contract_rejections():
    ce, _, _, _ = fixture_rows()
    specs = new_pipeline().contract_specs
    wrong_direction = [dict(ce[0], option_type="PE")]
    result = select_option_contract(
        "NIFTY", 23500, "bullish", wrong_direction, specs,
        min_oi=1000, expected_exchange="NSE",
    )
    assert "selected_contract" not in result
    assert "option_type" in result["rejection"]["rejection_reasons"][0]

    invalids = [
        (dict(ce[0], underlying_symbol="BANKNIFTY"), "underlying"),
        (dict(ce[0], exchange="BSE"), "exchange"),
        (dict(ce[0], strike=0), "strike"),
        (dict(ce[0], expiry="not-a-date"), "expiry"),
        (dict(ce[0], ask=0), "price"),
        (dict(ce[0], bid=100, ask=120), "spread"),
        (dict(ce[0], open_interest=1), "Open interest"),
        (dict(ce[0], lot_size=None), "lot_size"),
    ]
    for row, expected in invalids:
        result = select_option_contract(
            "NIFTY", 23500, "bullish", [row], specs,
            min_oi=1000, max_spread_pct=5, expected_exchange="NSE",
        )
        assert "selected_contract" not in result
        text = str(result["rejection"])
        assert expected.lower() in text.lower(), (expected, text)

    empty = select_option_contract(
        "NIFTY", 23500, "bullish", [], specs,
        min_oi=1000, expected_exchange="NSE",
    )
    assert "selected_contract" not in empty
    assert "No eligible" in empty["rejection"]["reason"]


def test_risk_rejections_do_not_enter_ledger():
    ce, _, bullish, _ = fixture_rows()
    pipeline = new_pipeline()
    candidate = make_candidate("bullish", "CE", ce, bullish, "FIXTURE_RISK_001")
    pipeline.add_candidate(candidate)
    pipeline.select_contracts_for_candidates()

    state = get_experimental_risk_state()
    state.positions["NIFTY"] = {"candidate_id": "already-open"}
    state.open_positions = state.max_open_positions
    approved, rejected = pipeline.approve_candidates_with_risk(pipeline.rank_candidates())
    assert not approved
    assert rejected and "MAX OPEN POSITIONS" in rejected[0]["reason"]
    assert pipeline.daily_pnl == 0.0
    assert not pipeline.approved_trades

    # The duplicate guard is independently exercised after the position-count
    # guard is cleared.
    state.open_positions = 0
    state.positions = {"NIFTY": {"candidate_id": "already-open"}}
    approved, rejected = pipeline.approve_candidates_with_risk(pipeline.rank_candidates())
    assert not approved
    assert rejected and "DUPLICATE ENTRY" in rejected[0]["reason"]
    assert pipeline.daily_pnl == 0.0


def _prepare_candidate(pipeline, candidate_id="FIXTURE_RISK"):
    ce, _, bullish, _ = fixture_rows()
    candidate = make_candidate("bullish", "CE", ce, bullish, candidate_id)
    pipeline.add_candidate(candidate)
    pipeline.select_contracts_for_candidates()
    return candidate


def test_all_global_risk_rejections():
    # Insufficient capital for one complete option lot.
    pipeline = CandidatePipeline(
        csv_candles_path="", initial_equity=1000.0, risk_per_trade_pct=0.30
    )
    _prepare_candidate(pipeline, "FIXTURE_CAPITAL")
    approved, rejected = pipeline.approve_candidates_with_risk(pipeline.rank_candidates())
    assert not approved and "POSITION SIZE" in rejected[0]["reason"]
    assert pipeline.daily_pnl == 0.0

    # Daily loss limit.
    pipeline = new_pipeline()
    candidate = _prepare_candidate(pipeline, "FIXTURE_DAILY_LOSS")
    get_experimental_risk_state().daily_pnl = -6000.0
    approved, rejected = pipeline.approve_candidates_with_risk(pipeline.rank_candidates())
    assert not approved and "DAILY LOSS LIMIT" in rejected[0]["reason"]
    assert candidate.trade_executed is False

    # Daily entry ceiling.
    pipeline = new_pipeline()
    _prepare_candidate(pipeline, "FIXTURE_MAX_DAILY")
    pipeline.daily_trade_count = 5
    approved, rejected = pipeline.approve_candidates_with_risk(pipeline.rank_candidates())
    assert not approved and "MAX TRADES PER DAY" in rejected[0]["reason"]

    # Consecutive-loss guard is observational during approval.
    pipeline = new_pipeline()
    _prepare_candidate(pipeline, "FIXTURE_CONSECUTIVE")
    state = get_experimental_risk_state()
    state.consecutive_losses = 2
    approved, rejected = pipeline.approve_candidates_with_risk(pipeline.rank_candidates())
    assert not approved and "CONSECUTIVE LOSSES" in rejected[0]["reason"]
    assert state.consecutive_losses == 2

    # Correlated exposure is portfolio-wide, not per-symbol.
    pipeline = CandidatePipeline(
        csv_candles_path="", correlated_groups=[["NIFTY", "FINNIFTY"]]
    )
    _prepare_candidate(pipeline, "FIXTURE_CORRELATED")
    state = get_experimental_risk_state()
    state.positions["FINNIFTY"] = {"candidate_id": "other-open"}
    approved, rejected = pipeline.approve_candidates_with_risk(pipeline.rank_candidates())
    assert not approved and "CORRELATED EXPOSURE" in rejected[0]["reason"]


def test_exit_policy_stop_and_eod():
    ce, _, _, _ = fixture_rows()
    # Stop fixture: the underlying crosses stop before any target.
    stop_rows = [underlying_row("2026-01-01T09:35:00+05:30", 23510, 23440, 23450)]
    pipeline = new_pipeline()
    candidate = make_candidate("bullish", "CE", ce, stop_rows, "FIXTURE_STOP_001")
    pipeline.add_candidate(candidate)
    pipeline.select_contracts_for_candidates()
    approved, _ = pipeline.approve_candidates_with_risk(pipeline.rank_candidates())
    result = pipeline.execute_approved_trades(approved)
    assert result["executed_trades"][0]["exit_reason"] == "stop_loss"

    # EOD fixture: no trigger, but a later real bid closes the position.
    eod_rows = [underlying_row("2026-01-01T09:35:00+05:30", 23520, 23480, 23500)]
    pipeline = new_pipeline()
    candidate = make_candidate("bullish", "CE", ce, eod_rows, "FIXTURE_EOD_001")
    candidate.target = 23600
    candidate.stop_loss = 23400
    pipeline.add_candidate(candidate)
    pipeline.select_contracts_for_candidates()
    approved, _ = pipeline.approve_candidates_with_risk(pipeline.rank_candidates())
    result = pipeline.execute_approved_trades(approved)
    assert result["executed_trades"][0]["exit_reason"] == "end_of_day"


def test_data_readiness_guard():
    ce, _, _, _ = fixture_rows()
    assert options_pnl_readiness([], provenance="unavailable") == NOT_READY_FOR_OPTIONS_PNL
    # Even complete-looking fixture rows remain non-historical unless the
    # caller explicitly supplies independently validated historical provenance.
    assert options_pnl_readiness(ce, provenance="synthetic_fixture") == NOT_READY_FOR_OPTIONS_PNL


def run_all():
    tests = [
        test_bullish_ce_path,
        test_bearish_pe_path,
        test_direction_and_invalid_contract_rejections,
        test_risk_rejections_do_not_enter_ledger,
        test_all_global_risk_rejections,
        test_exit_policy_stop_and_eod,
        test_data_readiness_guard,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print("Synthetic fixtures only: no historical profitability claim")


if __name__ == "__main__":
    run_all()
