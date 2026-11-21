"""Offline lifecycle tests for the options-only runtime boundary."""

from datetime import datetime, timezone, timedelta
import json

import pytest
import requests

from src.broker.base import BrokerOrder, Position
from src.options_intraday_runtime import (
    OptionsIntradayPolicyError,
    OptionsIntradayRuntime,
    OptionsIntradayUnresolved,
)
from src.risk_manager import get_risk_state, reset_risk_state


IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 10, 14, 10, 0, tzinfo=IST)
QUOTE = "2026-10-14T04:25:00+00:00"


@pytest.fixture(autouse=True)
def _offline_options_scope(monkeypatch):
    """This module must never make an HTTP request, even accidentally."""
    monkeypatch.setenv("TRIO_TRADING_MODE", "options_intraday")
    monkeypatch.setenv("TRIO_BROKER_PROVIDER", "megabull")
    monkeypatch.setenv("TRIO_REMOTE_REQUIRED", "1")
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.delenv("TRIO_LIVE", raising=False)
    monkeypatch.delenv("TRIO_ALLOW_LOCAL_OPTION_SHADOW", raising=False)
    monkeypatch.setattr(
        requests.sessions.Session,
        "request",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("network is forbidden in options runtime tests")),
    )
    reset_risk_state()
    yield
    reset_risk_state()


def _chain(bid=45.0, ask=50.0, quote_timestamp=QUOTE):
    row = {
        "strike": 25000.0,
        "expiry": "30-Oct-2026",
        "CE": {"bidPrice": bid, "askPrice": ask, "lastPrice": ask,
               "openInterest": 5000, "totalTradedVolume": 2000},
        "PE": {"bidPrice": 45.0, "askPrice": 50.0, "lastPrice": 50.0,
               "openInterest": 5000, "totalTradedVolume": 2000},
    }
    return {
        "symbol": "NIFTY",
        "underlying": 25000.0,
        "exchange": "NSE",
        "expiries": ["30-Oct-2026"],
        "by_expiry": {"30-Oct-2026": [row]},
        "quote_timestamp": quote_timestamp,
    }


def _contract():
    return {
        "instrumentToken": "12345",
        "tradingSymbol": "NIFTY26O3025000CE",
        "exchange": "NSE",
        "instrumentType": "OPTIDX",
        "lotSize": 65,
        "expiry": "30-Oct-2026",
        "strike": 25000.0,
        "instrumentName": "NIFTY",
    }


def _candidate(chain=None, ask=50.0, bid=45.0, strategy="orb"):
    return {
        "asset": "options",
        "signal_timestamp": "2026-10-14T04:20:00+00:00",
        "signal_underlying": "NIFTY",
        "option_underlying": "NIFTY",
        "action": "BUY",
        "direction": "bullish",
        "strategy": strategy,
        "setup": "breakout",
        "metadata": {"setup": "breakout", "strategy": strategy},
        "option_diagnostic": {"expiry_status": {"listed_by_exchange": False}},
        "option_legs": {
            "strategy": "long-call",
            "underlying": "NIFTY",
            "exchange": "NSE",
            "expiry": "30-Oct-2026",
            "lot_size": 65,
            "net_debit": (bid + ask) / 2.0,
            "maxLoss": ask * 65,
            "legs": [{
                "strike": 25000.0,
                "expiry": "30-Oct-2026",
                "exchange": "NSE",
                "kind": "CE",
                "side": "BUY",
                "premium": (bid + ask) / 2.0,
                "premium_src": "live",
                "bid": bid,
                "ask": ask,
                "oi": 5000,
                "volume": 2000,
            }],
        },
    }


def _plan(candidate=None, chain=None, retrieved=QUOTE):
    chain = chain or _chain()
    return {
        "date": "2026-10-14",
        "trading_mode": "options_intraday",
        "retrieved_timestamp": retrieved,
        "diagnostics": {"retrieved_at": retrieved},
        "option_chain": chain,
        "quotes_listing_artifact": {"source": "exchange-chain"},
        "option_candidates": [candidate or _candidate()],
    }


def _cfg(tmp_path, **overrides):
    mode = {
        "state_path": str(tmp_path / "private" / "options.state.json"),
        "PAPER_TRADING": True,
        "risk_per_trade_pct": 0.50,
        "max_daily_loss_pct": 1.50,
        "max_open_positions": 2,
        "max_premium_exposure_pct": 5.00,
        "allow_averaging_down": False,
        "max_portfolio_risk_pct": 1.0,
        # Routing/lifecycle fixtures supply one post-range close.  The shipped
        # overlay requires two consecutive closes and is covered separately.
        "orb_confirm_closes": 1,
        # These routing/lifecycle tests intentionally act at 09:35.  The live
        # overlay's per-strategy ORB entry floor (10:00) is a separate policy
        # covered by its own test, so opt out here.
        "earliest_entry_time_by_strategy": None,
        "chain_provider": overrides.pop("chain_provider", None),
    }
    mode.update(overrides)
    return {
        "general": {"mode": "paper"},
        "execution": {"remote_paper_required": True},
        "broker": {"provider": "megabull"},
        "risk_management": {"capital": 500000, "max_open_positions": 5},
        "options": {"underlying": "NIFTY", "exchange": "NSE",
                     "lot_size": 65, "strike_step": 50, "min_oi": 1000,
                     "max_spread_pct": 20, "brokerage_per_order": 20},
        "market": {"timezone": "Asia/Kolkata", "open": "09:15",
                    "entry_start": "09:20", "entry_cutoff": "14:30",
                    "hard_flat": "15:10", "close": "15:30", "holidays": []},
        "options_intraday": mode,
    }


class _EquityMirror:
    provider_name = "megabull"

    def __init__(self):
        self.positions = {}
        self.place_calls = []

    def get_balance(self):
        return {"available": 500000.0, "total": 500000.0}


class _OptionsDouble:
    provider_name = "megabull_options"

    def __init__(self, *, pending=False, accepted_close=False):
        self.positions = {}
        self.open_calls = []
        self.close_calls = []
        self.pending = pending
        self.accepted_close = accepted_close

    def get_balance(self):
        return {"available": 500000.0, "total": 500000.0}

    def instrument_for(self, symbol):
        if symbol == "NIFTY26O3025000CE":
            return _contract()
        return None

    def open_spread(self, candidate, lots=1, reason=""):
        self.open_calls.append((candidate, lots, reason))
        if self.pending:
            return BrokerOrder(order_id="pending-1", symbol="OPT_PENDING",
                               status="PENDING", broker="megabull_options")
        sid = "OPT_long-call_30-Oct-2026_25000x0"
        self.positions[sid] = Position(symbol=sid, side="LONG", quantity=lots,
                                       avg_price=50.0, current_price=50.0)
        return BrokerOrder(order_id="entry-1", symbol=sid, side="BUY",
                           quantity=lots, price=50.0, status="FILLED",
                           broker="megabull_options")

    def close_spread(self, sid, bid, reason=""):
        self.close_calls.append((sid, bid, reason))
        if self.accepted_close:
            return {"status": "ACCEPTED", "symbol": sid}
        self.positions.pop(sid, None)
        return {"symbol": sid, "status": "FILLED", "pnl": -100.0,
                "closed_at": "2026-10-14T05:00:00+00:00"}

    def get_positions(self):
        return list(self.positions.values())

    def get_order_status(self, order_id):
        return BrokerOrder(order_id=order_id, status="PENDING",
                           broker="megabull_options")


class _Trader:
    def __init__(self, options, tmp_path):
        self.opt_broker = options
        self.broker = _EquityMirror()
        self.events = []
        self.options_intraday_state_path = tmp_path / "private" / "state.json"

    def _log_event(self, event, payload):
        self.events.append((event, payload))


def test_effective_paper_risk_values_are_explicit_and_strict(tmp_path):
    from src.options_intraday import options_intraday_config

    effective = options_intraday_config(_cfg(tmp_path))

    assert effective["risk_per_trade_pct"] == 0.50
    assert effective["max_daily_loss_pct"] == 1.50
    assert effective["max_open_positions"] == 2
    assert effective["max_premium_exposure_pct"] == 5.00
    assert effective["allow_averaging_down"] is False
    assert effective["PAPER_TRADING"] is True


def test_per_trade_cap_uses_initial_twenty_percent_stop_and_brokerage(tmp_path):
    options = _OptionsDouble()
    runtime = OptionsIntradayRuntime(_Trader(options, tmp_path), _cfg(tmp_path))

    assert runtime.execute_plan(_plan(), options, now=NOW)

    # Rs50 ask x 65 units: Rs650 premium loss per lot at -20%, plus Rs40
    # round-trip brokerage.  Three lots fit Rs2,500 (0.50% of Rs500,000);
    # four lots do not.
    assert options.open_calls[0][1] == 3
    position = next(iter(runtime._state["positions"].values()))
    assert position["risk_amount"] == 1990.0
    assert position["premium_exposure"] == 9790.0


