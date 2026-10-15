"""Offline acceptance tests for the production options-intraday planner."""

from datetime import datetime, timedelta, timezone
import pytest
from src.options_intraday_plan import build_intraday_plan


IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 10, 14, 9, 35, tzinfo=IST)
EXPIRY = "16-Oct-2026"


def _cfg(strategies=None, *, with_specs=True):
    mode = {
        "strategies": strategies or ["orb", "pullback", "momentum"],
        "max_quote_age_minutes": 15,
        "min_oi": 1000,
        "max_spread_pct": 5,
    }
    if with_specs:
        mode["contract_specs"] = {
            "lot_size": 65,
            "provenance": "test broker instrument snapshot",
            "valid_from": "2026-01-01",
            "valid_until": "2026-12-31",
        }
    else:
        # Explicitly override the production overlay for this negative case.
        mode["contract_specs"] = {
            "NIFTY": {
                "lot_size": 65,
                "provenance": "",
                "valid_from": "",
                "valid_until": "",
            },
        }
    return {
        "options_intraday": mode,
        "options": {"underlying": "NIFTY", "lot_size": 65},
        "risk_management": {"capital": 500000},
    }


def _candles():
    values = [
        ("09:15", 100, 101, 99, 100),
        ("09:20", 100, 101, 99, 100),
        ("09:25", 100, 101, 99, 100),
        ("09:30", 100, 103, 100, 102),
    ]
    return [
        {
            "timestamp": f"2026-10-14 {stamp}:00+05:30",
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1000,
            "exchange": "NSE",
            "underlying_symbol": "NIFTY",
            "instrument_type": "index",
        }
        for stamp, open_price, high, low, close in values
    ]


def _chain(*, quote_timestamp="2026-10-14T09:34:00+05:30", rows=True,
           symbol="NIFTY"):
    data = {
        "symbol": symbol,
        "underlying": 102.0,
        "expiries": [EXPIRY],
        "by_expiry": {},
        "source": "nse",
        "retrieved_at": "2026-10-14T09:35:00+05:30",
        "diagnostics": {"retrieval": {"success": True}},
    }
    if rows:
        data["by_expiry"][EXPIRY] = [{
            "strike": 100,
            "expiry": EXPIRY,
            "underlying_symbol": "NIFTY",
            "exchange": "NSE",
            "CE": {
                "bidPrice": 9.8,
                "askPrice": 10.0,
                "openInterest": 5000,
                "volume": 1000,
                "quote_timestamp": quote_timestamp,
            },
            "PE": {
                "bidPrice": 8.8,
                "askPrice": 9.0,
                "openInterest": 5000,
                "volume": 1000,
                "quote_timestamp": quote_timestamp,
            },
        }]
    return data


def _patched_signal(option_type="CE", signal_type="breakout"):
    return {
        "allowed": True,
        "signal_type": signal_type,
        "option_type": option_type,
        "entry_price": 102,
        "stop_loss": 99,
        "target": 105,
        "reasoning": "patched evaluate_signal boundary",
    }


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    """A scoped network ban applies only to this production-plan test module."""
    import requests
    monkeypatch.setattr(requests.sessions.Session, "request",
                        lambda *args, **kwargs: pytest.fail("unexpected HTTP request"))
    monkeypatch.delenv("TRIO_RESEARCH_STRATEGIES", raising=False)
    monkeypatch.delenv("TRIO_INTRADAY_DATA_PATH", raising=False)


@pytest.mark.parametrize("strategy", ["orb", "pullback", "momentum"])
def test_each_configured_strategy_reaches_live_selection(monkeypatch, strategy):
    from strategies import orb_strategy, trend_pullback_strategy, momentum_continuation_strategy

    monkeypatch.setenv("TRIO_RESEARCH_STRATEGIES", strategy)
    monkeypatch.setattr(orb_strategy.OrbStrategy, "evaluate_signal",
                        lambda *args, **kwargs: _patched_signal("CE"))
    monkeypatch.setattr(trend_pullback_strategy.TrendPullbackStrategy, "evaluate_signal",
                        lambda *args, **kwargs: _patched_signal("CE"))
    monkeypatch.setattr(momentum_continuation_strategy.MomentumContinuationStrategy,
                        "evaluate_signal", lambda *args, **kwargs: _patched_signal("CE"))

    plan = build_intraday_plan(_cfg(), now=NOW, candles=_candles(), chain=_chain())

    assert plan["candidates"] == []
    assert len(plan["option_candidates"]) == 1
    candidate = plan["option_candidates"][0]
    assert candidate["strategy"] == strategy
    assert candidate["signal"] == "BUY"
    assert candidate["order_side"] == "BUY"
    assert candidate["signal_timestamp"] == "2026-10-14T09:35:00+05:30"
    assert candidate["option_legs"]["strategy"] == "long-call"
    assert candidate["option_legs"]["legs"][0]["premium"] == 10.0
    assert candidate["option_legs"]["legs"][0]["premium_src"] == "live"


