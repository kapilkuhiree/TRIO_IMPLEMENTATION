"""Focused tests for the shared MegaBull equity/options account boundary."""

from datetime import datetime, timedelta, timezone

import pytest

from src.broker.base import Position
from src.broker.megabull import (
    ASSET_EQUITY,
    ASSET_OPTIONS,
    ASSET_UNKNOWN,
    MegaBullBroker,
)
from src.execution_lifecycle import reconcile_broker
from src.options_intraday_runtime import OptionsIntradayPolicyError, OptionsIntradayRuntime, OptionsIntradayUnresolved
from src.risk_manager import get_risk_state, reset_risk_state


IST = timezone(timedelta(hours=5, minutes=30))


def _cache(tmp_path):
    path = tmp_path / "instruments.csv"
    path.write_text(
        "instrumentToken,tradingSymbol,exchange,instrumentType,lotSize,expiry,strike,instrumentName\n"
        "1,RELIANCE,NSE,EQ,1,,,RELIANCE\n"
        "2,NIFTY26O3025000CE,NSE,OPTIDX,65,30-Oct-2026,25000,NIFTY\n",
        encoding="utf-8",
    )
    return path


def _sparse_cache(tmp_path):
    path = tmp_path / "sparse-instruments.csv"
    path.write_text(
        "tradingSymbol,instrumentName,instrumentToken\n"
        "RELIANCE,RELIANCE,738561\n",
        encoding="utf-8",
    )
    return path


def test_megabull_partition_is_cache_only_and_conservative(tmp_path):
    broker = MegaBullBroker(api_key="test", instrument_cache=_cache(tmp_path))
    positions = {
        "RELIANCE.NS": Position("RELIANCE.NS", "LONG", 2, 100.0, 100.0),
        "NIFTY26O3025000CE": Position(
            "NIFTY26O3025000CE", "LONG", 65, 50.0, 50.0),
        "MYSTERY": Position("MYSTERY", "LONG", 1, 10.0, 10.0),
    }

    buckets = broker.partition_positions(positions)

    assert set(buckets[ASSET_EQUITY]) == {"RELIANCE.NS"}
    assert set(buckets[ASSET_OPTIONS]) == {"NIFTY26O3025000CE"}
    assert set(buckets[ASSET_UNKNOWN]) == {"MYSTERY"}


def test_options_runtime_ignores_sparse_cache_equity_holding(tmp_path, monkeypatch):
    """An existing cash position must not block Monday's options session."""
    monkeypatch.setenv("TRIO_TRADING_MODE", "combined")
    monkeypatch.setenv("TRIO_BROKER_PROVIDER", "megabull")
    monkeypatch.setenv("TRIO_REMOTE_REQUIRED", "1")
    reset_risk_state()

    equity = MegaBullBroker(api_key="test", instrument_cache=_sparse_cache(tmp_path))
    equity.positions = {
        "RELIANCE.NS": Position("RELIANCE.NS", "LONG", 2, 100.0, 100.0),
    }
    equity.refresh_positions = lambda: True
    options = _OptionsMirror()
    trader = _Trader(equity, options, tmp_path / "runtime.state.json")
    runtime = OptionsIntradayRuntime(trader, _runtime_cfg(tmp_path),
                                     trading_mode="combined")

    assert runtime._reconcile(options) == []
    assert not runtime._state.get("broker_blocked")
    assert not get_risk_state().halt_active


def test_lifecycle_rebuild_excludes_equity_broker_options_and_merges_strict_risk(
        tmp_path):
    reset_risk_state()
    broker = MegaBullBroker(api_key="test", instrument_cache=_cache(tmp_path))
    broker.positions = {
        "RELIANCE.NS": Position("RELIANCE.NS", "LONG", 2, 100.0, 100.0),
        "NIFTY26O3025000CE": Position(
            "NIFTY26O3025000CE", "LONG", 65, 50.0, 50.0),
    }
    broker.refresh_positions = lambda: True

    class Runtime:
        def risk_positions(self):
            return {
                "OPT_SPREAD": {
                    "asset": "options", "size": 1, "entry": 50.0,
                    "risk_amount": 3250.0,
                }
            }

    class Trader:
        pass

    trader = Trader()
    trader.broker = broker
    trader._options_runtime = Runtime()

    # Remote MegaBull confidence requires two successful refreshes.
    reconcile_broker(trader)
    reconcile_broker(trader)

    risk = get_risk_state()
    assert set(risk.positions) == {"RELIANCE.NS", "OPT_SPREAD"}
    assert risk.positions["OPT_SPREAD"]["asset"] == ASSET_OPTIONS
    assert "NIFTY26O3025000CE" not in risk.positions