def test_premium_exposure_includes_open_and_pending_ask_debits(tmp_path):
    options = _OptionsDouble()
    runtime = OptionsIntradayRuntime(_Trader(options, tmp_path), _cfg(tmp_path))
    runtime.mode_cfg["allow_averaging_down"] = True
    runtime.mode_cfg["max_correlated_positions"] = 3
    # Keep the approved two-slot policy in normal execution; this direct
    # preflight isolates the exposure rule after two already-reserved slots.
    runtime._max_open_positions = lambda: 3
    runtime._state["mark_cycle_ms"] = cycle = int(NOW.timestamp() * 1000)
    runtime._state["positions"]["existing"] = {
        "option_underlying": "NIFTY", "lots": 1, "lot_size": 65,
        "entry_premium": 50.0, "last_bid": 50.0,
        "last_bid_at_ms": cycle, "mark_status": "fresh",
        "ask_premium": 50.0, "premium_exposure": 11000.0,
    }
    runtime._state["intents"]["pending"] = {
        "status": "pending", "option_underlying": "NIFTY",
        "premium_exposure": 11000.0,
    }
    option = _candidate()["option_legs"]
    leg = option["legs"][0]

    assert runtime._reserved_premium_exposure() == 22000.0
    lots, _, reason = runtime._risk_preflight(
        _candidate(), option, leg, options, now=NOW)
    assert lots == 0
    assert "premium-exposure" in reason


def test_max_two_open_positions_includes_reserved_state(tmp_path):
    options = _OptionsDouble()
    runtime = OptionsIntradayRuntime(_Trader(options, tmp_path), _cfg(tmp_path))
    for index in range(2):
        runtime._state["positions"][f"existing-{index}"] = {
            "option_underlying": f"OTHER{index}", "premium_exposure": 100.0,
        }
    option = _candidate()["option_legs"]
    lots, _, reason = runtime._risk_preflight(
        _candidate(), option, option["legs"][0], options, now=NOW)

    assert lots == 0
    assert reason == "options max-open-position limit reached"


def test_two_independent_strategies_may_hold_nifty(tmp_path):
    options = _OptionsDouble()
    runtime = OptionsIntradayRuntime(
        _Trader(options, tmp_path),
        _cfg(tmp_path, max_open_positions=2, max_correlated_positions=2,
             max_premium_per_position=18000),
    )
    # First strategy opens a NIFTY CE
    runtime.execute_plan(_plan(), options, now=NOW)
    # Refresh mark for existing positions
    cycle = int(NOW.timestamp() * 1000)
    runtime._state["mark_cycle_ms"] = cycle
    for pos in runtime._state["positions"].values():
        pos["last_bid_at_ms"] = cycle
        pos["mark_status"] = "fresh"
    
    assert runtime._active_position_count() == 1
    pos1 = next(iter(runtime._state["positions"].values()))
    assert pos1["strategy"] in ("orb", "long-call")
    assert pos1["option_underlying"] == "NIFTY"
    # Second strategy (different strategy) may also open a NIFTY CE
    candidate2 = _candidate()
    candidate2["strategy"] = "mean_reversion"
    candidate2["metadata"] = {"setup": "mean_reversion"}
    lots2, _, reason2 = runtime._risk_preflight(
        candidate2, candidate2["option_legs"], candidate2["option_legs"]["legs"][0],
        options, now=NOW)
    assert lots2 > 0, f"Failed: {reason2}"
    assert reason2 == ""
    # Same strategy must still be blocked
    candidate3 = _candidate()
    candidate3["strategy"] = pos1["strategy"]
    lots3, _, reason3 = runtime._risk_preflight(
        candidate3, candidate3["option_legs"], candidate3["option_legs"]["legs"][0],
        options, now=NOW)
    assert lots3 == 0
    assert "same-strategy" in reason3.lower() or "averaging down" in reason3.lower()


def test_averaging_down_is_rejected_for_open_or_pending_underlying(tmp_path):
    options = _OptionsDouble()
    runtime = OptionsIntradayRuntime(_Trader(options, tmp_path), _cfg(tmp_path))
    runtime._state["mark_cycle_ms"] = cycle = int(NOW.timestamp() * 1000)
    runtime._state["positions"]["existing"] = {
        "option_underlying": "NIFTY", "entry_premium": 100.0,
        "last_bid": 100.0, "last_bid_at_ms": cycle, "mark_status": "fresh",
        "lots": 1, "lot_size": 65, "premium_exposure": 100.0,
    }
    option = _candidate()["option_legs"]
    lots, _, reason = runtime._risk_preflight(
        _candidate(), option, option["legs"][0], options, now=NOW)

    assert lots == 0
    assert "same-strategy" in reason or "averaging down" in reason


def test_daily_loss_boundary_is_fail_closed_at_realized_plus_unrealized_limit(
        tmp_path):
    options = _OptionsDouble()
    runtime = OptionsIntradayRuntime(_Trader(options, tmp_path), _cfg(tmp_path))
    runtime._state["mark_cycle_ms"] = int(NOW.timestamp() * 1000)
    runtime._state["positions"]["existing"] = {
        "option_underlying": "NIFTY", "entry_premium": 50.0,
        "last_bid": 45.0, "last_bid_at_ms": runtime._state["mark_cycle_ms"],
        "mark_status": "fresh", "lots": 1, "lot_size": 65,
        "ask_premium": 50.0, "premium_exposure": 3290.0,
    }
    # Realized -Rs7,175 plus unrealized -Rs325 equals -Rs7,500, exactly
    # -1.50% of Rs500,000.
    get_risk_state().daily_pnl = -7175.0
    option = _candidate()["option_legs"]
    lots, _, reason = runtime._risk_preflight(
        _candidate(), option, option["legs"][0], options, now=NOW)

    assert lots == 0
    assert reason == "options daily-loss budget exhausted"


@pytest.mark.parametrize("paper_value", [None, False])
def test_missing_or_false_paper_trading_is_rejected_before_broker_use(
        tmp_path, paper_value):
    options = _OptionsDouble()
    with pytest.raises(OptionsIntradayPolicyError, match="PAPER_TRADING=true"):
        OptionsIntradayRuntime(
            _Trader(options, tmp_path),
            _cfg(tmp_path, PAPER_TRADING=paper_value),
        )
    assert options.open_calls == []


def test_missing_paper_trading_key_fails_effective_policy_validation():
    from src.options_intraday import validate_options_intraday_risk_config

    mode = {
        "risk_per_trade_pct": 0.50,
        "max_daily_loss_pct": 1.50,
        "max_open_positions": 2,
        "max_premium_exposure_pct": 5.00,
        "allow_averaging_down": False,
        "stop_premium_fraction": 0.20,
    }

    valid, reason = validate_options_intraday_risk_config(mode)

    assert not valid
    assert "PAPER_TRADING" in reason


def test_remote_entry_is_one_long_leg_and_deduplicated(tmp_path):
    options = _OptionsDouble()
    trader = _Trader(options, tmp_path)
    runtime = OptionsIntradayRuntime(trader, _cfg(tmp_path))

    placed = runtime.execute_plan(_plan(), options, now=NOW)
    assert len(placed) == 1
    payload, lots, reason = options.open_calls[0]
    assert lots == 3 and reason == "options-intraday-entry"
    assert payload["execution_class"] == "remote_paper"
    assert payload["order_action"] == "BUY"
    assert payload["signal_action"] == "BUY"
    assert payload["validated_contract"] == _contract()
    assert payload["trading_symbol"] == "NIFTY26O3025000CE"
    assert payload["quote_timestamp"] == QUOTE
    assert len(payload["legs"]) == 1 and payload["legs"][0]["side"] == "BUY"
    assert not trader.broker.place_calls, "runtime must not use an equity order path"

    assert runtime.execute_plan(_plan(), options, now=NOW) == []
    assert len(options.open_calls) == 1
    state = json.loads((tmp_path / "private" / "options.state.json").read_text())
    assert state["daily_entries"] == 1
    assert len(state["positions"]) == 1


def test_policy_rechecks_chain_quote_and_ignores_false_archived_diagnostic(tmp_path):
    options = _OptionsDouble()
    runtime = OptionsIntradayRuntime(_Trader(options, tmp_path), _cfg(tmp_path))
    changed = _chain(bid=44.0, ask=50.0)
    with pytest.raises(OptionsIntradayPolicyError, match="exchange chain"):
        runtime.execute_plan(_plan(chain=changed), options, now=NOW)
    assert options.open_calls == []


def test_previous_session_plan_is_rejected_before_any_option_order(tmp_path):
    options = _OptionsDouble()
    runtime = OptionsIntradayRuntime(_Trader(options, tmp_path), _cfg(tmp_path))
    stale = _plan()
    stale["date"] = "2026-10-13"
    with pytest.raises(OptionsIntradayPolicyError, match="today's valid session"):
        runtime.execute_plan(stale, options, now=NOW)
    assert options.open_calls == []


def test_previous_session_signal_is_rejected_before_any_option_order(tmp_path):
    options = _OptionsDouble()
    runtime = OptionsIntradayRuntime(_Trader(options, tmp_path), _cfg(tmp_path))
    stale = _plan()
    stale["option_candidates"][0]["signal_timestamp"] = (
        "2026-10-13T04:20:00+00:00")
    with pytest.raises(OptionsIntradayPolicyError, match="missing, future-dated, or stale"):
        runtime.execute_plan(stale, options, now=NOW)
    assert options.open_calls == []


