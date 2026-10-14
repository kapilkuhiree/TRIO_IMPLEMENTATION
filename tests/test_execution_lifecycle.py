"""
TRIO — Execution lifecycle tests (unified entry/exit path)
Author: Kapil Kuhire <kapilkuhire89@gmail.com>
"""
import math
import sys
from collections import UserDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.broker.paper import PaperBroker
from src.broker.base import BrokerOrder
from src.paper_trader import PaperTrader
from src.risk_manager import (reset_risk_state, get_risk_state,
                              rebuild_from_ledger, update_pnl)
from src.execution_lifecycle import (submit_candidate, reconcile_broker,
                                      record_confirmed_close,
                                      record_confirmed_fill, preflight)


def setup_function():
    reset_risk_state()


def _trader():
    t = PaperTrader(symbols=["X"], timeframe="1d", initial_capital=100000,
                    test_mode=True)
    t.broker = PaperBroker(initial_capital=100000)
    t.broker_name = "paper"
    return t


def _cand(symbol="X", action="BUY", price=100.0, size=10):
    return {"symbol": symbol, "action": action, "entry_price": price,
            "stop_loss": 90.0, "target": 115.0, "position_size": size,
            "confidence": 80, "rank": 90.0, "setup_name": "x", "risk_amount": 100.0}


def test_single_engine_scan_and_plan_paths_agree():
    """scan_once and execute_plan must register risk identically."""
    t = _trader()
    r1 = submit_candidate(t, _cand("A"), reason="scan", proposed={})
    assert r1["status"] == "FILLED"
    assert get_risk_state().open_positions == 1
    t2 = _trader()
    placed = t2.execute_plan({"candidates": [_cand("B")]})
    assert len(placed) == 1
    assert get_risk_state().open_positions >= 1


def test_unconfirmed_broker_orders_do_not_register_risk_or_alert():
    """Pending/unknown submissions stop before confirmed-fill side effects."""
    from unittest.mock import patch

    class _UnconfirmedBroker:
        provider_name = "megabull"

        def __init__(self, status):
            self.status = status
            self.positions = {}
            self.closed_trades = []

        def get_balance(self):
            return {"available": 100000.0}

        def place_order(self, **kwargs):
            return BrokerOrder(order_id="remote-1", symbol=kwargs["symbol"],
                               side=kwargs["side"], quantity=kwargs["quantity"],
                               status=self.status, broker="megabull")

    with patch("src.alerts.send_signal_alert") as alert:
        for status in ("PENDING", "UNKNOWN"):
            reset_risk_state()
            t = _trader()
            t.broker = _UnconfirmedBroker(status)
            result = submit_candidate(t, _cand(), proposed={})
            assert result["status"] == "PENDING"
            assert get_risk_state().open_positions == 0
            assert get_risk_state().positions == {}
        assert alert.call_count == 0


def test_record_confirmed_fill_rejects_unconfirmed_order():
    from unittest.mock import patch

    t = _trader()
    order = BrokerOrder(order_id="remote-1", status="UNKNOWN")
    with patch("src.alerts.send_signal_alert") as alert:
        result = record_confirmed_fill(t, _cand(), order, 10)
    assert result == {}
    assert get_risk_state().open_positions == 0
    assert alert.call_count == 0


def test_sector_atomic_plan_two_banks_one_tech():
    """Three candidates, banking cap: banks see the updated portfolio."""
    from unittest.mock import patch
    t = _trader()
    # force a tight sector cap via config patch inside preflight
    plan = {"candidates": [
        {"symbol": "HDFCBANK.NS", "action": "BUY", "entry_price": 1000.0,
         "stop_loss": 950.0, "target": 1100.0, "position_size": 10,
         "confidence": 80, "rank": 90.0, "setup_name": "x"},
        {"symbol": "ICICIBANK.NS", "action": "BUY", "entry_price": 1000.0,
         "stop_loss": 950.0, "target": 1100.0, "position_size": 10,
         "confidence": 80, "rank": 89.0, "setup_name": "x"},
        {"symbol": "TCS.NS", "action": "BUY", "entry_price": 1000.0,
         "stop_loss": 950.0, "target": 1100.0, "position_size": 10,
         "confidence": 80, "rank": 88.0, "setup_name": "x"},
    ]}
    with patch("src.risk_manager.load_config", return_value={
            "risk_management": {"capital": 100000, "max_open_positions": 10,
                                "max_exposure_per_sector_pct": 15},
            "trading": {"allow_shorts": False}}):
        placed = t.execute_plan(plan)
    syms = [r["symbol"] for r in placed]
    assert "HDFCBANK.NS" in syms  # first bank fits: 10k/100k = 10% <= 15
    assert "ICICIBANK.NS" not in syms  # second would be 20% > 15
    assert "TCS.NS" in syms  # tech is independent


