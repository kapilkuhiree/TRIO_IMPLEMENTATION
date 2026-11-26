"""Offline validation tests for two-leg long-volatility option candidates.

Fixtures intentionally mirror the hand-built chain snapshots used by
``tests/test_options.py`` and ``tests/test_options_intraday_plan.py``: no
network, no broker, just a candidate plus a private chain snapshot.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from src.options_intraday import (
    load_options_intraday_config,
    validate_option_candidate,
)


IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 12, 25, 10, 0, tzinfo=IST)
EXPIRY = "26-Dec-2026"
STRIKE = 25800.0
QUOTE_TS = "2026-12-25T09:59:30+05:30"


@pytest.fixture(autouse=True)
def offline_environment(monkeypatch):
    """Keep the validation gate deterministic and network-free."""
    monkeypatch.delenv("TRIO_OPTIONS_LIVE", raising=False)
    monkeypatch.delenv("TRIO_DEMAND_SUPPLY", raising=False)
    monkeypatch.delenv("TRIO_TRADING_MODE", raising=False)


def _cfg():
    # The dedicated overlay supplies the strict paper-risk policy; the base
    # options block supplies the exchange/lot/strike/quote thresholds.
    return load_options_intraday_config({
        "options": {
            "underlying": "NIFTY",
            "exchange": "NSE",
            "lot_size": 65,
            "strike_step": 50,
            "min_oi": 1000,
            "max_spread_pct": 5,
        },
    })


def _quote(bid, ask):
    return {
        "bid": bid,
        "ask": ask,
        "openInterest": 5000,
        "volume": 1200,
        "quote_timestamp": QUOTE_TS,
    }


def _plan():
    return {"option_chain": {
        "symbol": "NIFTY",
        "exchange": "NSE",
        "expiries": [EXPIRY],
        "by_expiry": {EXPIRY: [{
            "strike": STRIKE,
            "expiry": EXPIRY,
            "CE": _quote(199.0, 200.0),
            "PE": _quote(201.0, 202.0),
        }]},
        "quote_timestamp": QUOTE_TS,
    }}


def _leg(kind):
    bid, ask = (199.0, 200.0) if kind == "CE" else (201.0, 202.0)
    return {
        "strike": STRIKE,
        "expiry": EXPIRY,
        "kind": kind,
        "side": "BUY",
        "premium": ask,
        "premium_src": "live",
        "bid": bid,
        "ask": ask,
        "oi": 5000,
        "open_interest": 5000,
        "volume": 1200,
        "quote_timestamp": QUOTE_TS,
        "trading_symbol": f"NIFTY26D26{int(STRIKE)}{kind}",
    }


def _candidate(option, *, action="BUY", direction="bullish"):
    return {
        "asset": "options",
        "action": action,
        "direction": direction,
        "signal_underlying": "NIFTY",
        "option_underlying": "NIFTY",
        "symbol": "NIFTY",
        "underlying": "NIFTY",
        "option_legs": option,
        "option_diagnostic": {"demand_supply": {"confirmed": True}},
    }


def _single_candidate():
    option = {
        "strategy": "long-call",
        "underlying": "NIFTY",
        "exchange": "NSE",
        "lot_size": 65,
        "expiry": EXPIRY,
        "legs": [_leg("CE")],
    }
    return _candidate(option)


def _structure_candidate(strategy="long-straddle", *, direction="any",
                         action="BUY", net_debit=402.0):
    option = {
        "strategy": strategy,
        "structure": ("LONG_STRADDLE" if "straddle" in strategy
                      else "LONG_STRANGLE"),
        "underlying": "NIFTY",
        "spot": STRIKE,
        "exchange": "NSE",
        "lot_size": 65,
        "expiry": EXPIRY,
        "legs": [_leg("CE"), _leg("PE")],
        "net_debit": net_debit,
        "maxLoss": 26130,
        "max_loss": 26130,
    }
    return _candidate(option, action=action, direction=direction)


def _validate(candidate):
    return validate_option_candidate(candidate, _plan(), _cfg(), now=NOW)


@pytest.mark.parametrize("strategy", [
    "long-straddle", "long-strangle", "long_straddle", "long_strangle",
])
@pytest.mark.parametrize("direction", [
    "any", "direction_neutral", "neutral", "bullish", "bearish",
])
def test_valid_structure_passes(strategy, direction):
    valid, reason = _validate(_structure_candidate(strategy, direction=direction))
    assert valid is True, reason


def test_existing_single_leg_candidate_still_passes():
    valid, reason = _validate(_single_candidate())
    assert valid is True, reason


@pytest.mark.parametrize("leg_count", [1, 3])
def test_wrong_leg_count_rejected(leg_count):
    candidate = _structure_candidate()
    ce, pe = deepcopy(candidate["option_legs"]["legs"])
    candidate["option_legs"]["legs"] = ([ce] if leg_count == 1
                                        else [ce, pe, deepcopy(ce)])
    valid, reason = _validate(candidate)
    assert valid is False
    assert reason == "long-volatility structure requires exactly two option legs"


@pytest.mark.parametrize("kinds", [("CE", "CE"), ("PE", "PE")])
def test_missing_ce_or_pe_leg_rejected(kinds):
    candidate = _structure_candidate()
    ce, pe = deepcopy(candidate["option_legs"]["legs"])
    candidate["option_legs"]["legs"] = [
        deepcopy(ce if kind == "CE" else pe) for kind in kinds]
    valid, reason = _validate(candidate)
    assert valid is False
    assert reason == "long-volatility structure requires one CE and one PE leg"


def test_structure_sell_action_rejected():
    valid, reason = _validate(_structure_candidate(action="SELL"))
    assert valid is False
    assert reason == "long-volatility structure requires BUY action"


def test_structure_sell_leg_rejected():
    candidate = _structure_candidate()
    candidate["option_legs"]["legs"][1]["side"] = "SELL"
    valid, reason = _validate(candidate)
    assert valid is False
    assert reason == "long-volatility structure requires two BUY legs"


@pytest.mark.parametrize("net_debit, expected", [
    (401.0, "net debit does not match leg premiums"),
    (0.0, "missing or invalid net debit"),
    (-1.0, "missing or invalid net debit"),
])
def test_net_debit_mismatch_rejected(net_debit, expected):
    candidate = _structure_candidate(net_debit=net_debit)
    valid, reason = _validate(candidate)
    assert valid is False
    assert reason == expected


def test_each_leg_must_match_its_exchange_quote():
    """The shared gate applies to BOTH legs, not just the first one."""
    candidate = _structure_candidate()
    candidate["option_legs"]["legs"][1]["ask"] = 205.0
    valid, reason = _validate(candidate)
    assert valid is False
    assert reason == "selected quote differs from exchange chain"


def test_each_leg_must_share_option_expiry():
    candidate = _structure_candidate()
    candidate["option_legs"]["legs"][1]["expiry"] = "02-Dec-2026"
    valid, reason = _validate(candidate)
    assert valid is False
    assert reason == "missing or inconsistent expiry"


def test_structure_unknown_direction_rejected():
    valid, reason = _validate(_structure_candidate(direction="sideways"))
    assert valid is False
    assert reason == "structure signal direction/action mismatch"