def test_complete_lot_affordability_does_not_force_one_lot(tmp_path):
    options = _OptionsDouble()
    # The approved 5% exposure cap is intentionally exercised at a lower
    # fixture-specific cap: one 65-unit lot at an Rs80 ask plus charges is
    # already above the configured premium budget.
    runtime = OptionsIntradayRuntime(
        _Trader(options, tmp_path),
        _cfg(tmp_path, max_premium_exposure_pct=1.0),
    )
    expensive = _candidate(ask=80.0, bid=75.0)
    assert runtime.execute_plan(
        _plan(candidate=expensive, chain=_chain(bid=75.0, ask=80.0)),
        options, now=NOW) == []
    assert options.open_calls == []
    assert get_risk_state().open_positions == 0


def test_stale_mark_blocks_new_entries_instead_of_using_old_bid(tmp_path):
    options = _OptionsDouble()
    runtime = OptionsIntradayRuntime(_Trader(options, tmp_path), _cfg(tmp_path))
    runtime.mode_cfg["allow_averaging_down"] = True
    runtime.mode_cfg["max_correlated_positions"] = 2
    runtime._max_open_positions = lambda: 2
    runtime._state["positions"]["existing"] = {
        "option_underlying": "BANKNIFTY", "entry_premium": 100.0,
        "last_bid": 100.0, "last_bid_at_ms": 1, "lots": 1, "lot_size": 30,
        "ask_premium": 100.0, "premium_exposure": 3040.0,
    }
    option = _candidate()["option_legs"]

    lots, _, reason = runtime._risk_preflight(
        _candidate(), option, option["legs"][0], options, now=NOW)

    assert lots == 0
    assert "mark is unresolved" in reason


def test_pending_intent_counts_toward_correlation_limit(tmp_path):
    options = _OptionsDouble()
    runtime = OptionsIntradayRuntime(_Trader(options, tmp_path), _cfg(tmp_path))
    runtime.mode_cfg["allow_averaging_down"] = True
    runtime.mode_cfg["correlated_groups"] = [["NIFTY", "BANKNIFTY"]]
    runtime.mode_cfg["max_correlated_positions"] = 1
    runtime._state["intents"]["pending-bank"] = {
        "status": "pending", "option_underlying": "BANKNIFTY",
        "premium_exposure": 1000.0, "risk_amount": 100.0,
    }
    option = _candidate()["option_legs"]

    lots, _, reason = runtime._risk_preflight(
        _candidate(), option, option["legs"][0], options, now=NOW)

    assert lots == 0
    assert reason == "correlated-options limit reached"


def test_pending_entry_is_retained_and_never_resubmitted(tmp_path):
    options = _OptionsDouble(pending=True)
    runtime = OptionsIntradayRuntime(_Trader(options, tmp_path), _cfg(tmp_path))
    with pytest.raises(OptionsIntradayUnresolved, match="pending"):
        runtime.execute_plan(_plan(), options, now=NOW)
    with pytest.raises(OptionsIntradayUnresolved, match="unresolved"):
        runtime.execute_plan(_plan(), options, now=NOW)
    assert len(options.open_calls) == 1


def test_tick_closes_at_fresh_bid_stop_first_and_updates_production_risk(tmp_path):
    exit_chain = _chain(bid=39.0, ask=44.0, quote_timestamp="2026-10-14T04:34:00+00:00")
    options = _OptionsDouble()
    trader = _Trader(options, tmp_path)
    runtime = OptionsIntradayRuntime(
        trader, _cfg(tmp_path, chain_provider=lambda *args, **kwargs: exit_chain))
    runtime.execute_plan(_plan(), options, now=NOW)
    opened_at = runtime._state["positions"][
        "OPT_long-call_30-Oct-2026_25000x0"]["opened_at"]
    closed = runtime.tick(now=datetime(2026, 10, 14, 10, 5, tzinfo=IST))
    assert len(closed) == 1
    assert closed[0]["reason"] == "stop-loss"
    # The live audit reads the closed leg from the state file; keep the entry
    # timestamp with it so a restart cannot lose it.
    assert closed[0]["entry_ts"] == opened_at
    assert options.close_calls == [("OPT_long-call_30-Oct-2026_25000x0", 39.0, "stop-loss")]
    assert get_risk_state().open_positions == 0
    assert get_risk_state().daily_pnl == -100.0
    assert closed[0]["historical_options_pnl"] == "NOT_READY_FOR_OPTIONS_PNL"
    assert trader.broker.place_calls == []


def test_profit_ladder_locks_at_ten_percent_then_stops_on_bid_reversal(tmp_path):
    quotes = iter([
        _chain(bid=55.0, ask=60.0, quote_timestamp="2026-10-14T04:35:00+00:00"),
        _chain(bid=54.0, ask=59.0, quote_timestamp="2026-10-14T04:40:00+00:00"),
    ])
    options = _OptionsDouble()
    runtime = OptionsIntradayRuntime(
        _Trader(options, tmp_path),
        _cfg(
            tmp_path,
            chain_provider=lambda *args, **kwargs: next(quotes),
            profit_lock_rungs=[],
        ),
    )
    runtime.execute_plan(_plan(), options, now=NOW)

    assert runtime.tick(now=datetime(2026, 10, 14, 10, 5, tzinfo=IST)) == []
    position = next(iter(runtime._state["positions"].values()))
    assert position["profit_lock_stage"] == 1
    assert position["stop_premium"] == 55.0
    assert position["target_premium"] == 60.0
    assert options.close_calls == []

    closed = runtime.tick(now=datetime(2026, 10, 14, 10, 10, tzinfo=IST))
    # The +10% rung lifted the stop above entry; closing there is a locked
    # profit, not a stop-loss.
    assert closed[0]["reason"] == "trailing-stop"
    assert options.close_calls == [
        ("OPT_long-call_30-Oct-2026_25000x0", 54.0, "trailing-stop")
    ]


def test_profit_ladder_skips_rungs_and_locks_highest_reached_level(tmp_path):
    quotes = iter([
        _chain(bid=62.0, ask=67.0, quote_timestamp="2026-10-14T04:35:00+00:00"),
        _chain(bid=59.0, ask=64.0, quote_timestamp="2026-10-14T04:40:00+00:00"),
    ])
    options = _OptionsDouble()
    runtime = OptionsIntradayRuntime(
        _Trader(options, tmp_path),
        _cfg(
            tmp_path,
            chain_provider=lambda *args, **kwargs: next(quotes),
            profit_lock_rungs=[],
        ),
    )
    runtime.execute_plan(_plan(), options, now=NOW)

    assert runtime.tick(now=datetime(2026, 10, 14, 10, 5, tzinfo=IST)) == []
    position = next(iter(runtime._state["positions"].values()))
    assert position["profit_lock_stage"] == 2
    assert position["stop_premium"] == 60.0
    assert position["target_premium"] == 65.0

    closed = runtime.tick(now=datetime(2026, 10, 14, 10, 10, tzinfo=IST))
    assert closed[0]["reason"] == "trailing-stop"
    assert options.close_calls[-1][1:] == (59.0, "trailing-stop")


def test_profit_ladder_walks_custom_rungs_ten_to_twenty_five_percent(tmp_path):
    quotes = iter([
        _chain(bid=55.0, ask=60.0, quote_timestamp="2026-10-14T04:35:00+00:00"),
        _chain(bid=57.5, ask=62.5, quote_timestamp="2026-10-14T04:40:00+00:00"),
        _chain(bid=60.0, ask=65.0, quote_timestamp="2026-10-14T04:45:00+00:00"),
        _chain(bid=62.5, ask=67.5, quote_timestamp="2026-10-14T04:50:00+00:00"),
        _chain(bid=62.4, ask=67.4, quote_timestamp="2026-10-14T04:55:00+00:00"),
    ])
    options = _OptionsDouble()
    runtime = OptionsIntradayRuntime(
        _Trader(options, tmp_path),
        _cfg(
            tmp_path,
            chain_provider=lambda *args, **kwargs: next(quotes),
            profit_lock_rungs=[0.10, 0.15, 0.20, 0.25],
        ),
    )
    runtime.execute_plan(_plan(), options, now=NOW)
    position = next(iter(runtime._state["positions"].values()))
    assert position["profit_lock_rungs"] == [0.10, 0.15, 0.20, 0.25]

    steps = [
        (datetime(2026, 10, 14, 10, 5, tzinfo=IST), 1, 55.0, 57.5),
        (datetime(2026, 10, 14, 10, 10, tzinfo=IST), 2, 57.5, 60.0),
        (datetime(2026, 10, 14, 10, 15, tzinfo=IST), 3, 60.0, 62.5),
        (datetime(2026, 10, 14, 10, 20, tzinfo=IST), 4, 62.5, 65.0),
    ]
    for now, stage, stop, target in steps:
        assert runtime.tick(now=now) == []
        assert position["profit_lock_stage"] == stage
        assert position["stop_premium"] == stop
        assert position["target_premium"] == target

    # Bid falls back below the +25% rung: the locked stop exits the position
    # in profit rather than the -20% initial stop.
    closed = runtime.tick(now=datetime(2026, 10, 14, 10, 25, tzinfo=IST))
    assert closed[0]["reason"] == "trailing-stop"
    assert options.close_calls[-1][1:] == (62.4, "trailing-stop")


