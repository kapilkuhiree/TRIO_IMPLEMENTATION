"""Offline runtime-entry tests for two-leg long-volatility structures."""

from datetime import datetime, timedelta, timezone

import pytest
import requests

from src.broker.base import BrokerOrder, Position
from src.options_intraday_runtime import OptionsIntradayRuntime
from src.risk_manager import reset_risk_state


IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 10, 14, 10, 0, tzinfo=IST)
QUOTE = "2026-10-14T04:25:00+00:00"
EXPIRY = "30-Oct-2026"


@pytest.fixture(autouse=True)
def _offline_scope(monkeypatch):
    monkeypatch.setenv("TRIO_TRADING_MODE", "options_intraday")
    monkeypatch.setenv("TRIO_BROKER_PROVIDER", "megabull")
    monkeypatch.setenv("TRIO_REMOTE_REQUIRED", "1")
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.delenv("TRIO_LIVE", raising=False)
    monkeypatch.setattr(
        requests.sessions.Session,
        "request",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("network is forbidden in structure runtime tests")),
    )
    reset_risk_state()
    yield
    reset_risk_state()


def _contract(kind):
    return {
        "instrumentToken": "12345" if kind == "CE" else "12346",
        "tradingSymbol": f"NIFTY26O3025000{kind}",
        "exchange": "NSE",
        "instrumentType": "OPTIDX",
        "lotSize": 65,
        "expiry": EXPIRY,
        "strike": 25000.0,
        "instrumentName": "NIFTY",
    }


def _chain():
    row = {
        "strike": 25000.0,
        "expiry": EXPIRY,
        "CE": {"bidPrice": 49.0, "askPrice": 50.0, "lastPrice": 50.0,
               "openInterest": 5000, "totalTradedVolume": 2000,
               "quote_timestamp": QUOTE},
        "PE": {"bidPrice": 49.0, "askPrice": 50.0, "lastPrice": 50.0,
               "openInterest": 5000, "totalTradedVolume": 2000,
               "quote_timestamp": QUOTE},
    }
    return {
        "symbol": "NIFTY",
        "underlying": 25000.0,
        "exchange": "NSE",
        "expiries": [EXPIRY],
        "by_expiry": {EXPIRY: [row]},
        "quote_timestamp": QUOTE,
    }


def _leg(kind):
    return {
        "strike": 25000.0,
        "expiry": EXPIRY,
        "kind": kind,
        "side": "BUY",
        "premium": 50.0,
        "premium_src": "live",
        "bid": 49.0,
        "ask": 50.0,
        "oi": 5000,
        "volume": 2000,
        "trading_symbol": f"NIFTY26O3025000{kind}",
    }


def _candidate():
    return {
        "asset": "options",
        "signal_timestamp": "2026-10-14T04:20:00+00:00",
        "signal_underlying": "NIFTY",
        "option_underlying": "NIFTY",
        "symbol": "NIFTY",
        "underlying": "NIFTY",
        "action": "BUY",
        "direction": "any",
        "strategy": "long_straddle",
        "setup": "long_straddle",
        "option_diagnostic": {
            "expiry_status": {"listed_by_exchange": True},
            "demand_supply": {"confirmed": True},
        },
        "option_legs": {
            "strategy": "long-straddle",
            "structure": "LONGSTRADDLE",
            "underlying": "NIFTY",
            "exchange": "NSE",
            "expiry": EXPIRY,
            "lot_size": 65,
            "net_debit": 100.0,
            "maxLoss": 6500.0,
            "legs": [_leg("CE"), _leg("PE")],
        },
    }


def _plan():
    return {
        "date": "2026-10-14",
        "trading_mode": "options_intraday",
        "retrieved_timestamp": QUOTE,
        "diagnostics": {"retrieved_at": QUOTE},
        "option_chain": _chain(),
        "option_candidates": [_candidate()],
    }