def test_strict_option_close_is_rebuilt_once(tmp_path, monkeypatch):
    import src.risk_manager as risk_manager

    monkeypatch.setattr(risk_manager, "load_config", lambda: {
        "risk_management": {"capital": 500000, "max_open_positions": 5},
        "logging": {"trade_log": str(tmp_path / "missing-trades.jsonl")},
    })
    reset_risk_state()
    broker = MegaBullBroker(api_key="test", instrument_cache=_cache(tmp_path))
    broker.refresh_positions = lambda: True
    closed_at = datetime.now(timezone.utc).isoformat()

    class Runtime:
        def risk_positions(self):
            return {}

        def strict_options_closed_trades(self):
            return [{"position_id": "OPT_SPREAD", "symbol": "OPT_SPREAD",
                     "close_id": "options:OPT_SPREAD|" + closed_at,
                     "pnl": 12.0, "closed_at": closed_at,
                     "asset": "options"}]

    class Trader:
        pass

    trader = Trader()
    trader.broker = broker
    trader._options_runtime = Runtime()
    reconcile_broker(trader)
    first = get_risk_state().daily_pnl
    reconcile_broker(trader)
    second = get_risk_state().daily_pnl

    assert first == 12.0
    assert second == first


def _runtime_cfg(tmp_path):
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
        "options_intraday": {
            "state_path": str(tmp_path / "options.state.json"),
        },
    }


class _OptionsMirror:
    provider_name = "megabull_options"

    def __init__(self):
        self.positions = {}


class _EquityMirror:
    provider_name = "megabull"

    def __init__(self, cache, positions):
        self.positions = positions
        self._broker = MegaBullBroker(api_key="test", instrument_cache=cache)

    def classify_position(self, symbol, position):
        return self._broker.classify_position(symbol, position)


class _Trader:
    def __init__(self, equity, options, state_path):
        self.broker = equity
        self.opt_broker = options
        self.options_intraday_state_path = state_path


def test_combined_runtime_ignores_known_equity_but_blocks_unknown_option(
        tmp_path, monkeypatch):
    monkeypatch.setenv("TRIO_TRADING_MODE", "combined")
    monkeypatch.setenv("TRIO_BROKER_PROVIDER", "megabull")
    monkeypatch.setenv("TRIO_REMOTE_REQUIRED", "1")
    reset_risk_state()

    equity = _EquityMirror(
        _cache(tmp_path),
        {"RELIANCE.NS": Position("RELIANCE.NS", "LONG", 2, 100.0, 100.0)},
    )
    options = _OptionsMirror()
    trader = _Trader(equity, options, tmp_path / "runtime.state.json")
    runtime = OptionsIntradayRuntime(trader, _runtime_cfg(tmp_path),
                                     trading_mode="combined")

    assert runtime._reconcile(options) == []
    assert runtime.risk_positions() == {}

    options.positions["UNKNOWN_OPTION"] = Position(
        "UNKNOWN_OPTION", "LONG", 1, 10.0, 10.0)
    with pytest.raises(OptionsIntradayUnresolved, match="unknown remote holding"):
        runtime._reconcile(options)


def test_combined_runtime_still_requires_strict_nested_option_plan(tmp_path,
                                                                    monkeypatch):
    monkeypatch.setenv("TRIO_TRADING_MODE", "combined")
    monkeypatch.setenv("TRIO_BROKER_PROVIDER", "megabull")
    monkeypatch.setenv("TRIO_REMOTE_REQUIRED", "1")
    options = _OptionsMirror()
    trader = _Trader(_EquityMirror(_cache(tmp_path), {}), options,
                     tmp_path / "runtime.state.json")
    runtime = OptionsIntradayRuntime(trader, _runtime_cfg(tmp_path),
                                     trading_mode="combined")

    now = datetime.now(IST)
    with pytest.raises(OptionsIntradayPolicyError, match="plan trading_mode"):
        runtime._validate_plan_window(
            {"date": now.date().isoformat(), "trading_mode": "combined"}, now)
