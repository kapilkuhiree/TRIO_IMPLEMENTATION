"""
TRIO — Unit tests for the Active Manager (Guard logic).
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.paper_trader import PaperTrader
from src.indicators import IndicatorReading
from types import SimpleNamespace


def _order_for(sym, stop, target):
    return SimpleNamespace(symbol=sym, status="FILLED",
                           stop_loss=stop, target=target)


def _trader():
    t = PaperTrader(symbols=["RELIANCE.NS"], timeframe="1d",
                       initial_capital=10000.0)
    # Give it a "filled" order record so _manage_open_positions can read SL/TP
    t.broker.orders["test_id"] = _order_for(
        "RELIANCE.NS", 90.0, 115.0)
    return t

def _md(close=100.0):
    return SimpleNamespace(latest_price=close)

def test_guard_stop_hit():
    t = _trader()
    # Manually add a position
    t.broker.positions["RELIANCE.NS"] = SimpleNamespace(
        quantity=10, side="LONG", avg_price=100.0, order_id="test_id"
    )
    with patch("src.paper_trader.fetch_market_data", return_value=_md(89.0)):
        t._manage_open_positions()
    
    assert "RELIANCE.NS" not in t.broker.positions
    assert len(t.broker.closed_trades) == 1
    assert t.broker.closed_trades[0]["reason"].startswith("stop-hit")

def test_guard_target_hit():
    t = _trader()
    t.broker.positions["RELIANCE.NS"] = SimpleNamespace(
        quantity=10, side="LONG", avg_price=100.0, order_id="test_id",
        halved=False, booked_pnl=0.0
    )
    with patch("src.paper_trader.fetch_market_data", return_value=_md(116.0)):
        t._manage_open_positions()

    assert "RELIANCE.NS" not in t.broker.positions
    assert len(t.broker.closed_trades) == 1
    assert t.broker.closed_trades[0]["reason"].startswith("target-hit")


def _paper_trader():
    from src.broker.paper import PaperBroker
    from src.paper_trader import PaperTrader as PT
    t2 = PaperTrader(symbols=["RELIANCE.NS"], timeframe="1d",
                     initial_capital=10000.0)
    t2.broker = PaperBroker(initial_capital=10000.0)
    t2.broker_name = "paper"
    return t2


def test_guard_target_hit_still_closes_when_called_directly():
    # Loose guard (not ladder): target-hit closes in isolation. The guard
    # path now skips MegaBull mirrors (remote safety); this test pins the
    # local paper broker so the full-close path must still fire.
    t2 = _paper_trader()
    t2.broker.place_order("RELIANCE.NS", "BUY", 10, price=100.0,
                          stop_loss=90.0, target=115.0)
    assert t2.broker.get_positions()[0].halved is False
    with patch("src.paper_trader.fetch_market_data", return_value=_md(116.0)):
        t2._manage_open_positions()
    assert t2.broker.positions == {}
    assert t2.broker.closed_trades[-1]["reason"] == "target-hit"


def test_ladder_t1_partial_and_breakeven():
    """Ladder Phase 1: +0.8R takes 50% and moves stop to breakeven (risk-free).
    Entry 100, stop 90 => risk 10, T1 at 0.8R is 108. Price 109 should fire."""
    t = _paper_trader()
    t.broker.orders["test_id"] = _order_for("RELIANCE.NS", 90.0, 115.0)
    t.broker.positions["RELIANCE.NS"] = SimpleNamespace(
        quantity=10, side="LONG", avg_price=100.0,
        halved=False, current_price=100.0, pnl=0.0, pnl_pct=0.0)
    from unittest.mock import patch as _p
    with _p("src.paper_trader.fetch_market_data", return_value=_md(109.0)), \
         _p("src.paper_trader.load_config",
            return_value={"risk_management": {
                "partial_at_r": 0.8, "partial_fraction": 0.5}}):
        with _p("src.alerts.send_exit_alert"):
            t._guard_open_positions()
    assert t.broker.positions["RELIANCE.NS"].quantity == 5
    assert t.broker.positions["RELIANCE.NS"].halved is True
    order = t.broker.orders["test_id"]
    assert order.stop_loss == 100.0  # breakeven


def test_ladder_targets_derived():
    from src.risk_manager import ladder_targets
    assert ladder_targets(100, 90, "BUY")["T1"] == 108.0
    assert ladder_targets(100, 110, "SELL")["T1"] == 92.0
