"""Focused tests for production stock-signal to NIFTY-overlay diagnostics."""

from src.options_chain import expiry_status
from src.options_selector import resolve_option_underlying, select
from src.signal_engine import TradeSignal
from scripts.options_dry_run import build_diagnostic
from unittest.mock import patch


def _chain():
    rows = []
    for strike in (24900.0, 25000.0, 25100.0):
        rows.append({
            "strike": strike,
            "expiry": "19-Oct-2026",
            "CE": {"lastPrice": 120.0, "bidPrice": 118.0, "askPrice": 122.0,
                   "openInterest": 5000, "volume": 2000, "iv": 12.0},
            "PE": {"lastPrice": 110.0, "bidPrice": 108.0, "askPrice": 112.0,
                   "openInterest": 6000, "volume": 2100, "iv": 12.5},
        })
    return {
        "symbol": "NIFTY",
        "underlying": 25000.0,
        "expiries": ["19-Oct-2026"],
        "by_expiry": {"19-Oct-2026": rows},
    }


def test_stock_signal_uses_explicit_configured_nifty_overlay():
    mapping = resolve_option_underlying("NMDC.NS", "NIFTY")
    assert mapping == {
        "signal_underlying": "NMDC",
        "option_underlying": "NIFTY",
        "mapping": "configured_index_overlay",
    }
    signal = TradeSignal(symbol="NMDC.NS", action="SELL", confidence=78)
    with patch("src.options_selector._opt_cfg", return_value={
        "enabled": True, "underlying": "NIFTY", "lot_size": 65,
        "min_oi": 1000, "style": "spread", "delta_atm": 0.50,
        "delta_otm": 0.30, "max_iv_rank": 80,
    }):
        pick = select(signal, _chain(), expiry="19-Oct-2026", adx=30.0,
                      signal_underlying="NMDC.NS")
    assert pick.strategy == "bear-put-spread"
    assert pick.underlying == "NIFTY"
    assert pick.signal_underlying == "NMDC"
    assert pick.underlying_mapping == "configured_index_overlay"


def test_selector_rejects_wrong_chain_underlying():
    chain = _chain()
    chain["symbol"] = "BANKNIFTY"
    signal = TradeSignal(symbol="NMDC.NS", action="SELL", confidence=78)
    with patch("src.options_selector._opt_cfg", return_value={
        "enabled": True, "underlying": "NIFTY", "lot_size": 65,
        "min_oi": 1000, "style": "spread", "delta_atm": 0.50,
        "delta_otm": 0.30, "max_iv_rank": 80,
    }):
        pick = select(signal, chain, expiry="19-Oct-2026", adx=30.0)
    assert pick.strategy == "none"
    assert "underlying mismatch" in pick.reasoning[0]


def test_expiry_is_exchange_listed_but_has_zero_strikes():
    chain = _chain()
    chain["by_expiry"] = {}
    status = expiry_status(chain, "19-Oct-2026")
    assert status["listed_by_exchange"] is True
    assert status["strike_rows"] == 0
    assert status["reason"] == "zero_strikes_for_expiry"


def test_archived_diagnostic_keeps_failure_stage_and_readiness():
    plan = {
        "date": "2026-10-13",
        "generated_at": "2026-10-13T06:31:33+00:00",
        "candidates": [{"symbol": "NMDC.NS", "action": "SELL"}],
        "option_candidates": [{
            "symbol": "NMDC.NS",
            "action": "SELL",
            "setup_name": "composite-short",
            "option_legs": {
                "strategy": "none",
                "underlying": "NIFTY",
                "expiry": "19-Oct-2026",
                "legs": [],
                "reasoning": ["zero strikes for expiry"],
            },
        }],
    }
    report = build_diagnostic(plan, source="fixture-replay", chain=_chain())
    row = report["candidates"][0]
    assert row["rejection_stage"] == "contract_selection"
    assert row["expiry_status"]["reason"] == "usable_strikes"
    assert report["historical_options_pnl"] == "NOT_READY_FOR_OPTIONS_PNL"
    assert report["broker_orders_submitted"] is False


if __name__ == "__main__":
    for test in (
        test_stock_signal_uses_explicit_configured_nifty_overlay,
        test_selector_rejects_wrong_chain_underlying,
        test_expiry_is_exchange_listed_but_has_zero_strikes,
        test_archived_diagnostic_keeps_failure_stage_and_readiness,
    ):
        test()
        print(f"PASS: {test.__name__}")