def _cfg(tmp_path, **overrides):
    mode = {
        "state_path": str(tmp_path / "private" / "options.state.json"),
        "PAPER_TRADING": True,
        "risk_per_trade_pct": 0.50,
        "max_daily_loss_pct": 1.50,
        "max_open_positions": 2,
        "max_premium_exposure_pct": 5.00,
        "max_premium_per_position": 18000,
        "allow_averaging_down": False,
        "max_portfolio_risk_pct": 1.0,
        "orb_confirm_closes": 1,
        "earliest_entry_time_by_strategy": None,
        "chain_provider": None,
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

    def get_balance(self):
        return {"available": 500000.0, "total": 500000.0}


class _StructureBroker:
    provider_name = "megabull_options"

    def __init__(self):
        self.positions = {}
        self.open_calls = []

    def get_balance(self):
        return {"available": 500000.0, "total": 500000.0}

    def instrument_for(self, symbol):
        if symbol.endswith("CE"):
            return _contract("CE")
        if symbol.endswith("PE"):
            return _contract("PE")
        return None

    def open_spread(self, candidate, lots=1, reason=""):
        self.open_calls.append((candidate, lots, reason))
        sid = "OPT_NIFTY26O3025000CE_NIFTY26O3025000PE"
        self.positions[sid] = Position(
            symbol=sid, side="LONG", quantity=lots,
            avg_price=100.0, current_price=100.0)
        return BrokerOrder(order_id="entry-structure", symbol=sid, side="BUY",
                           quantity=lots, price=100.0, status="FILLED",
                           broker="megabull_options")

    def get_positions(self):
        return list(self.positions.values())


class _Trader:
    def __init__(self, options, tmp_path):
        self.opt_broker = options
        self.broker = _EquityMirror()
        self.events = []
        self.options_intraday_state_path = tmp_path / "private" / "state.json"

    def _log_event(self, event, payload):
        self.events.append((event, payload))


def _open_structure(tmp_path):
    broker = _StructureBroker()
    runtime = OptionsIntradayRuntime(_Trader(broker, tmp_path), _cfg(tmp_path))
    assert len(runtime.execute_plan(_plan(), broker, now=NOW)) == 1
    return broker, runtime


def test_structure_entry_uses_combined_debit_and_both_remote_symbols(tmp_path):
    broker, runtime = _open_structure(tmp_path)

    assert len(broker.open_calls) == 1
    payload, lots, _ = broker.open_calls[0]
    assert lots == 1
    assert payload["strategy"] == "long-straddle"
    assert [leg["kind"] for leg in payload["legs"]] == ["CE", "PE"]
    assert [leg["trading_symbol"] for leg in payload["legs"]] == [
        "NIFTY26O3025000CE", "NIFTY26O3025000PE"]
    assert all(leg.get("validated_contract") for leg in payload["legs"])

    position = next(iter(runtime._state["positions"].values()))
    assert position["kind"] == "CE+PE"
    assert position["strategy"] == "long_straddle"
    assert position["entry_premium"] == pytest.approx(100.0)
    assert position["ask_premium"] == pytest.approx(100.0)
    # 100 debit x 65 units + Rs20 brokerage each way.
    assert position["premium_exposure"] == pytest.approx(6540.0)
    assert position["remote_symbols"] == [
        "NIFTY26O3025000CE", "NIFTY26O3025000PE"]
    assert len(position["legs"]) == 2
    key = next(iter(runtime._state["intents"]))
    assert key.startswith("STRUCT&")
    assert "25000.0|CE" in key and "25000.0|PE" in key


def test_structure_mark_sums_both_leg_bids(tmp_path, monkeypatch):
    broker, runtime = _open_structure(tmp_path)
    monkeypatch.setattr(runtime, "_fresh_chain",
                        lambda broker_, now, underlying: _chain())

    assert runtime.tick(now=NOW + timedelta(minutes=1)) == []

    position = next(iter(runtime._state["positions"].values()))
    # 49 CE bid + 49 PE bid = 98 umbrella premium.
    assert position["last_bid"] == pytest.approx(98.0)
    assert position["mark_status"] == "fresh"