def test_real_orb_signal_uses_completion_time_and_no_backtest(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("production plan called a backtest wrapper")

    monkeypatch.setattr("strategies.orb_strategy.run_orb_backtest", forbidden)
    monkeypatch.setattr("strategies.trend_pullback_strategy.run_pullback_backtest", forbidden)
    monkeypatch.setattr("strategies.momentum_continuation_strategy.run_momentum_backtest", forbidden)

    plan = build_intraday_plan(_cfg(["orb"]), now=NOW, candles=_candles(), chain=_chain())

    assert len(plan["option_candidates"]) == 1
    assert plan["option_candidates"][0]["direction"] == "bullish"
    assert plan["option_candidates"][0]["signal_timestamp"].endswith("09:35:00+05:30")


def test_bearish_signal_is_sell_intent_but_long_put_order(monkeypatch):
    from strategies.orb_strategy import OrbStrategy

    monkeypatch.setenv("TRIO_RESEARCH_STRATEGIES", "orb")
    monkeypatch.setattr(OrbStrategy, "evaluate_signal",
                        lambda *args, **kwargs: _patched_signal("PE", "breakdown"))
    plan = build_intraday_plan(_cfg(["orb"]), now=NOW,
                               candles=_candles(), chain=_chain())

    candidate = plan["option_candidates"][0]
    assert candidate["direction"] == "bearish"
    assert candidate["signal"] == "SELL"
    assert candidate["order_side"] == "BUY"
    assert candidate["option_legs"]["strategy"] == "long-put"
    assert candidate["option_legs"]["legs"][0]["kind"] == "PE"


def test_default_mode_keeps_existing_premarket_screen_flow(monkeypatch):
    from scripts import premarket

    called = {}
    monkeypatch.delenv("TRIO_TRADING_MODE", raising=False)
    monkeypatch.setattr(premarket, "load_config", lambda: {
        "options": {"enabled": False},
        "screener": {"min_rank": 55, "min_confidence": 70},
    })
    monkeypatch.setattr(premarket, "resolve_basket", lambda universe: ["RELIANCE.NS"])

    class Signal:
        confidence = 80

    class ScreenCandidate:
        rank = 80
        signal = Signal()

        def to_dict(self):
            return {"symbol": "RELIANCE.NS", "action": "BUY", "rank": 80}

    def fake_screen(**kwargs):
        called.update(kwargs)
        return [ScreenCandidate()]

    monkeypatch.setattr(premarket, "screen", fake_screen)
    plan = premarket.build_plan("nifty100", "1d", 1, with_options=False)

    assert called["symbols"] == ["RELIANCE.NS"]
    assert len(plan["candidates"]) == 1
    assert plan["option_candidates"] == []
    assert plan["trading_mode"] == "production"


@pytest.mark.parametrize("bad_rows", [False, True])
def test_empty_or_wrong_expiry_rows_are_rejected(bad_rows):
    chain = _chain(rows=not bad_rows)
    if bad_rows:
        chain["by_expiry"][EXPIRY] = [{"strike": 100, "expiry": ""}]
    else:
        chain["by_expiry"] = {}
    plan = build_intraday_plan(_cfg(["orb"]), now=NOW, candles=_candles(), chain=chain)
    assert plan["option_candidates"] == []
    assert plan["option_chain_diagnostic"]["failure_reason"] in {
        "invalid_or_empty_expiry_rows", "no_exact_eligible_long_contract",
        "no_fresh_live_quotes",
    }


@pytest.mark.parametrize("timestamp", [
    "2026-10-14T09:00:00+05:30",
    "not-a-timestamp",
])
def test_old_or_bad_chain_quote_is_rejected(timestamp):
    plan = build_intraday_plan(
        _cfg(["orb"]), now=NOW, candles=_candles(),
        chain=_chain(quote_timestamp=timestamp),
    )
    assert plan["option_candidates"] == []
    assert plan["option_chain_diagnostic"]["failure_reason"] == "no_fresh_live_quotes"


def test_wrong_identity_and_missing_verified_specs_are_rejected():
    wrong_identity = _chain()
    wrong_identity["by_expiry"][EXPIRY][0]["underlying_symbol"] = "BANKNIFTY"
    plan = build_intraday_plan(_cfg(["orb"]), now=NOW,
                               candles=_candles(), chain=wrong_identity)
    assert plan["option_candidates"] == []

    plan = build_intraday_plan(_cfg(["orb"], with_specs=False), now=NOW,
                               candles=_candles(), chain=_chain())
    assert plan["option_candidates"] == []
    assert "contract_specs" in plan["option_chain_diagnostic"]["failure_reason"]


@pytest.mark.parametrize("mutate", [
    lambda rows: rows.append(dict(rows[-1])),
    lambda rows: rows.pop(1),
    lambda rows: rows[-1].update({"timestamp": "2026-10-14T09:35:00+05:30"}),
    lambda rows: rows[-1].update({"high": float("nan")}),
    lambda rows: rows[-1].update({"low": 200}),
    lambda rows: rows[-1].update({"timestamp": "2026-10-14T09:30:00"}),
])
def test_invalid_candle_boundaries_fail_closed(mutate):
    rows = _candles()
    mutate(rows)
    plan = build_intraday_plan(_cfg(["orb"]), now=NOW, candles=rows, chain=_chain())
    assert plan["option_candidates"] == []
    assert not plan["intraday_data_diagnostic"]["valid"]


def test_latest_complete_bar_must_be_current_and_fresh():
    old = _candles()
    for row in old:
        row["timestamp"] = row["timestamp"].replace("2026-10-14", "2026-10-13")
    plan = build_intraday_plan(_cfg(["orb"]), now=NOW, candles=old, chain=_chain())
    assert plan["option_candidates"] == []
    assert "no_completed_bar_for_current_ist_date" in plan["intraday_data_diagnostic"]["errors"]
    plan = build_intraday_plan(_cfg(["orb"]), now=NOW + timedelta(minutes=16),
                               candles=_candles(), chain=_chain())
    assert plan["option_candidates"] == []
    assert any("stale_latest_bar" in error for error in plan["intraday_data_diagnostic"]["errors"])


def test_chain_http_time_cannot_replace_exchange_timestamp():
    chain = _chain(quote_timestamp=None)
    assert chain["retrieved_at"]
    plan = build_intraday_plan(_cfg(["orb"]), now=NOW, candles=_candles(), chain=chain)
    assert plan["option_candidates"] == []
    assert plan["option_chain_diagnostic"]["failure_reason"] == "no_fresh_live_quotes"


def test_provider_uses_nifty_5m_and_cache_bypassing_chain(monkeypatch):
    import pandas as pd
    from types import SimpleNamespace
    import src.data_fetcher
    import src.options_chain
    calls = []
    frame = pd.DataFrame(_candles()).set_index("timestamp")
    frame.index = pd.to_datetime(frame.index)
    monkeypatch.setattr(src.data_fetcher, "fetch_market_data",
                        lambda symbol, tf: (calls.append((symbol, tf)) or
                                           SimpleNamespace(ohlcv=frame, source="test")))
    monkeypatch.setattr(src.options_chain, "fetch_chain",
                        lambda symbol, use_cache: (calls.append((symbol, use_cache)) or _chain()))
    plan = build_intraday_plan(_cfg(["orb"]), now=NOW)
    assert calls == [("^NSEI", "5m"), ("NIFTY", False)]
    assert plan["option_candidates"]


def test_production_plan_does_not_construct_or_consult_experimental_pipeline(monkeypatch):
    import candidate_pipeline
    def forbidden(*args, **kwargs):
        pytest.fail("experimental stateful pipeline boundary called")
    monkeypatch.setattr(candidate_pipeline.CandidatePipeline, "__init__", forbidden)
    monkeypatch.setattr(candidate_pipeline, "get_experimental_risk_state", forbidden)
    monkeypatch.setattr(candidate_pipeline, "init_experimental_risk", forbidden)
    plan = build_intraday_plan(_cfg(["orb"]), now=NOW,
                               candles=_candles(), chain=_chain())
    assert plan["option_candidates"]


def test_injected_path_makes_no_provider_or_broker_network_call(monkeypatch):
    import src.data_fetcher
    import src.options_chain

    monkeypatch.setattr(src.data_fetcher, "fetch_market_data",
                        lambda *args, **kwargs: pytest.fail("market provider called"))
    monkeypatch.setattr(src.options_chain, "fetch_chain",
                        lambda *args, **kwargs: pytest.fail("chain provider called"))
    plan = build_intraday_plan(_cfg(["orb"]), now=NOW,
                               candles=_candles(), chain=_chain())
    assert plan["option_candidates"]
