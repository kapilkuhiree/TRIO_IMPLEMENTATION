"""Focused offline regressions for the remote-required options margin gate."""

import json
import math
import os
from pathlib import Path
from unittest.mock import patch

from src.broker.base import BrokerOrder
from src.paper_trader import PaperTrader
from src.risk_manager import get_risk_state, reset_risk_state


class _EquityRemoteDouble:
    provider_name = "megabull"
    positions = {}
    closed_trades = []

    def __init__(self, balance):
        self.balance = balance

    def get_balance(self):
        return self.balance


class _OptionsRemoteDouble:
    provider_name = "megabull_options"

    def __init__(self):
        self.open_calls = 0
        self.positions = {}

    def open_spread(self, candidate, lots=1, reason="options-plan"):
        self.open_calls += 1
        return BrokerOrder(order_id="remote-option-1", status="FILLED",
                           symbol="OPT-test", quantity=lots,
                           broker="megabull_options")


def _plan():
    return {"date": "2026-10-14", "candidates": [{
        "symbol": "NIFTY", "action": "BUY", "risk_amount": 1000.0,
        "option_legs": {
            "strategy": "long-call", "underlying": "NIFTY",
            "spot": 25000.0, "expiry": "30-Oct-2026", "net_debit": 10.0,
            "maxLoss": 500.0, "maxGain": 1000.0, "breakeven": 25010.0,
            "rank": 90.0, "confidence": 80,
            "legs": [{"strike": 25000.0, "expiry": "30-Oct-2026",
                      "kind": "CE", "side": "BUY", "premium": 10.0}],
        },
    }]}


def _trader(tmp_path, balance):
    cfg = {
        "general": {"output_dir": str(tmp_path)},
        "logging": {"trade_log": str(tmp_path / "trade_log.jsonl")},
        "risk_management": {"capital": 500000},
        "options": {"lot_size": 50, "max_spreads_per_session": 4},
        "trading": {"allow_shorts": False},
        "broker": {"provider": "paper"},
    }
    # Construct locally, then exercise the remote-only execution gate with
    # explicit boundary doubles. No remote adapter or network is initialized.
    with patch("src.paper_trader.load_config", return_value=cfg), \
            patch("src.paper_trader.remote_paper_required", return_value=False):
        trader = PaperTrader(symbols=["NIFTY"], test_mode=False)
    trader.trade_log_path = tmp_path / "trade_log.jsonl"
    trader._live_log_path = trader.trade_log_path
    trader._options_test_config = cfg
    trader.broker = _EquityRemoteDouble(balance)
    trader.broker_name = "megabull"
    return trader


def _run_remote_options(tmp_path, balance):
    reset_risk_state()
    trader = _trader(tmp_path, balance)
    option_broker = _OptionsRemoteDouble()
    with patch.dict(os.environ, {"TRIO_TEST_MODE": "0"}), \
            patch("src.paper_trader.load_config",
                  return_value=trader._options_test_config), \
            patch("src.paper_trader.remote_paper_required", return_value=True), \
            patch.object(trader, "_session_open", return_value=True), \
            patch("src.execution_lifecycle.reconcile_broker",
                  return_value={"uncertain": False}):
        placed = trader.execute_option_plan(_plan(), opt_broker=option_broker)
    return trader, option_broker, placed


def test_remote_options_invalid_margin_never_submits_or_registers(tmp_path):
    for balance in ({}, {"available": True},
                    {"available": "not-a-number"},
                    {"available": math.nan}, {"available": math.inf}):
        trader, option_broker, placed = _run_remote_options(tmp_path, balance)
        assert placed == []
        assert option_broker.open_calls == 0
        assert get_risk_state().open_positions == 0
        assert getattr(trader, "_opt_registry_keys", set()) == set()
        assert not (Path(tmp_path) / "runtime" /
                    "options_registry.json").exists()


def test_remote_options_accepts_numeric_string_balance(tmp_path):
    trader, option_broker, placed = _run_remote_options(
        tmp_path, {"available": "1000.25"})

    assert len(placed) == 1
    assert option_broker.open_calls == 1
    assert get_risk_state().open_positions == 0
    registry = Path(tmp_path) / "runtime" / "options_registry.json"
    data = json.loads(registry.read_text(encoding="utf-8"))
    assert data["2026-10-14"]
    assert trader._last_option_order_ids == ["remote-option-1"]