def test_custom_rungs_leave_the_target_exit_ahead_of_the_ladder(tmp_path):
    """Live sessions target +25%; the target still exits before the rung arms."""
    quotes = iter([
        _chain(bid=62.5, ask=67.5, quote_timestamp="2026-10-14T04:35:00+00:00"),
    ])
    options = _OptionsDouble()
    runtime = OptionsIntradayRuntime(
        _Trader(options, tmp_path),
        _cfg(
            tmp_path,
            chain_provider=lambda *args, **kwargs: next(quotes),
            profit_lock_rungs=[0.10, 0.15, 0.20, 0.25],
            target_premium_fraction=0.25,
        ),
    )
    runtime.execute_plan(_plan(), options, now=NOW)
    closed = runtime.tick(now=datetime(2026, 10, 14, 10, 5, tzinfo=IST))
    assert closed[0]["reason"] == "target"
    assert options.close_calls[-1][1:] == (62.5, "target")


def test_afternoon_target_drops_rungs_at_or_above_it(tmp_path):
    """A reduced afternoon target must not silently disable the ladder.

    On 2026-11-16 both afternoon entries ran a 10% target against rungs that
    started at 10%: the target branch fires first, so ``profit_lock_stage``
    stayed 0 and the ratchet never armed.  Rungs at or above the target are
    dropped; the rungs below it must still raise the stop.
    """
    options = _OptionsDouble()
    runtime = OptionsIntradayRuntime(_Trader(options, tmp_path), _cfg(tmp_path))
    position = {
        "entry_premium": 100.0,
        "stop_premium": 80.0,
        "initial_stop_premium": 80.0,
        "target_premium": 110.0,
        "final_target_premium": 110.0,
        "profit_lock_step_fraction": 0.10,
        "profit_lock_rungs": [0.05, 0.08, 0.10, 0.15],
        "profit_lock_stage": 0,
    }

    assert runtime._update_profit_ladder(position, 104.9) is None
    assert position["profit_lock_stage"] == 0

    assert runtime._update_profit_ladder(position, 105.0) is None
    assert position["profit_lock_stage"] == 1
    assert position["stop_premium"] == 105.0
    assert position["target_premium"] == 108.0

    # The target still wins at 110: the ladder is a ratchet, not a second exit.
    assert runtime._update_profit_ladder(position, 110.0) == "target"


def test_legacy_rung_at_target_still_gets_a_reachable_ladder(tmp_path):
    """Positions stored before entry-time filtering keep a usable ladder."""
    options = _OptionsDouble()
    runtime = OptionsIntradayRuntime(_Trader(options, tmp_path), _cfg(tmp_path))
    position = {
        "entry_premium": 100.0,
        "stop_premium": 80.0,
        "initial_stop_premium": 80.0,
        "target_premium": 110.0,
        "final_target_premium": 110.0,
        "profit_lock_step_fraction": 0.10,
        "profit_lock_rungs": [0.10, 0.15, 0.20, 0.25],
        "profit_lock_stage": 0,
    }

    assert runtime._update_profit_ladder(position, 105.0) is None
    assert position["profit_lock_stage"] == 1
    assert position["stop_premium"] == 105.0


def test_afternoon_entry_stores_only_reachable_rungs(tmp_path):
    """Entry-time filtering keeps the persisted rungs below the target."""
    options = _OptionsDouble()
    cfg = _cfg(
        tmp_path,
        afternoon_target_fraction=0.10,
        afternoon_target_from="13:30",
        profit_lock_rungs=[0.05, 0.08, 0.10, 0.15],
    )
    runtime = OptionsIntradayRuntime(_Trader(options, tmp_path), cfg)
    plan = _plan(retrieved="2026-10-14T08:25:00+00:00")
    plan["option_chain"] = _chain(quote_timestamp="2026-10-14T08:25:00+00:00")
    plan["option_candidates"][0]["signal_timestamp"] = "2026-10-14T08:20:00+00:00"

    runtime.execute_plan(
        plan, options, now=datetime(2026, 10, 14, 14, 0, tzinfo=IST))

    position = next(iter(runtime._state["positions"].values()))
    assert position["final_target_premium"] == 55.0
    assert position["profit_lock_rungs"] == [0.05, 0.08]


def test_profitable_trailing_exit_does_not_arm_loss_cooldown(tmp_path):
    quotes = iter([
        _chain(bid=55.0, ask=60.0, quote_timestamp="2026-10-14T04:35:00+00:00"),
        _chain(bid=54.0, ask=59.0, quote_timestamp="2026-10-14T04:40:00+00:00"),
    ])
    options = _OptionsDouble()
    runtime = OptionsIntradayRuntime(
        _Trader(options, tmp_path),
        _cfg(
            tmp_path,
            chain_provider=lambda *args, **kwargs: next(quotes),
            underlying_stop_loss_cooldown_seconds=900,
        ),
    )
    runtime.execute_plan(_plan(), options, now=NOW)
    runtime.tick(now=datetime(2026, 10, 14, 10, 5, tzinfo=IST))

    closed = runtime.tick(now=datetime(2026, 10, 14, 10, 10, tzinfo=IST))
    assert closed[0]["reason"] == "trailing-stop"
    # A locked-in profit must not arm the loss cooldown or the loss streak.
    assert runtime._underlying_stop_times == {}
    assert get_risk_state().consecutive_losses == 0

    option = _candidate()["option_legs"]
    lots, _, reason = runtime._risk_preflight(
        _candidate(), option, option["legs"][0], options,
        now=datetime(2026, 10, 14, 10, 10, 31, tzinfo=IST))
    assert lots > 0
    assert reason != "options underlying stop-loss cooldown is active"


def test_loss_only_reentry_cooldown_blocks_after_a_loss(tmp_path):
    options = _OptionsDouble()
    runtime = OptionsIntradayRuntime(
        _Trader(options, tmp_path),
        _cfg(tmp_path, reentry_cooldown_after_exit_seconds=300,
             reentry_cooldown_loss_only=True,
             reentry_cooldown_by_strategy={"orb": 300},
             max_consecutive_losses=0),
    )
    runtime._underlying_exit_times[("orb", "NIFTY")] = datetime(
        2026, 10, 14, 10, 0, tzinfo=IST)
    runtime._underlying_last_exit_loss[("orb", "NIFTY")] = True

    candidate = _candidate(strategy="orb")
    option = candidate["option_legs"]
    lots, _, reason = runtime._risk_preflight(
        candidate, option, option["legs"][0], options,
        now=datetime(2026, 10, 14, 10, 2, tzinfo=IST))
    assert lots == 0
    assert reason == "options underlying reentry cooldown is active"

    lots, _, reason = runtime._risk_preflight(
        candidate, option, option["legs"][0], options,
        now=datetime(2026, 10, 14, 10, 6, tzinfo=IST))
    assert reason != "options underlying reentry cooldown is active"


def test_loss_only_reentry_cooldown_ignores_a_winner(tmp_path):
    options = _OptionsDouble()
    runtime = OptionsIntradayRuntime(
        _Trader(options, tmp_path),
        _cfg(tmp_path, reentry_cooldown_after_exit_seconds=300,
             reentry_cooldown_loss_only=True,
             reentry_cooldown_by_strategy={"orb": 300},
             max_consecutive_losses=0),
    )
    runtime._underlying_exit_times[("orb", "NIFTY")] = datetime(
        2026, 10, 14, 10, 0, tzinfo=IST)
    runtime._underlying_last_exit_loss[("orb", "NIFTY")] = False

    candidate = _candidate(strategy="orb")
    option = candidate["option_legs"]
    lots, _, reason = runtime._risk_preflight(
        candidate, option, option["legs"][0], options,
        now=datetime(2026, 10, 14, 10, 1, tzinfo=IST))
    assert reason != "options underlying reentry cooldown is active"


def test_earliest_entry_time_is_per_strategy(tmp_path):
    options = _OptionsDouble()
    runtime = OptionsIntradayRuntime(
        _Trader(options, tmp_path),
        _cfg(tmp_path,
             earliest_entry_time_by_strategy={"orb": "10:00",
                                              "orb_retest": "09:45"},
             max_consecutive_losses=0),
    )
    early = datetime(2026, 10, 14, 9, 50, tzinfo=IST)

    orb = _candidate(strategy="orb")
    orb_option = orb["option_legs"]
    lots, _, reason = runtime._risk_preflight(
        orb, orb_option, orb_option["legs"][0], options, now=early)
    assert lots == 0
    assert reason == "options earliest-entry time not reached"

    retest = _candidate(strategy="orb_retest")
    retest_option = retest["option_legs"]
    _, _, reason = runtime._risk_preflight(
        retest, retest_option, retest_option["legs"][0], options, now=early)
    assert reason != "options earliest-entry time not reached"


def test_skip_event_records_the_blocking_strategy(tmp_path, monkeypatch):
    """The forward report needs to attribute a gate skip to its sleeve."""
    options = _OptionsDouble()
    runtime = OptionsIntradayRuntime(_Trader(options, tmp_path), _cfg(tmp_path))
    captured = []

    def fake_record(event, record):
        captured.append((event, dict(record)))

    monkeypatch.setattr(runtime, "_record_event", fake_record)
    monkeypatch.setattr(
        runtime, "_risk_preflight",
        lambda *args, **kwargs: (0, 0.0, "options earliest-entry time not reached"))

    runtime.execute_plan(_plan(), options, now=NOW)

    skips = [record for event, record in captured if event == "skip"]
    assert skips, "a blocked candidate must emit a skip event"
    assert skips[0]["strategy"] == "orb"
    assert skips[0]["setup"] == "breakout"
    assert skips[0]["option_underlying"] == "NIFTY"
    assert skips[0]["reason"] == "options earliest-entry time not reached"