def test_halt_max_positions_dynamic_clears():
    """max_positions is dynamic: falls away when count drops; daily persists."""
    from src.risk_manager import add_position, close_position
    import src.risk_manager as rm
    cfg = {"risk_management": {"capital": 100000, "max_open_positions": 2,
                               "max_daily_loss_pct": 50.0}}
    from unittest.mock import patch
    with patch("src.risk_manager.load_config", return_value=cfg):
        add_position("A", 1, 10.0, 1.0)
        assert get_risk_state().halt_active is False
        add_position("B", 1, 10.0, 1.0)
        assert get_risk_state().halt_active is True
        assert "max_positions" in get_risk_state().halts
        close_position("A")
        rm._check_halt()
        assert "max_positions" not in get_risk_state().halts
        assert get_risk_state().halt_active is False


def test_rebuild_dedup_broker_and_jsonl(tmp_path, monkeypatch):
    """Same close in broker list + JSONL counts once; test-mode ignored."""
    import json
    from datetime import datetime, timedelta, timezone
    IST = timezone(timedelta(hours=5, minutes=30))
    today = datetime.now(IST).strftime("%Y-%m-%d")
    ts = datetime.now(IST).isoformat()
    rec = {"event": "close", "mode": "paper", "symbol": "A", "side": "LONG",
           "entry": 100.0, "exit": 110.0, "quantity": 10, "pnl": 100.0,
           "ts": ts, "order_id": "OID-1"}
    lp = tmp_path / "trade_log.jsonl"
    lp.write_text(json.dumps(rec) + "\n"
                  + json.dumps({**rec, "mode": "test", "pnl": 999.0}) + "\n"
                  + json.dumps({**rec, "pnl": 5.0,
                                 "ts": "2020-01-01T00:00:00+00:00"}) + "\n")
    monkeypatch.setenv("TRIO_BROKER_PROVIDER", "paper")
    from unittest.mock import patch as _p
    with _p("src.risk_manager.load_config", return_value={
            "risk_management": {"capital": 100000},
            "logging": {"trade_log": str(lp)}}):
        s = rebuild_from_ledger({}, [{**rec, "order_id": "OID-1", "pnl": 100.0,
                                      "closed_at": ts}], today=today)
    assert s.daily_pnl == 100.0  # not 200, not 1104, not 105
    assert s.consecutive_wins == 1


def test_rebuild_restores_consecutive_losses():
    seq = [{"pnl": -10.0, "closed_at": "", "order_id": f"K{i}"} for i in range(3)]
    s = rebuild_from_ledger({}, seq, today="2026-10-10")
    assert s.consecutive_losses == 3
    assert s.consecutive_wins == 0


def test_rebuild_orders_close_events_before_restoring_streak():
    """Ledger input order is not a reliable proxy for close chronology."""
    from unittest.mock import patch
    records = [
        {"pnl": 25.0, "closed_at": "2026-10-10T10:05:00+05:30",
         "order_id": "later"},
        {"pnl": -10.0, "closed_at": "2026-10-10T10:00:00+05:30",
         "order_id": "earlier"},
    ]
    with patch("src.risk_manager.load_config", return_value={
            "risk_management": {"capital": 100000}}):
        s = rebuild_from_ledger({}, records, today="2026-10-10")
    assert s.consecutive_wins == 1
    assert s.consecutive_losses == 0


