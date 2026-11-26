"""Production live-plan tests for two-leg long-volatility structures."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.options_intraday_plan import ALLOWED_STRATEGIES, build_intraday_plan


IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 10, 14, 9, 35, tzinfo=IST)
EXPIRY = "16-Oct-2026"


def _cfg(strategy):
    return {
        "options_intraday": {
            "strategies": [strategy],
            "max_quote_age_minutes": 15,
            "min_oi": 1000,
            "max_spread_pct": 5,
            "contract_specs": {
                "lot_size": 65,
                "provenance": "test broker instrument snapshot",
                "valid_from": "2026-01-01",
                "valid_until": "2026-12-31",
            },
        },
        "options": {"underlying": "NIFTY", "lot_size": 65, "strike_step": 50},
        "risk_management": {"capital": 500000},
    }


def _candles():
    return [
        {
            "timestamp": f"2026-10-14 {stamp}:00+05:30",
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
            "volume": 1000,
            "exchange": "NSE",
            "underlying_symbol": "NIFTY",
            "instrument_type": "index",
        }
        for stamp in ("09:15", "09:20", "09:25", "09:30")
    ]


def _row(strike, *, bid=9.8, ask=10.0):
    return {
        "strike": strike,
        "expiry": EXPIRY,
        "underlying_symbol": "NIFTY",
        "exchange": "NSE",
        "CE": {
            "bidPrice": bid,
            "askPrice": ask,
            "openInterest": 5000,
            "volume": 1000,
            "quote_timestamp": "2026-10-14T09:34:00+05:30",
        },
        "PE": {
            "bidPrice": bid - 1,
            "askPrice": ask - 1,
            "openInterest": 5000,
            "volume": 1000,
            "quote_timestamp": "2026-10-14T09:34:00+05:30",
        },
    }


def _chain(strikes=(100,)):
    return {
        "symbol": "NIFTY",
        "underlying": 100.0,
        "expiries": [EXPIRY],
        "by_expiry": {EXPIRY: [_row(strike) for strike in strikes]},
        "source": "nse",
        "retrieved_at": "2026-10-14T09:35:00+05:30",
        "diagnostics": {"retrieval": {"success": True}},
    }


def _fake_signal(offsets):
    return SimpleNamespace(
        allowed=True,
        entry_price=100.0,
        trigger_time="2026-10-14T09:30:00+05:30",
        reasoning="structure trigger",
        legs=[
            SimpleNamespace(kind=kind, side="BUY", offset=offset)
            for kind, offset in offsets
        ],
    )


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    import requests
    monkeypatch.setattr(
        requests.sessions.Session, "request",
        lambda *args, **kwargs: pytest.fail("unexpected HTTP request"))
    monkeypatch.delenv("TRIO_RESEARCH_STRATEGIES", raising=False)
    monkeypatch.delenv("TRIO_INTRADAY_DATA_PATH", raising=False)


@pytest.mark.parametrize(
    "strategy,offsets,strikes",
    [
        ("long_straddle", (("CE", 0), ("PE", 0)), (100,)),
        ("long_strangle", (("CE", 1), ("PE", -1)), (50, 150)),
    ],
)
def test_structure_strategy_reaches_live_selection(
        monkeypatch, strategy, offsets, strikes):
    from strategies.long_volatility_base import LongVolatilityStructure

    assert strategy in ALLOWED_STRATEGIES
    monkeypatch.setattr(
        LongVolatilityStructure, "evaluate_signal",
        lambda self, *args, **kwargs: _fake_signal(offsets))

    plan = build_intraday_plan(
        _cfg(strategy), now=NOW, candles=_candles(), chain=_chain(strikes))

    assert not plan.get("failed")
    assert len(plan["option_candidates"]) == 1
    candidate = plan["option_candidates"][0]
    assert candidate["strategy"] == strategy
    assert candidate["direction"] == "any"
    assert candidate["action"] == "BUY"
    legs = candidate["option_legs"]["legs"]
    assert len(legs) == 2
    assert [leg["kind"] for leg in legs] == ["CE", "PE"]
    assert all(leg["side"] == "BUY" and leg["premium_src"] == "live"
               for leg in legs)
    assert candidate["option_legs"]["strategy"] == strategy.replace("_", "-")
    assert candidate["option_legs"]["net_debit"] == round(
        sum(leg["premium"] for leg in legs), 2)
    assert candidate["option_legs"]["max_loss"] == round(
        candidate["option_legs"]["net_debit"] * 65, 2)
    if strategy == "long_strangle":
        assert [leg["strike"] for leg in legs] == [150.0, 50.0]


def test_structure_candidate_without_both_legs_is_rejected(monkeypatch):
    from strategies.long_volatility_base import LongVolatilityStructure

    monkeypatch.setattr(
        LongVolatilityStructure, "evaluate_signal",
        lambda self, *args, **kwargs: SimpleNamespace(
            allowed=True, entry_price=100.0,
            trigger_time="2026-10-14T09:30:00+05:30",
            reasoning="bad structure",
            legs=[SimpleNamespace(kind="CE", side="BUY", offset=0)]))

    plan = build_intraday_plan(
        _cfg("long_straddle"), now=NOW, candles=_candles(), chain=_chain())

    assert plan["option_candidates"] == []
    assert plan["option_chain_diagnostic"]["failure_reason"] in {
        "no_strategy_signal", "no_exact_eligible_long_contract"}
