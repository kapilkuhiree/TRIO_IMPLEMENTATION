"""
TRIO — Runtime risk wiring tests (Phase 3)
Author: Kapil Kuhire <kapilkuhire89@gmail.com>
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.broker.paper import PaperBroker
from src.paper_trader import PaperTrader
from src.signal_engine import TradeSignal
from src.risk_manager import (
    add_position, close_position, get_risk_state, rebuild_from_ledger,
    reset_risk_state, update_pnl, _check_halt,
)


def setup_function():
    reset_risk_state()


def _plan_with(symbol="RELIANCE.NS", price=100.0, stop=90.0, target=115.0, size=10):
    return {
        "candidates": [{
            "symbol": symbol, "action": "BUY", "entry_price": price,
            "stop_loss": stop, "target": target,
            "position_size": size, "confidence": 80, "rank": 90.0,
            "setup_name": "pullback-long", "risk_amount": 100.0,
        }]
    }


def test_daily_loss_activates_halt():
    update_pnl(-50000.0)  # beyond 5% even on a 500k account
    s = get_risk_state()
    assert s.halt_active is True
    assert "Daily loss" in s.halt_reason


def test_unrealized_loss_contributes_to_risk():
    # Realized + unrealized together should trip the halt even when realized
    # alone would not.
    update_pnl(-10000.0)
    _check_halt(unrealized_total=-30000.0)  # total -40000
    assert get_risk_state().halt_active is True
    assert "unrealized" in get_risk_state().halt_reason.lower()


def test_max_positions_blocks_new_trade():
    for i in range(5):
        add_position(f"SYM{i}", 1, 10.0, 10.0)
    assert get_risk_state().halt_active is True
    t = PaperTrader(symbols=["SYM6"], timeframe="1d", initial_capital=100000.0,
                     test_mode=True)
    t.broker = PaperBroker(initial_capital=100000.0)
    t.broker.positions = {f"SYM{i}": type("P", (), {
        "quantity": 1, "side": "LONG", "avg_price": 10.0, "current_price": 10.0,
    })() for i in range(5)}
    # max_positions=5 should block a 6th entry
    plan = _plan_with("SYM6")
    placed = t.execute_plan(plan)
    assert placed == []


def test_partial_exits_update_risk():
    t = PaperTrader(symbols=["RELIANCE.NS"], timeframe="1d",
                     initial_capital=100000.0, test_mode=True)
    t.broker.place_order("RELIANCE.NS", "BUY", 10, price=100.0,
                          stop_loss=90.0, target=115.0)
    pos = t.broker.positions["RELIANCE.NS"]
    t.broker.update_position("RELIANCE.NS", 115.0)
    # Simulate a partial close like the guard would do
    rec = t.broker.close_position("RELIANCE.NS", 115.0, "partial", fraction=0.5)
    update_pnl(float(rec["pnl"]))
    assert get_risk_state().daily_pnl == float(rec["pnl"])
    # remaining 5 still held, close the rest
    rec2 = t.broker.close_position("RELIANCE.NS", 90.0, "stop-hit")
    update_pnl(float(rec2["pnl"]))
    close_position("RELIANCE.NS")
    assert get_risk_state().open_positions == 0


def test_rejected_orders_do_not_consume_risk():
    t = PaperTrader(symbols=["SMALL"], timeframe="1d", initial_capital=100.0,
                     test_mode=True)
    t.broker = PaperBroker(initial_capital=100.0)
    # Will be rejected for insufficient capital (price 100 * 10 = 1000 > 100)
    plan = _plan_with("SMALL", price=100.0, size=10)
    placed = t.execute_plan(plan)
    assert placed == []
    assert get_risk_state().open_positions == 0
    assert get_risk_state().daily_pnl == 0.0


def test_restart_restores_exposure():
    add_position("RELIANCE.NS", 10, 100.0, 50.0)
    add_position("TCS.NS", 5, 50.0, 25.0)
    broker_positions = {"RELIANCE.NS": type("P", (), {
        "quantity": 10, "side": "LONG", "avg_price": 100.0})(),
        "TCS.NS": type("P", (), {
            "quantity": 5, "side": "LONG", "avg_price": 50.0})()}
    closed = [{"pnl": 100.0, "closed_at": "2026-10-10T10:00:00+00:00"},
              {"pnl": -20.0, "closed_at": "2026-10-10T11:00:00+00:00"}]
    s = rebuild_from_ledger(broker_positions, closed, today="2026-10-10")
    assert s.open_positions == 2
    assert "RELIANCE.NS" in s.positions


def test_broker_uncertainty_blocks_new_entries():
    t = PaperTrader(symbols=["PEND"], timeframe="1d", initial_capital=100000.0,
                     test_mode=True)
    t.broker = PaperBroker(initial_capital=100000.0)
    # Simulate a PENDING position marker (MegaBull would do this)
    t.broker.positions["PEND"] = type("P", (), {
        "quantity": 1, "side": "LONG", "avg_price": 100.0,
        "status": "PENDING",
    })()
    placed = t.execute_plan(_plan_with("SEMI", price=50.0))
    assert placed == []