def test_rebuild_ist_mapping_utc():
    from unittest.mock import patch as _p
    import json
    # 2026-10-09T19:00:00Z == 2026-10-10 00:30 IST -> belongs to 10th
    rec = {"event": "close", "mode": "paper", "symbol": "A", "pnl": 50.0,
           "ts": "2026-10-09T19:00:00+00:00", "order_id": "UTC-1"}
    with _p("src.risk_manager.load_config", return_value={
            "risk_management": {"capital": 100000},
            "logging": {"trade_log": "C:/no/such/file.jsonl"}}):
        s = rebuild_from_ledger({}, [rec], today="2026-10-10")
    assert s.daily_pnl == 50.0
    with _p("src.risk_manager.load_config", return_value={
            "risk_management": {"capital": 100000},
            "logging": {"trade_log": "C:/no/such/file.jsonl"}}):
        s2 = rebuild_from_ledger({}, [rec], today="2026-10-09")
    assert s2.daily_pnl == 0.0


def test_guard_megabull_no_longer_skips():
    """Guard is broker-agnostic: even provider=megabull must ladder-manage."""
    from types import SimpleNamespace
    from unittest.mock import patch
    t = _trader()
    t.broker_name = "megabull"
    t.broker.orders["x"] = SimpleNamespace(symbol="X", status="FILLED",
                                            stop_loss=90.0, target=130.0, side="BUY")
    pos = SimpleNamespace(quantity=10, side="LONG", avg_price=100.0,
                          halved=False, current_price=100.0, pnl=0.0, pnl_pct=0.0)
    t.broker.positions["X"] = pos
    md = SimpleNamespace(latest_price=109.0, ohlcv=None)
    with patch("src.paper_trader.fetch_market_data", return_value=md), \
         patch("src.alerts.send_exit_alert"):
        out = t._guard_open_positions()
    assert getattr(pos, "halved", False) is True
    assert pos.quantity == 5


def test_record_confirmed_close_updates_all():
    t = _trader()
    t.broker.place_order("X", "BUY", 10, price=100.0, stop_loss=90.0, target=120.0)
    update_pnl(-10.0)
    rec = t.broker.close_position("X", 80.0, "stop-hit")
    out = record_confirmed_close(t, "X", rec)
    assert out
    assert get_risk_state().daily_pnl < 0


def test_record_confirmed_close_idempotent():
    """Calling record_confirmed_close twice with the same record updates P&L only once."""
    t = _trader()
    rec = {"symbol": "X", "side": "LONG", "quantity": 10, "entry": 100.0,
           "exit": 110.0, "pnl": 100.0, "reason": "target-hit", "order_id": "CLS-100"}
    pnl_before = get_risk_state().daily_pnl
    record_confirmed_close(t, "X", rec)
    pnl_after1 = get_risk_state().daily_pnl
    record_confirmed_close(t, "X", rec)
    pnl_after2 = get_risk_state().daily_pnl
    assert pnl_after1 == pnl_before + 100.0
    assert pnl_after2 == pnl_after1


def test_remote_reconcile_keeps_uncertainty_after_rebuild():
    """Risk rebuild must not erase a pending/unknown broker halt."""
    class _Remote:
        provider_name = "megabull"
        positions = {}
        closed_trades = []
        _pending_orders = {"megabull_1"}

        def refresh_positions(self):
            return None

    t = _trader()
    t.broker = _Remote()
    result = reconcile_broker(t)
    assert result["uncertain"] is True
    assert get_risk_state().halt_active is True
    assert "broker_uncertain" in get_risk_state().halts


def test_remote_reconcile_keeps_uncertainty_when_refresh_reports_failure():
    class _Remote:
        provider_name = "megabull"
        positions = {}
        closed_trades = []
        _pending_orders = set()

        def refresh_positions(self):
            return False

    t = _trader()
    t.broker = _Remote()
    result = reconcile_broker(t)
    assert result["uncertain"] is True
    assert get_risk_state().halts["broker_uncertain"]


def test_preflight_fails_closed_when_risk_checks_error():
    from unittest.mock import patch
    t = _trader()
    with patch("src.risk_manager.would_breach_sector",
               side_effect=RuntimeError("sector data unavailable")):
        result = submit_candidate(t, _cand("RISK"), proposed={})
    assert result["status"] == "SKIP"
    assert result["reason"] == "sector-check-failed"
    assert get_risk_state().open_positions == 0