def _stop_position(strategy="orb"):
    return {
        "position_id": "OPT_stop",
        "status": "open",
        "asset": "options",
        "execution_class": "remote_paper",
        "strategy": strategy,
        "setup": strategy,
        "signal_action": "BUY",
        "direction": "bullish",
        "signal_underlying": "NIFTY",
        "option_underlying": "NIFTY",
        "trading_symbol": "NIFTY26O3025000CE",
        "expiry": "30-Oct-2026",
        "strike": 25000.0,
        "kind": "CE",
        "lots": 1,
        "lot_size": 65,
        "entry_premium": 50.0,
        "initial_stop_premium": 40.0,
        "stop_premium": 40.0,
    }


def test_underlying_stop_cooldown_is_scoped_per_strategy(tmp_path):
    options = _OptionsDouble()
    runtime = OptionsIntradayRuntime(
        _Trader(options, tmp_path),
        _cfg(tmp_path, underlying_stop_loss_cooldown_seconds=900),
    )
    now = datetime(2026, 10, 14, 10, 0, tzinfo=IST)

    record = runtime._close_one(
        options, "OPT_stop", _stop_position("orb"), 39.0, "stop-loss", now=now)

    assert record["strategy"] == "orb"
    assert runtime._underlying_stop_loss_cooldown_elapsed(
        "NIFTY", now + timedelta(seconds=31), "orb") is False
    # A stop on ORB must not block a different strategy on the same index.
    assert runtime._underlying_stop_loss_cooldown_elapsed(
        "NIFTY", now + timedelta(seconds=31), "mean_reversion") is True
    assert runtime._underlying_stop_loss_cooldown_elapsed(
        "NIFTY", now + timedelta(seconds=901), "orb") is True


def test_order_and_close_records_carry_strategy_and_setup(tmp_path):
    options = _OptionsDouble()
    trader = _Trader(options, tmp_path)
    runtime = OptionsIntradayRuntime(
        trader,
        _cfg(tmp_path, chain_provider=lambda *args, **kwargs:
             _chain(bid=39.0, ask=44.0,
                    quote_timestamp="2026-10-14T04:34:00+00:00")),
    )
    candidate = _candidate()
    candidate["strategy"] = "mean_reversion"
    candidate["setup"] = "mean_reversion"
    candidate["metadata"] = {"setup": "mean_reversion"}

    runtime.execute_plan(_plan(candidate), options, now=NOW)

    position = next(iter(runtime._state["positions"].values()))
    assert position["strategy"] == "mean_reversion"
    assert position["setup"] == "mean_reversion"
    orders = [payload for event, payload in trader.events if event == "order"]
    assert orders and orders[0]["strategy"] == "mean_reversion"

    closed = runtime.tick(now=datetime(2026, 10, 14, 10, 5, tzinfo=IST))
    assert closed[0]["strategy"] == "mean_reversion"
    assert closed[0]["setup"] == "mean_reversion"


def test_hard_flat_does_not_mark_accepted_close_as_flat(tmp_path):
    options = _OptionsDouble(accepted_close=True)
    trader = _Trader(options, tmp_path)
    runtime = OptionsIntradayRuntime(
        trader, _cfg(tmp_path, chain_provider=lambda *args, **kwargs:
                     _chain(bid=48.0, ask=53.0,
                            quote_timestamp="2026-10-14T09:35:00+00:00")))
    runtime.execute_plan(_plan(), options, now=NOW)
    eod = datetime(2026, 10, 14, 15, 10, tzinfo=IST)
    with pytest.raises(OptionsIntradayUnresolved, match="OPT_long-call"):
        runtime.tick(now=eod)
    assert len(options.close_calls) == 1
    assert runtime._state["positions"]
    with pytest.raises(OptionsIntradayUnresolved):
        runtime.finalize(now=eod)
    assert len(options.close_calls) == 2


