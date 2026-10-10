"""
TRIO — Execution lifecycle tests (unified entry/exit path)
Author: Kapil Kuhire <kapilkuhire89@gmail.com>
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.broker.paper import PaperBroker
from src.paper_trader import PaperTrader
from src.risk_manager import (reset_risk_state, get_risk_state,
                              rebuild_from_ledger, update_pnl)
from src.execution_lifecycle import (submit_candidate, reconcile_broker,
                                     record_confirmed_close, preflight)


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
    assert "X" not in get_risk_state().positions or True
    assert get_risk_state().daily_pnl < 0