class _BalanceBroker:
    """Remote double deliberately exposing only no-argument get_balance()."""
    provider_name = "megabull"

    def __init__(self, balance):
        self.balance = balance
        self.positions = {}
        self.balance_calls = 0
        self.place_order = Mock(return_value=BrokerOrder(
            order_id="remote-margin-1", status="FILLED", broker="megabull"))

    def get_balance(self):
        self.balance_calls += 1
        if isinstance(self.balance, Exception):
            raise self.balance
        return self.balance


@pytest.mark.parametrize("balance", [
    pytest.param(None, id="null-response"),
    pytest.param([], id="list-response"),
    pytest.param(SimpleNamespace(get=lambda key, default=0: 100000),
                 id="non-mapping-with-get"),
    pytest.param({}, id="missing-available"),
    pytest.param({"available": None}, id="null-available"),
    pytest.param({"available": True}, id="true"),
    pytest.param({"available": False}, id="false"),
    pytest.param({"available": "not-a-number"}, id="non-numeric"),
    pytest.param({"available": math.nan}, id="nan"),
    pytest.param({"available": math.inf}, id="infinity"),
    pytest.param({"available": -math.inf}, id="negative-infinity"),
    pytest.param({"available": "NaN"}, id="nan-string"),
    pytest.param({"available": "inf"}, id="infinity-string"),
    pytest.param({"available": "-inf"}, id="negative-infinity-string"),
])
def test_preflight_rejects_invalid_available_balance(balance, monkeypatch):
    """Invalid margin must block before submission and risk registration."""
    broker = _BalanceBroker(balance)
    trader = SimpleNamespace(broker=broker, _log_event=Mock())
    register = Mock()
    monkeypatch.setattr("src.risk_manager.add_position", register)
    proposed = {}

    assert preflight(trader, _cand(), proposed=proposed) == (
        False, "margin-check-failed")
    assert submit_candidate(trader, _cand(), proposed=proposed) == {
        "status": "SKIP", "reason": "margin-check-failed"}
    broker.place_order.assert_not_called()
    register.assert_not_called()
    assert get_risk_state().positions == {}
    assert get_risk_state().open_positions == 0
    assert proposed == {}


def test_preflight_without_broker_fails_closed():
    trader = SimpleNamespace(broker=None, _log_event=Mock())
    assert preflight(trader, _cand(), proposed={}) == (
        False, "margin-check-failed")
    assert submit_candidate(trader, _cand(), proposed={}) == {
        "status": "SKIP", "reason": "margin-check-failed"}
    assert get_risk_state().positions == {}


def test_preflight_balance_exception_blocks_without_logging_payload(caplog):
    secret = "private-balance-response-marker"
    broker = _BalanceBroker(RuntimeError(secret))
    trader = SimpleNamespace(broker=broker, _log_event=Mock())

    assert submit_candidate(trader, _cand(), proposed={}) == {
        "status": "SKIP", "reason": "margin-check-failed"}
    broker.place_order.assert_not_called()
    assert get_risk_state().positions == {}
    assert secret not in caplog.text
    assert secret not in str(trader._log_event.call_args_list)


@pytest.mark.parametrize("available", [0, -1, "0", "-1"])
def test_preflight_distinguishes_exhausted_balance(available):
    broker = _BalanceBroker({"available": available})
    trader = SimpleNamespace(broker=broker, _log_event=Mock())

    assert preflight(trader, _cand(), proposed={}) == (
        False, "margin-exhausted")
    assert submit_candidate(trader, _cand(), proposed={}) == {
        "status": "SKIP", "reason": "margin-exhausted"}
    broker.place_order.assert_not_called()
    assert get_risk_state().positions == {}


@pytest.mark.parametrize("balance", [
    {"available": 100000}, {"available": 100000.25},
    {"available": "100000.25"}, UserDict({"available": "1e5"}),
])
def test_valid_balance_submits_with_no_argument_remote_double(
        balance, monkeypatch):
    monkeypatch.setattr("src.alerts.send_signal_alert", Mock())
    broker = _BalanceBroker(balance)
    trader = SimpleNamespace(broker=broker, _log_event=Mock())

    result = submit_candidate(trader, _cand(), proposed={})

    assert result["status"] == "FILLED"
    assert broker.balance_calls == 1
    broker.place_order.assert_called_once()
    assert get_risk_state().open_positions == 1
    assert "X" in get_risk_state().positions