def test_explicit_live_switch_is_rejected_before_broker_use(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIO_LIVE", "1")
    with pytest.raises(OptionsIntradayPolicyError, match="rejects live"):
        OptionsIntradayRuntime(_Trader(_OptionsDouble(), tmp_path), _cfg(tmp_path))


def test_cli_trading_mode_is_honored_without_environment_override(tmp_path, monkeypatch):
    monkeypatch.delenv("TRIO_TRADING_MODE", raising=False)
    trader = _Trader(_OptionsDouble(), tmp_path)
    trader.trading_mode = "options_intraday"

    runtime = OptionsIntradayRuntime(trader, _cfg(tmp_path))

    assert runtime.execution_class == "remote_paper"


def test_unknown_remote_holding_blocks_entries_without_closing_it(tmp_path):
    options = _OptionsDouble()
    options.positions["UNRELATED_EQUITY"] = Position(
        symbol="UNRELATED_EQUITY", side="LONG", quantity=1,
        avg_price=100.0, current_price=100.0)
    runtime = OptionsIntradayRuntime(_Trader(options, tmp_path), _cfg(tmp_path))
    with pytest.raises(OptionsIntradayUnresolved, match="unknown remote holding"):
        runtime.execute_plan(_plan(), options, now=NOW)
    assert options.close_calls == []
    assert options.open_calls == []
    # An unreadable/unexpected remote book is also a mirror-health failure.
    assert runtime._state["mirror_failures"] == 1


def test_mirror_failure_blocks_entries_and_recovers_before_flatten(tmp_path):
    options = _OptionsDouble()
    options.refresh_positions = lambda: True
    runtime = OptionsIntradayRuntime(
        _Trader(options, tmp_path),
        _cfg(tmp_path, chain_provider=lambda *args, **kwargs: _chain(
            bid=45.0, ask=50.0, quote_timestamp=QUOTE)))
    assert runtime.execute_plan(_plan(), options, now=NOW)
    position_id = next(iter(options.positions))
    entries = len(options.open_calls)

    options.refresh_positions = lambda: False
    with pytest.raises(OptionsIntradayUnresolved, match="remote mirror"):
        runtime.tick(now=NOW + timedelta(seconds=10))
    assert runtime._state["mirror_failures"] == 1
    assert runtime._state["mirror_unhealthy_since_ms"] > 0
    assert runtime._state["last_mirror_error"] == "remote mirror refresh returned unhealthy"
    assert runtime._state["positions"][position_id]["mark_unresolved"] is True
    assert options.close_calls == []

    # Entries are blocked while the persisted failure is unresolved.
    with pytest.raises(OptionsIntradayUnresolved):
        runtime.execute_plan(_plan(), options, now=NOW + timedelta(seconds=20))
    assert len(options.open_calls) == entries
    assert options.close_calls == []

    # Recovery before the 120s threshold clears the guard and prevents a
    # defensive flatten; the leg is managed normally again.
    options.refresh_positions = lambda: True
    assert runtime.tick(now=NOW + timedelta(seconds=60)) == []
    assert runtime._state["mirror_failures"] == 0
    assert runtime._state["mirror_unhealthy_since_ms"] == 0
    assert runtime._state["last_mirror_error"] == ""
    assert options.close_calls == []
    assert runtime._state["positions"][position_id]["mark_unresolved"] is False


def test_mirror_outage_flattens_once_after_threshold(tmp_path):
    options = _OptionsDouble()
    options.refresh_positions = lambda: True
    runtime = OptionsIntradayRuntime(_Trader(options, tmp_path), _cfg(tmp_path))
    assert runtime.execute_plan(_plan(), options, now=NOW)
    position_id = next(iter(options.positions))

    options.refresh_positions = lambda: False
    with pytest.raises(OptionsIntradayUnresolved):
        runtime.tick(now=NOW + timedelta(seconds=10))
    # 90s after the first failure is still inside the 120s window.
    with pytest.raises(OptionsIntradayUnresolved):
        runtime.tick(now=NOW + timedelta(seconds=90))
    assert options.close_calls == []

    closed = runtime.tick(now=NOW + timedelta(seconds=130))
    assert [record["reason"] for record in closed] == ["mirror-unhealthy"]
    assert position_id not in runtime._state["positions"]
    assert options.close_calls[-1][2] == "mirror-unhealthy"
    assert runtime._state["mirror_flatten_attempted_ms"] > 0

    # The defensive close is never repeated during the same outage: a blind
    # retry could oversell the leg.
    with pytest.raises(OptionsIntradayUnresolved):
        runtime.tick(now=NOW + timedelta(seconds=140))
    assert len(options.close_calls) == 1


def test_mirror_unhealthy_state_survives_restart(tmp_path):
    options = _OptionsDouble()
    options.refresh_positions = lambda: True
    runtime = OptionsIntradayRuntime(_Trader(options, tmp_path), _cfg(tmp_path))
    assert runtime.execute_plan(_plan(), options, now=NOW)

    options.refresh_positions = lambda: False
    with pytest.raises(OptionsIntradayUnresolved):
        runtime.tick(now=NOW + timedelta(seconds=10))

    restarted_options = _OptionsDouble()
    restarted_options.positions.update(options.positions)
    restarted_options.refresh_positions = lambda: False
    restarted = OptionsIntradayRuntime(
        _Trader(restarted_options, tmp_path), _cfg(tmp_path))
    assert restarted._mirror_unhealthy()
    assert restarted._state["mirror_failures"] >= 1
    assert restarted._state["last_mirror_error"] == "remote mirror refresh returned unhealthy"

    with pytest.raises(OptionsIntradayUnresolved):
        restarted.execute_plan(
            _plan(), restarted_options, now=NOW + timedelta(seconds=30))
    assert restarted_options.open_calls == []


def test_existing_megabull_options_adapter_routes_with_fake_transport(tmp_path):
    """Exercise the repository adapter while keeping its transport offline."""
    from src.broker.megabull_options import MegaBullOptionsBroker

    class FakeMegaBull:
        provider_name = "megabull"

        def __init__(self):
            self.positions = {}

        def token_for(self, symbol):
            return "12345"

        def instrument_for(self, symbol):
            return _contract()

        def get_balance(self):
            return {"available": 500000.0, "total": 500000.0}

        def refresh_positions(self):
            return True

        def place_order(self, symbol, side, quantity, order_type="MKT",
                        price=None, reason="order", **kwargs):
            self.positions[symbol] = Position(
                symbol=symbol, side="LONG", quantity=quantity,
                avg_price=price or 0.0, current_price=price or 0.0)
            return BrokerOrder(order_id="leg-1", symbol=symbol, side=side,
                               quantity=quantity, price=price or 0.0,
                               status="FILLED", broker="megabull")

        def close_position(self, symbol, price, reason="close", fraction=1.0):
            self.positions.pop(symbol, None)
            return {"symbol": symbol, "status": "FILLED"}

    mega = FakeMegaBull()
    options = MegaBullOptionsBroker(mega, lot_size=65)
    trader = _Trader(options, tmp_path)
    runtime = OptionsIntradayRuntime(
        trader, _cfg(tmp_path, chain_provider=lambda *args, **kwargs:
                     _chain(bid=30.0, ask=35.0,
                            quote_timestamp="2026-10-14T04:34:00+00:00")))
    placed = runtime.execute_plan(_plan(), options, now=NOW)
    assert placed and len(mega.positions) == 1
    closed = runtime.tick(now=datetime(2026, 10, 14, 10, 5, tzinfo=IST))
    assert closed and not mega.positions and not options.positions


def test_restart_reconciles_saved_umbrella_without_resubmitting(tmp_path):
    first_options = _OptionsDouble()
    first_trader = _Trader(first_options, tmp_path)
    cfg = _cfg(tmp_path)
    first = OptionsIntradayRuntime(first_trader, cfg)
    first.execute_plan(_plan(), first_options, now=NOW)

    second_options = _OptionsDouble()
    second_options.positions.update(first_options.positions)
    second = OptionsIntradayRuntime(_Trader(second_options, tmp_path), cfg)
    assert second.execute_plan(_plan(), second_options, now=NOW) == []
    assert second_options.open_calls == []


def test_restart_reads_back_only_runtime_umbrella_close_into_risk(tmp_path, monkeypatch):
    options = _OptionsDouble()
    trader = _Trader(options, tmp_path)
    runtime = OptionsIntradayRuntime(
        trader, _cfg(tmp_path, chain_provider=lambda *args, **kwargs:
                     _chain(bid=39.0, ask=44.0,
                            quote_timestamp="2026-10-14T04:34:00+00:00")))
    runtime.execute_plan(_plan(), options, now=NOW)
    runtime.tick(now=datetime(2026, 10, 14, 10, 5, tzinfo=IST))
    assert get_risk_state().daily_pnl == -100.0

    reset_risk_state()
    # Keep the simulated restart in the same IST session as the fixed plan.
    import src.options_intraday_runtime as runtime_module
    monkeypatch.setattr(runtime_module, "_now_ist", lambda value: NOW if value is None else value)
    restarted = OptionsIntradayRuntime(_Trader(_OptionsDouble(), tmp_path), _cfg(tmp_path))
    assert restarted.historical_options_pnl == "NOT_READY_FOR_OPTIONS_PNL"
    assert get_risk_state().daily_pnl == -100.0


def _live_inputs(symbols=("NIFTY", "RELIANCE")):
    """Real ORB/planner/selector fixtures with different exact exchange lots."""
    rows, data, chains = [], {}, {}
    for symbol in symbols:
        index = symbol == "NIFTY"
        strike, lot = (100, 65) if index else (100.5, 17)
        for kind in ("CE", "PE"):
            rows.append({"instrumentToken": f"{symbol}-{kind}",
                         "tradingSymbol": f"{symbol}26OCT{strike}{kind}",
                         "instrumentName": symbol, "exchange": "NSE",
                         "instrumentType": "OPTIDX" if index else "OPTSTK",
                         "lotSize": lot, "expiry": "16-Oct-2026", "strike": strike})
        data[symbol] = [
            {"timestamp": f"2026-10-14T{stamp}:00+05:30", "open": 100,
             "high": high, "low": 99, "close": close, "volume": 1000,
             "underlying_symbol": symbol, "exchange": "NSE",
             "instrument_type": "index" if index else "stock"}
            for stamp, high, close in [("09:15", 101, 100), ("09:20", 101, 100),
                                        ("09:25", 101, 100), ("09:30", 103, 102)]
        ]
        quote = {"bidPrice": 9.8, "askPrice": 10.0, "openInterest": 5000,
                 "totalTradedVolume": 1000}
        chains[symbol] = {"symbol": symbol, "underlying": 102, "exchange": "NSE",
                          "expiries": ["16-Oct-2026"],
                          "quote_timestamp": "2026-10-14T09:34:00+05:30",
                          "by_expiry": {"16-Oct-2026": [{"strike": strike,
                              "expiry": "16-Oct-2026", "CE": dict(quote), "PE": dict(quote)}]}}
    return rows, data, chains


class _MultiOptions(_OptionsDouble):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def option_instruments(self):
        return self.rows

    def instrument_for(self, symbol):
        return next((row for row in self.rows if row["tradingSymbol"] == symbol), None)

    def open_spread(self, candidate, lots=1, reason=""):
        self.open_calls.append((candidate, lots, reason))
        sid = "OPT_" + candidate["trading_symbol"]
        ask = candidate["legs"][0]["ask"]
        self.positions[sid] = Position(symbol=sid, side="LONG", quantity=lots,
                                       avg_price=ask, current_price=ask)
        return BrokerOrder(order_id=sid, symbol=sid, side="BUY", quantity=lots,
                           price=ask, status="FILLED", broker="megabull_options")


def _live_runtime(tmp_path, monkeypatch, symbols=("NIFTY", "RELIANCE")):
    from copy import deepcopy
    from src import options_live_scan as scanner
    rows, data, chains = _live_inputs(symbols)
    options = _MultiOptions(rows)
    cfg = _cfg(tmp_path, scan_underlyings=list(symbols), strategies=["orb"], max_lots=1,
               chain_provider=lambda symbol, **kwargs: deepcopy(chains[symbol]))
    cfg["demand_supply"] = {"enabled": False}
    monkeypatch.delenv("TRIO_DEMAND_SUPPLY", raising=False)
    monkeypatch.delenv("TRIO_RESEARCH_STRATEGIES", raising=False)
    monkeypatch.setattr(scanner, "fetch_multiple", lambda *args, **kwargs: deepcopy(data))
    trader = _Trader(options, tmp_path)
    return OptionsIntradayRuntime(trader, cfg), options, data, chains


def test_live_scan_executes_real_stock_and_index_orb_without_plan_io(tmp_path, monkeypatch):
    from src import plan
    def forbidden(*args, **kwargs):
        pytest.fail("saved plan I/O must not be called")
    monkeypatch.setattr(plan, "load_plan", forbidden)
    monkeypatch.setattr(plan, "save_plan", forbidden)
    runtime, options, _, _ = _live_runtime(tmp_path, monkeypatch)
    now = datetime(2026, 10, 14, 9, 35, tzinfo=IST)
    placed = runtime.scan_and_execute(now=now)
    assert len(placed) == 2
    assert {item["option_underlying"]: item["lot_size"] for item in placed} == {
        "NIFTY": 65, "RELIANCE": 17}
    assert all(payload["order_action"] == "BUY" and len(payload["legs"]) == 1
               and payload["legs"][0]["kind"] == "CE" and lots == 1
               for payload, lots, _ in options.open_calls)
    assert runtime.last_scan_diagnostics["requested"] == 2
    assert runtime.last_scan_diagnostics["submitted"] == 2
    assert runtime.scan_and_execute(now=now) == []
    assert len(options.open_calls) == 2
    assert runtime.trader.broker.place_calls == []


def test_live_scan_rechecks_signal_and_does_not_trade_disappeared_setup(tmp_path, monkeypatch):
    from copy import deepcopy
    from src import options_live_scan as scanner
    runtime, options, data, _ = _live_runtime(tmp_path, monkeypatch, ("RELIANCE",))
    calls = []
    def market_data(*args, **kwargs):
        batch = deepcopy(data)
        calls.append(args)
        if len(calls) > 1:
            batch["RELIANCE"][-1].update(high=101, close=100)
        return batch
    monkeypatch.setattr(scanner, "fetch_multiple", market_data)
    assert runtime.scan_and_execute(now=datetime(2026, 10, 14, 9, 35, tzinfo=IST)) == []
    assert len(calls) == 2
    assert options.open_calls == []
    assert runtime.last_scan_diagnostics["entry_rejections"][0]["reason"] == "setup_or_contract_no_longer_valid"


def test_live_scan_uses_refreshed_ask_not_discovery_quote(tmp_path, monkeypatch):
    from copy import deepcopy
    runtime, options, _, chains = _live_runtime(tmp_path, monkeypatch, ("RELIANCE",))
    calls = []
    def chain(symbol, **kwargs):
        assert kwargs["use_cache"] is False
        calls.append(symbol)
        result = deepcopy(chains[symbol])
        if len(calls) > 1:
            result["by_expiry"]["16-Oct-2026"][0]["CE"].update(bidPrice=11.8, askPrice=12)
        return result
    runtime._chain_provider = chain
    assert runtime.scan_and_execute(now=datetime(2026, 10, 14, 9, 35, tzinfo=IST))
    assert options.open_calls[0][0]["legs"][0]["premium"] == 12
    assert options.open_calls[0][0]["lot_size"] == 17


def test_wider_scan_keeps_daily_entry_limit(tmp_path, monkeypatch):
    runtime, options, _, _ = _live_runtime(tmp_path, monkeypatch)
    runtime.mode_cfg["max_daily_entries"] = 1
    assert len(runtime.scan_and_execute(now=datetime(2026, 10, 14, 9, 35, tzinfo=IST))) == 1
    assert runtime._state["daily_entries"] == 1
    assert len(options.open_calls) == 1


def test_each_open_underlying_uses_its_own_fresh_exit_chain(tmp_path, monkeypatch):
    runtime, options, _, chains = _live_runtime(tmp_path, monkeypatch)
    assert len(runtime.scan_and_execute(now=datetime(2026, 10, 14, 9, 35, tzinfo=IST))) == 2
    for symbol, bid in (("NIFTY", 7.5), ("RELIANCE", 13)):
        chains[symbol]["quote_timestamp"] = "2026-10-14T09:36:00+05:30"
        chains[symbol]["by_expiry"]["16-Oct-2026"][0]["CE"].update(bidPrice=bid, askPrice=bid + .2)
    closed = runtime.tick(now=datetime(2026, 10, 14, 9, 37, tzinfo=IST))
    assert {item["option_underlying"]: (item["exit_bid"], item["reason"]) for item in closed} == {
        "NIFTY": (7.5, "stop-loss"), "RELIANCE": (13, "target")}
    position = {"option_underlying": "RELIANCE", "expiry": "16-Oct-2026",
                "strike": 100, "kind": "CE"}
    assert runtime._chain_quote(chains["NIFTY"], position,
                                datetime(2026, 10, 14, 9, 37, tzinfo=IST)) is None
    assert not options.positions


def test_slow_read_only_discovery_keeps_management_on_main_thread(tmp_path, monkeypatch):
    from threading import Event, get_ident
    from src import options_live_scan as scanner
    runtime, _, _, _ = _live_runtime(tmp_path, monkeypatch)
    release, managed = Event(), []
    caller = get_ident()
    def scan(*args, **kwargs):
        assert get_ident() != caller
        assert release.wait(5), "management did not run during slow discovery"
        return {"snapshots": []}
    def tick(**kwargs):
        managed.append(get_ident())
        release.set()
    monkeypatch.setattr(scanner, "build_live_options_scan", scan)
    monkeypatch.setattr(runtime, "tick", tick)
    runtime.management_interval_seconds = 1
    assert runtime._read_only_scan(runtime.cfg, lambda: NOW, []) == {"snapshots": []}
    assert managed == [caller]


def test_real_adapter_routes_exact_stock_symbol_units_and_marks_own_lot(tmp_path, monkeypatch):
    from src.broker.megabull import classify_instrument
    from src.broker.megabull_options import MegaBullOptionsBroker
    runtime, listing, _, _ = _live_runtime(tmp_path, monkeypatch)

    class Mega(_EquityMirror):
        def option_instruments(self):
            return listing.rows

        def instrument_for(self, symbol):
            return listing.instrument_for(symbol)

        def classify_position(self, symbol, value):
            return classify_instrument(symbol, self.instrument_for(symbol))

        def token_for(self, symbol):
            return self.instrument_for(symbol)["instrumentToken"]

        def refresh_positions(self):
            return True

        def place_order(self, symbol, side, quantity, **kwargs):
            self.place_calls.append((symbol, side, quantity))
            self.positions[symbol] = Position(symbol=symbol, side="LONG", quantity=quantity,
                                               avg_price=10, current_price=10)
            return BrokerOrder(order_id=symbol, symbol=symbol, side=side, quantity=quantity,
                               price=10, status="FILLED", broker="megabull")

        def close_position(self, symbol, price, reason, **kwargs):
            self.positions.pop(symbol, None)
            return {"symbol": symbol, "status": "FILLED"}

    mega = Mega()
    broker = MegaBullOptionsBroker(mega, lot_size=65)
    runtime._broker = runtime.trader.opt_broker = broker
    placed = runtime.scan_and_execute(now=datetime(2026, 10, 14, 9, 35, tzinfo=IST))
    assert len(placed) == 2
    assert set(mega.place_calls) == {("NIFTY26OCT100CE", "BUY", 65),
                                    ("RELIANCE26OCT100.5CE", "BUY", 17)}
    stock_id = next(item["position_id"] for item in placed if item["option_underlying"] == "RELIANCE")
    broker.mark_spread(stock_id, 11)
    assert broker.positions[stock_id].pnl == 17
    record = broker.close_spread(stock_id, 11, "test-close")
    assert record["gross_pnl"] == 17
    assert stock_id not in broker.positions


def test_cutoff_and_unknown_holdings_never_start_discovery(tmp_path, monkeypatch):
    runtime, options, _, _ = _live_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime, "_read_only_scan", lambda *args: pytest.fail("discovery must not start"))
    assert runtime.scan_and_execute(now=datetime(2026, 10, 14, 14, 30, tzinfo=IST)) == []
    options.positions["UNKNOWN"] = Position(symbol="UNKNOWN", side="LONG", quantity=1)
    with pytest.raises(OptionsIntradayUnresolved, match="unknown remote holding"):
        runtime.scan_and_execute(now=datetime(2026, 10, 14, 9, 35, tzinfo=IST))
    assert options.open_calls == options.close_calls == []


