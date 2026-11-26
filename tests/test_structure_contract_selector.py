"""Unit tests for two-leg live structure contract selection (offline)."""

from copy import deepcopy

import pytest

from contract_selector import select_structure_contracts


EXPIRY = "2026-12-24"
EXCHANGE = "NFO"
SPOT = 23723.0
STEP = 50
ATM = 23700.0


def _contract(kind, strike, *, bid=100.0, ask=100.5, oi=5000, volume=800,
              timestamp="2026-11-25T09:30:00+05:30", expiry=EXPIRY,
              exchange=EXCHANGE, lot_size=75):
    return {
        "underlying_symbol": "NIFTY",
        "trading_symbol": f"NIFTY{int(strike)}{kind}",
        "option_type": kind,
        "strike": float(strike),
        "expiry": expiry,
        "exchange": exchange,
        "bid": bid,
        "ask": ask,
        "volume": volume,
        "open_interest": oi,
        "quote_timestamp": timestamp,
        "lot_size": lot_size,
    }


def _specs(**overrides):
    specs = {"lot_size": 75, "strike_interval": STEP, "expiry_preference": "weekly"}
    specs.update(overrides)
    return specs


def _straddle_chain():
    return [
        _contract("CE", ATM - STEP),
        _contract("CE", ATM),
        _contract("PE", ATM),
        _contract("PE", ATM + STEP),
    ]


def _strangle_chain():
    return [
        _contract("CE", ATM + STEP),
        _contract("PE", ATM - STEP),
        _contract("CE", ATM),
        _contract("PE", ATM),
    ]


def _legs(ce_offset=0, pe_offset=0):
    return [
        {"kind": "CE", "side": "BUY", "offset": ce_offset},
        {"kind": "PE", "side": "BUY", "offset": pe_offset},
    ]


def _spread_pct(contract):
    bid, ask = contract["bid"], contract["ask"]
    return (ask - bid) / ((ask + bid) / 2.0) * 100.0


def test_straddle_selects_both_atm_legs_with_offsets_zero():
    legs = _legs()
    contracts = _straddle_chain()

    result = select_structure_contracts(
        "nifty", SPOT, contracts, _specs(), legs,
        expected_exchange=EXCHANGE, expected_expiry=EXPIRY,
    )

    assert "selected_contract" in result
    selected = result["selected_contract"]
    assert selected["__structure_legs__"] is True
    assert selected["option_type"] == "CE+PE"
    assert selected["strike"] == ATM
    assert selected["underlying_symbol"] == "NIFTY"
    assert selected["underlying_price"] == SPOT
    assert selected["expiry"] == EXPIRY
    assert selected["exchange"] == EXCHANGE
    assert selected["ce_contract"]["option_type"] == "CE"
    assert selected["pe_contract"]["option_type"] == "PE"
    assert selected["ce_contract"]["strike"] == ATM
    assert selected["pe_contract"]["strike"] == ATM


def test_strangle_selects_plus_one_ce_and_minus_one_pe():
    result = select_structure_contracts(
        "NIFTY", SPOT, _strangle_chain(), _specs(), _legs(1, -1),
        expected_exchange=EXCHANGE, expected_expiry=EXPIRY,
    )

    assert "selected_contract" in result
    selected = result["selected_contract"]
    assert selected["ce_contract"]["strike"] == ATM + STEP
    assert selected["pe_contract"]["strike"] == ATM - STEP
    assert selected["ce_contract"]["option_type"] == "CE"
    assert selected["pe_contract"]["option_type"] == "PE"
    assert [leg["offset"] for leg in selected["structure_legs"]] == [1, -1]


def test_missing_leg_is_rejected_with_rejection_reasons():
    contracts = [_contract("CE", ATM)]

    result = select_structure_contracts(
        "NIFTY", SPOT, contracts, _specs(), _legs(),
        expected_exchange=EXCHANGE, expected_expiry=EXPIRY,
    )

    assert "selected_contract" not in result
    rejection = result["rejection"]
    assert "PE" in rejection["reason"]
    assert any("PE" in reason for reason in rejection["rejection_reasons"])


