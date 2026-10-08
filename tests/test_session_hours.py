"""
TRIO — Session-hours guard tests.

The paper loop must not open NEW positions outside 09:20–15:15 IST
(the live 21:23 SELL filled on stale candles). Open positions still get
stop/target management — only entries stop. The history-replay tool
bypasses scan_once entirely, so it is unaffected by design.
"""

import sys
from datetime import time as dtime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.paper_trader import PaperTrader
from src.screener import RankedCandidate
from src.signal_engine import TradeSignal


def _cand(action="BUY", symbol="RELIANCE.NS", size=10):
    sig = TradeSignal(symbol=symbol, action=action, entry_price=100.0,
                      stop_loss=95.0, target=107.5, position_size=size,
                      confidence=80, reasoning=["x"])
    return RankedCandidate(rank=90.0, signal=sig, edge_atr=2.0,
                           setup_name="pullback-long")


def _trader():
    return PaperTrader(symbols=["RELIANCE.NS"], timeframe="1d",
                       initial_capital=10000.0)


def test_session_open_boundaries():
    t = _trader()
    assert t._session_open(dtime(9, 19)) is False
    assert t._session_open(dtime(9, 20)) is True
    assert t._session_open(dtime(12, 0)) is True
    assert t._session_open(dtime(15, 15)) is True
    assert t._session_open(dtime(15, 16)) is False
    assert t._session_open(dtime(21, 23)) is False  # the live incident


def test_after_hours_scan_places_no_orders_but_manages_stops():
    t = _trader()
    t.broker.place_order("RELIANCE.NS", "BUY", 10, price=100.0,
                         stop_loss=90.0, target=115.0)
    with patch("src.screener.screen", return_value=[_cand("BUY")]), \
         patch.object(PaperTrader, "_session_open", return_value=False), \
         patch.object(PaperTrader, "_maybe_eod_square_off",
                      return_value=[]), \
         patch("src.paper_trader.fetch_market_data") as md:
        # Simulates a LUNCH BREAK (closed market, not end of day):
        # price 85 < stop 90 -> stop management must still fire.
        # Post-15:15 the EOD square-off settles everything instead
        # (see tests/test_eod_squareoff.py).
        md.return_value.latest_price = 85.0
        md.return_value.ohlcv = None
        out = t.scan_once()
    assert out == []
    assert t.broker.positions == {}
    assert t.broker.closed_trades[-1]["reason"] == "stop-hit"


def test_in_hours_scan_executes_normally():
    t = _trader()
    with patch("src.screener.screen", return_value=[_cand("BUY")]), \
         patch.object(PaperTrader, "_session_open", return_value=True), \
         patch.object(PaperTrader, "_guard_open_positions", return_value=[]), \
         patch.object(PaperTrader, "_manage_open_positions", return_value=None):
        out = t.scan_once()
    assert len(out) == 1
    assert "RELIANCE.NS" in t.broker.positions


def test_session_open_misconfigured_window_fails_open():
    t = _trader()
    with patch("src.paper_trader.load_config",
               return_value={"forward_test": {"session_start": "xx",
                                             "session_end": "yy"}}):
        # A bad window must never halt trading silently.
        assert t._session_open(dtime(3, 0)) is True