def test_bearish_stock_signal_routes_buy_pe_not_a_short_option(tmp_path, monkeypatch):
    runtime, options, data, _ = _live_runtime(tmp_path, monkeypatch, ("RELIANCE",))
    data["RELIANCE"][-1].update(high=101, low=98, close=98.75)
    placed = runtime.scan_and_execute(now=datetime(2026, 10, 14, 9, 35, tzinfo=IST))
    assert len(placed) == 1
    payload, lots, _ = options.open_calls[0]
    assert payload["signal_action"] == "SELL"
    assert payload["order_action"] == "BUY"
    assert payload["trading_symbol"] == "RELIANCE26OCT100.5PE"
    assert payload["legs"][0]["side"] == "BUY" and payload["legs"][0]["kind"] == "PE"
    assert lots == 1


def test_stale_refresh_never_falls_back_to_discovery_snapshot(tmp_path, monkeypatch):
    from copy import deepcopy
    runtime, options, _, chains = _live_runtime(tmp_path, monkeypatch, ("RELIANCE",))
    calls = []
    def provider(symbol, **kwargs):
        result = deepcopy(chains[symbol])
        calls.append(symbol)
        if len(calls) > 1:
            result["quote_timestamp"] = "2026-10-14T09:00:00+05:30"
        return result
    runtime._chain_provider = provider
    assert runtime.scan_and_execute(now=datetime(2026, 10, 14, 9, 35, tzinfo=IST)) == []
    assert options.open_calls == []
    assert runtime.last_scan_diagnostics["selected"] == 1
    assert runtime.last_scan_diagnostics["submitted"] == 0


def test_stock_scan_preserves_bank_correlation_limit(tmp_path, monkeypatch):
    runtime, options, _, _ = _live_runtime(tmp_path, monkeypatch, ("HDFCBANK", "SBIN"))
    runtime.mode_cfg["max_correlated_positions"] = 1
    placed = runtime.scan_and_execute(now=datetime(2026, 10, 14, 9, 35, tzinfo=IST))
    assert len(placed) == 1
    assert len(options.open_calls) == 1
    assert placed[0]["option_underlying"] in {"HDFCBANK", "SBIN"}
    assert runtime._state["daily_entries"] == 1


def test_shadow_signals_are_logged_without_orders_intents_or_daily_entries(
        tmp_path, monkeypatch):
    from copy import deepcopy

    runtime, options, _, _ = _live_runtime(tmp_path, monkeypatch)
    found = {
        "snapshots": [],
        "diagnostics": {
            "requested": 2, "data_ok": 2, "no_signal": 2, "not_listed": 0,
            "contract_rejection": 0, "fetch_errors": 0, "selected": 0,
        },
        "shadow": [{
            "underlying": "NIFTY", "strategy": "mean_reversion",
            "direction": "bullish", "option_type": "CE",
            "expiry": "16-Oct-2026", "strike": 100.0, "kind": "CE",
            "bid": 9.8, "ask": 10.0, "vwap_would_block": True,
            "vwap_reason": "vwap_counter_trend(close=99.00<vwap=100.00)",
            "collides_with_tradable": False,
        }],
    }
    monkeypatch.setattr(
        runtime, "_read_only_scan", lambda *args, **kwargs: deepcopy(found))

    placed = runtime.scan_and_execute(now=datetime(2026, 10, 14, 9, 35, tzinfo=IST))

    assert placed == []
    assert options.open_calls == []
    assert runtime._state.get("daily_entries", 0) == 0
    assert not runtime._state.get("intents")
    shadow_events = [payload for event, payload in runtime.trader.events
                     if event == "shadow_signal"]
    assert len(shadow_events) == 1
    assert shadow_events[0]["strategy"] == "mean_reversion"
    assert shadow_events[0]["vwap_would_block"] is True
    assert runtime.last_scan_diagnostics["shadow_signals"] == 1
    scan_events = [payload for event, payload in runtime.trader.events
                   if event == "options_scan"]
    assert scan_events and scan_events[-1]["submitted"] == 0
    assert scan_events[-1]["shadow_signals"] == 1


