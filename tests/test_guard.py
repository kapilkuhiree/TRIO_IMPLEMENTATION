"""
TRIO — Unit tests for the Active Manager (Guard logic).
Author: Kapil Kuhire <kapilkuhire89@gmail.com>
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.paper_trader import PaperTrader
from src.indicators import IndicatorReading
from types import SimpleNamespace

def _trader():
    t = PaperTrader(symbols=["RELIANCE.NS"], timeframe="1d",
                       initial_capital=10000.0, test_mode=True)
    # Give it a "filled" order record so _manage_open_positions can read SL/TP
    t.broker.orders["test_id"] = SimpleNamespace(
        symbol="RELIANCE.NS", status="FILLED", stop_loss=90.0, target=115.0
    )
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
        quantity=10, side="LONG", avg_price=100.0, order_id="test_id"
    )
    with patch("src.paper_trader.fetch_market_data", return_value=_md(116.0)):
        t._manage_open_positions()
    
    assert "RELIANCE.NS" not in t.broker.positions
    assert len(t.broker.closed_trades) == 1
    assert t.broker.closed_trades[0]["reason"].startswith("target-hit")
