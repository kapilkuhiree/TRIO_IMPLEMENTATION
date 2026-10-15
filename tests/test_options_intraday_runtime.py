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


def _candidate(chain=None, ask=50.0, bid=45.0):
    return {
        "asset": "options",
        "signal_timestamp": "2026-10-14T04:20:00+00:00",
        "signal_underlying": "NIFTY",
        "option_underlying": "NIFTY",
        "action": "BUY",
        "direction": "bullish",
        "metadata": {"setup": "breakout"},
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
        "max_portfolio_risk_pct": 1.0,
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
                     "max_spread_pct": 20},
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


def test_remote_entry_is_one_long_leg_and_deduplicated(tmp_path):
    options = _OptionsDouble()
    trader = _Trader(options, tmp_path)
    runtime = OptionsIntradayRuntime(trader, _cfg(tmp_path))

    placed = runtime.execute_plan(_plan(), options, now=NOW)
    assert len(placed) == 1
    payload, lots, reason = options.open_calls[0]
    assert lots == 1 and reason == "options-intraday-entry"
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


def test_complete_lot_affordability_does_not_force_one_lot(tmp_path):
    options = _OptionsDouble()
    runtime = OptionsIntradayRuntime(_Trader(options, tmp_path), _cfg(tmp_path))
    expensive = _candidate(ask=80.0, bid=75.0)
    assert runtime.execute_plan(
        _plan(candidate=expensive, chain=_chain(bid=75.0, ask=80.0)),
        options, now=NOW) == []
    assert options.open_calls == []
    assert get_risk_state().open_positions == 0


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
    closed = runtime.tick(now=datetime(2026, 10, 14, 10, 5, tzinfo=IST))
    assert len(closed) == 1
    assert closed[0]["reason"] == "stop-loss"
    assert options.close_calls == [("OPT_long-call_30-Oct-2026_25000x0", 39.0, "stop-loss")]
    assert get_risk_state().open_positions == 0
    assert get_risk_state().daily_pnl == -100.0
    assert closed[0]["historical_options_pnl"] == "NOT_READY_FOR_OPTIONS_PNL"
    assert trader.broker.place_calls == []


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
    # Keep the simulated restart in the same IST session as the fixed plan
    # timestamps above.  The real runtime uses the wall clock and correctly
    # rolls the private daily ledger on an actual date change.
    import src.options_intraday_runtime as runtime_module
    monkeypatch.setattr(runtime_module, "_now_ist", lambda value: NOW if value is None else value)
    restarted = OptionsIntradayRuntime(_Trader(_OptionsDouble(), tmp_path), _cfg(tmp_path))
    assert restarted.historical_options_pnl == "NOT_READY_FOR_OPTIONS_PNL"
    assert get_risk_state().daily_pnl == -100.0
