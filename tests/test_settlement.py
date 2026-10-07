"""
TRIO — Settlement-rule tests (no naked shorts on a CNC-style account).
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.paper_trader import PaperTrader
from src.screener import RankedCandidate
from src.signal_engine import TradeSignal
from src.indicators import TechnicalReadings


def _cand(action="SELL", symbol="RELIANCE.NS", size=10):
    # Long-only engine: SELL candidates never occur in pullback mode.
    # These tests drive the settlement layer directly in signals mode.
    sig = TradeSignal(symbol=symbol, action=action, entry_price=100.0,
                      stop_loss=105.0, target=92.0, position_size=size,
                      confidence=80, reasoning=["x"])
    return RankedCandidate(rank=90.0, signal=sig, edge_atr=2.0,
                           setup_name="composite-short")


def _readings():
    return TechnicalReadings(symbol="T", indicators={}, summary={})


def _trader():
    t = PaperTrader(symbols=["RELIANCE.NS"], timeframe="1d",
                    initial_capital=10000.0)
    return t


def _in_hours(*a, **k):
    from datetime import time as dtime
    return dtime(10, 0)


def test_sell_without_holdings_is_skipped():
    t = _trader()
    with patch("src.paper_trader.load_config", return_value={"trading": {"allow_shorts": False}}), \
         patch("src.screener.screen", return_value=[_cand("SELL")]), \
         patch.object(t, "_session_open", return_value=True), \
         patch("src.paper_trader.PaperTrader._guard_open_positions", return_value=[]):
        t.scan_once()
    assert t.broker.positions == {}
    assert t.signals_log[-1].get("skipped") == "no-holdings"


def test_sell_with_holdings_executes():
    t = _trader()
    t.broker.place_order("RELIANCE.NS", "BUY", 10, price=100.0)
    with patch("src.screener.screen", return_value=[_cand("SELL", size=10)]), \
         patch.object(t, "_session_open", return_value=True), \
         patch("src.paper_trader.PaperTrader._guard_open_positions",
               return_value=[]), \
         patch("src.paper_trader.PaperTrader._manage_open_positions",
               return_value=None):
        t.scan_once()
    assert t.broker.positions == {}  # bought then sold: flat
    assert "skipped" not in t.signals_log[-1]


def test_sell_clipped_to_holdings():
    t = _trader()
    t.broker.place_order("RELIANCE.NS", "BUY", 4, price=100.0)
    with patch("src.screener.screen", return_value=[_cand("SELL", size=10)]), \
         patch.object(t, "_session_open", return_value=True), \
         patch("src.paper_trader.PaperTrader._guard_open_positions",
               return_value=[]), \
         patch("src.paper_trader.PaperTrader._manage_open_positions",
               return_value=None):
        t.scan_once()
    # 4 held, 10 wanted -> clipped to 4, position closed flat.
    assert t.broker.positions == {}
    assert "skipped" not in t.signals_log[-1]


def test_buy_unaffected_by_settlement_rule():
    t = _trader()
    with patch("src.screener.screen", return_value=[_cand("BUY")]), \
         patch.object(t, "_session_open", return_value=True), \
         patch("src.paper_trader.PaperTrader._guard_open_positions",
               return_value=[]), \
         patch("src.paper_trader.PaperTrader._manage_open_positions",
               return_value=None):
        t.scan_once()
    assert "RELIANCE.NS" in t.broker.positions
    assert "skipped" not in t.signals_log[-1]


def test_repeat_sell_on_short_is_skipped():
    """Hold-skip regression (2026-10-06): a SELL signal on an already
    held SHORT must not place a second order."""
    t = _trader()
    t.broker.place_order("RELIANCE.NS", "SELL", 10, price=100.0)
    with patch("src.screener.screen", return_value=[_cand("SELL", size=10)]), \
         patch.object(t, "_session_open", return_value=True), \
         patch("src.paper_trader.PaperTrader._guard_open_positions",
               return_value=[]), \
         patch("src.paper_trader.PaperTrader._manage_open_positions",
               return_value=None):
        t.scan_once()
    assert t.broker.positions["RELIANCE.NS"].quantity == 10
    assert t.signals_log[-1].get("skipped") == "already-holding"
