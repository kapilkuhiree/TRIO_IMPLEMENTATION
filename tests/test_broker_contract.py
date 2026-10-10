"""
TRIO — Broker contract tests (Phase 5)
Author: Kapil Kuhire <kapilkuhire89@gmail.com>
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.paper_trader import PaperTrader


def test_unknown_broker_provider_raises(monkeypatch):
    monkeypatch.delenv("TRIO_BROKER_PROVIDER", raising=False)
    with patch("src.paper_trader.load_config",
               return_value={"broker": {"provider": "typo"},
                             "risk_management": {"capital": 10000},
                             "forward_test": {}}):
        try:
            PaperTrader(symbols=["A"], test_mode=True)
            assert False, "should have raised on typo provider"
        except ValueError as exc:
            assert "Unknown broker provider" in str(exc)


def test_pending_not_booked_as_filled():
    """A PENDING order must not be treated as a confirmed fill."""
    from src.broker.base import BrokerOrder
    from src.broker.paper import PaperBroker
    # Fake a broker that returns a PENDING order (MegaBull pending leg)
    class PendingBroker(PaperBroker):
        def place_order(self, symbol, side, quantity, price=None,
                        stop_loss=None, target=None, reason="order"):
            return BrokerOrder(order_id="p1", symbol=symbol, side=side,
                               quantity=quantity, price=price or 0.0,
                               status="PENDING", broker="pending")
    t = PaperTrader(symbols=["X"], timeframe="1d", initial_capital=100000,
                     test_mode=True)
    t.broker = PendingBroker(initial_capital=100000)
    t.broker.positions = {}
    plan = {"candidates": [{
        "symbol": "X", "action": "BUY", "entry_price": 100.0,
        "stop_loss": 90.0, "target": 115.0, "position_size": 10,
        "confidence": 80, "rank": 90.0, "setup_name": "x"}]}
    placed = t.execute_plan(plan)
    assert placed == []
    assert t.broker.positions == {}