def test_expiry_mismatch_is_rejected():
    result = select_structure_contracts(
        "NIFTY", SPOT, _straddle_chain(), _specs(), _legs(),
        expected_exchange=EXCHANGE, expected_expiry="2026-12-31",
    )

    assert "selected_contract" not in result
    rejection = result["rejection"]
    assert rejection["rejection_reasons"]
    assert any("expiry" in reason for reason in rejection["rejection_reasons"])


def test_combined_quote_shape_and_derived_fields():
    ce = _contract("CE", ATM, bid=100.0, ask=101.0, oi=9000, volume=700,
                   timestamp="2026-11-25T09:30:00+05:30")
    pe = _contract("PE", ATM, bid=200.0, ask=203.0, oi=4000, volume=500,
                   timestamp="2026-11-25T09:32:00+05:30")
    legs = _legs()
    contracts = [ce, pe]

    result = select_structure_contracts(
        "NIFTY", SPOT, contracts, _specs(), legs,
        expected_exchange=EXCHANGE, expected_expiry=EXPIRY,
    )
    selected = result["selected_contract"]

    assert set(selected) == {
        "__structure_legs__", "structure_legs", "underlying_symbol",
        "underlying_price", "strike", "option_type", "expiry", "exchange",
        "lot_size", "ce_contract", "pe_contract", "bid", "ask",
        "open_interest", "volume", "quote_timestamp", "contract_rank_score",
    }
    assert selected["bid"] == pytest.approx(300.0)
    assert selected["ask"] == pytest.approx(304.0)
    assert selected["open_interest"] == pytest.approx(4000.0)
    assert selected["volume"] == pytest.approx(500.0)
    assert selected["quote_timestamp"] == "2026-11-25T09:32:00+05:30"
    assert selected["lot_size"] == 75
    average_spread = (_spread_pct(ce) + _spread_pct(pe)) / 2.0
    assert selected["contract_rank_score"] == pytest.approx(
        100.0 - average_spread + min(100 / 10000.0, 10.0)
    )
    assert selected["ce_contract"] == ce
    assert selected["pe_contract"] == pe
    assert selected["ce_contract"] is not ce
    assert selected["structure_legs"] == legs
    assert selected["structure_legs"] is not legs

    legs[0]["offset"] = 99
    contracts[0]["bid"] = 1.0
    assert selected["structure_legs"][0]["offset"] == 0
    assert selected["ce_contract"]["bid"] == 100.0


def test_ineligible_leg_is_rejected():
    contracts = [
        _contract("CE", ATM, oi=10),
        _contract("PE", ATM),
    ]

    result = select_structure_contracts(
        "NIFTY", SPOT, contracts, _specs(), _legs(),
        expected_exchange=EXCHANGE, expected_expiry=EXPIRY,
    )

    assert "selected_contract" not in result
    assert any("open interest" in reason.lower()
               for reason in result["rejection"]["rejection_reasons"])


def test_rejects_two_buy_ce_legs_and_sell_legs():
    two_calls = [{"kind": "CE", "side": "BUY", "offset": 0},
                 {"kind": "CE", "side": "BUY", "offset": 1}]
    sell_legs = [{"kind": "CE", "side": "SELL", "offset": 0},
                 {"kind": "pe", "side": "BUY", "offset": 0}]

    for legs in (two_calls, sell_legs):
        result = select_structure_contracts(
            "NIFTY", SPOT, _straddle_chain(), _specs(), deepcopy(legs),
            expected_exchange=EXCHANGE, expected_expiry=EXPIRY,
        )
        assert "selected_contract" not in result
        assert result["rejection"]["rejection_reasons"]


def test_uses_strike_step_fallback_when_interval_is_absent():
    specs = {"lot_size": 75, "strike_step": STEP}
    result = select_structure_contracts(
        "NIFTY", SPOT, _strangle_chain(), specs, _legs(1, -1),
        expected_exchange=EXCHANGE, expected_expiry=EXPIRY,
    )

    assert "selected_contract" in result
    selected = result["selected_contract"]
    assert selected["ce_contract"]["strike"] == ATM + STEP
    assert selected["pe_contract"]["strike"] == ATM - STEP