class _FakeJudge:
    """Minimal stand-in for ShadowJudge; records what it was asked to do."""

    def __init__(self, records):
        self.records = list(records)
        self.submitted = []
        self.closed = False

    def submit(self, snapshot):
        self.submitted.append(snapshot)
        return True

    def poll(self):
        drained, self.records = list(self.records), []
        return drained

    def close(self, timeout=3.0):
        self.closed = True


def test_judge_snapshots_are_submitted_but_never_touch_the_order_path(tmp_path):
    options = _OptionsDouble()
    trader = _Trader(options, tmp_path)
    runtime = OptionsIntradayRuntime(trader, _cfg(tmp_path))
    runtime._llm_judge = _FakeJudge([])

    snapshot = {"underlying": "NIFTY", "strategy": "orb",
                "direction": "bearish", "bar_ts": "2026-11-11T13:50:00+05:30"}
    runtime._submit_judge([
        {"judge_snapshot": snapshot, "rank": 1},
        {"rank": 2},                       # no snapshot: shadow sleeve
        {"judge_snapshot": {}, "rank": 3},  # builder declined
        "not-a-dict",
    ])

    assert runtime._llm_judge.submitted == [snapshot]
    # Nothing about the judge may create state, intents or orders.
    assert runtime._state.get("intents") == {}
    assert runtime._state.get("positions") == {}
    assert options.open_calls == []


def test_completed_judge_verdicts_are_logged_as_llm_verdict_events(tmp_path):
    options = _OptionsDouble()
    trader = _Trader(options, tmp_path)
    runtime = OptionsIntradayRuntime(trader, _cfg(tmp_path))
    runtime._llm_judge = _FakeJudge([
        {"underlying": "NIFTY", "strategy": "orb", "status": "ok",
         "bar_ts": "2026-11-11T13:50:00+05:30",
         "verdict": {"bias": "short", "confidence": 0.8, "agrees": True}},
    ])

    runtime._drain_judge()

    logged = [payload for event, payload in trader.events if event == "llm_verdict"]
    assert len(logged) == 1
    assert logged[0]["verdict"]["bias"] == "short"
    # Drain is idempotent: a verdict is written exactly once.
    runtime._drain_judge()
    assert len([p for e, p in trader.events if e == "llm_verdict"]) == 1


def test_judge_failures_never_block_tick_or_finalize(tmp_path):
    class ExplodingJudge(_FakeJudge):
        def submit(self, snapshot):
            raise RuntimeError("judge exploded")

        def poll(self):
            raise RuntimeError("judge exploded")

        def close(self, timeout=3.0):
            raise RuntimeError("judge exploded")

    runtime = OptionsIntradayRuntime(_Trader(_OptionsDouble(), tmp_path), _cfg(tmp_path))
    runtime._llm_judge = ExplodingJudge([])

    runtime._submit_judge([{"judge_snapshot": {"underlying": "NIFTY"}}])
    runtime._drain_judge()
    runtime._drain_judge()  # no raise, no state change

_PAPER_ALL_SLEEVES = (
    "orb", "orb_retest", "mean_reversion",
    "opening_range_reversion", "smc_liquidity",
)

_PAPER_ALL_OVERRIDES = {
    "shadow_strategies": [],
    "max_open_positions": 5,
    "max_correlated_positions": 5,
    "max_premium_exposure_pct": 9.00,
    "max_consecutive_losses": 5,
    "vwap_filter_strategies": ["orb", "orb_retest"],
    "max_premium_per_position": 18000,
}


def _paper_all_cfg(tmp_path):
    return _cfg(tmp_path, **_PAPER_ALL_OVERRIDES)


def _open_position(strategy, cycle, *, strike=25000.0):
    return {
        "option_underlying": "NIFTY",
        "strategy": strategy,
        "entry_premium": 50.0,
        "last_bid": 50.0,
        "last_bid_at_ms": cycle,
        "mark_status": "fresh",
        "lots": 1,
        "lot_size": 65,
        "ask_premium": 50.0,
        "premium_exposure": 3250.0,
        "strike": strike,
    }


def test_paper_all_five_sleeves_each_get_one_slot(tmp_path):
    options = _OptionsDouble()
    runtime = OptionsIntradayRuntime(_Trader(options, tmp_path), _paper_all_cfg(tmp_path))
    cycle = int(NOW.timestamp() * 1000)
    runtime._state["mark_cycle_ms"] = cycle

    # Every sleeve passes its own preflight while the other four already hold
    # a NIFTY position: five independent slots, not two.
    for index, sleeve in enumerate(_PAPER_ALL_SLEEVES):
        candidate = _candidate(strategy=sleeve)
        option = candidate["option_legs"]
        lots, _, reason = runtime._risk_preflight(
            candidate, option, option["legs"][0], options, now=NOW)
        assert lots > 0, f"{sleeve} was blocked: {reason}"
        runtime._state["positions"][f"pos-{index}"] = _open_position(sleeve, cycle)

    assert runtime._active_position_count() == 5
    assert runtime._slots_used() == {sleeve: 1 for sleeve in _PAPER_ALL_SLEEVES}

    # With all five slots taken, the sixth entry is refused by the slot cap.
    candidate = _candidate(strategy="orb")
    option = candidate["option_legs"]
    lots, _, reason = runtime._risk_preflight(
        candidate, option, option["legs"][0], options, now=NOW)
    assert lots == 0
    assert reason == "options max-open-position limit reached"


def test_paper_all_same_sleeve_cannot_pyramid_but_other_sleeves_can(tmp_path):
    options = _OptionsDouble()
    runtime = OptionsIntradayRuntime(_Trader(options, tmp_path), _paper_all_cfg(tmp_path))
    cycle = int(NOW.timestamp() * 1000)
    runtime._state["mark_cycle_ms"] = cycle
    runtime._state["positions"]["orb-1"] = _open_position("orb", cycle)

    repeated = _candidate(strategy="orb")
    option = repeated["option_legs"]
    lots, _, reason = runtime._risk_preflight(
        repeated, option, option["legs"][0], options, now=NOW)
    assert lots == 0
    assert "same-strategy" in reason

    for sleeve in _PAPER_ALL_SLEEVES[1:]:
        candidate = _candidate(strategy=sleeve)
        option = candidate["option_legs"]
        lots, _, reason = runtime._risk_preflight(
            candidate, option, option["legs"][0], options, now=NOW)
        assert lots > 0, f"{sleeve} was blocked by ORB's position: {reason}"


def test_paper_all_contract_collision_keeps_the_slot_free(tmp_path):
    options = _OptionsDouble()
    trader = _Trader(options, tmp_path)
    runtime = OptionsIntradayRuntime(trader, _paper_all_cfg(tmp_path))
    first = _candidate(strategy="orb")
    second = _candidate(strategy="mean_reversion")
    plan = _plan(first)
    plan["option_candidates"] = [first, second]

    placed = runtime.execute_plan(plan, options, now=NOW)

    # The second sleeve picks the exact contract the first one holds: it is
    # skipped, and the position count stays at one so another contract can
    # still use a slot.
    assert [item["strategy"] for item in placed] == ["orb"]
    assert runtime._active_position_count() == 1
    reasons = [payload["reason"] for event, payload in trader.events
               if event == "skip"]
    assert reasons == ["contract_held_by_other_strategy"]


def test_slots_used_counts_open_and_pending_by_strategy(tmp_path):
    runtime = OptionsIntradayRuntime(
        _Trader(_OptionsDouble(), tmp_path), _paper_all_cfg(tmp_path))
    cycle = int(NOW.timestamp() * 1000)
    runtime._state["positions"]["p1"] = _open_position("orb", cycle)
    runtime._state["positions"]["p2"] = _open_position("mean_reversion", cycle)
    runtime._state["intents"]["i1"] = {
        "status": "pending", "strategy": "smc_liquidity"}
    runtime._state["intents"]["i2"] = {
        "status": "closed", "strategy": "opening_range_reversion"}

    assert runtime._slots_used() == {"orb": 1, "mean_reversion": 1,
                                     "smc_liquidity": 1}


def test_paper_all_yaml_policy_unchanged_by_default(tmp_path):
    """Off by default: the shipped two-slot policy is still what the YAML says."""
    from src.options_intraday import options_intraday_config

    effective = options_intraday_config(_cfg(tmp_path))

    assert effective["max_open_positions"] == 2
    assert "vwap_filter_strategies" not in effective or not effective["vwap_filter_strategies"]
