"""
TRIO — EOD square-off tests (2026-10-06 regression).

On 2026-10-06 the trading loop kept 2 positions open 25 minutes past
the 15:15 IST close: no exit, no PASS/FAIL record, no Telegram alert —
because square-off only ran on Ctrl+C. Positions must now settle once,
automatically, right after session end — from BOTH the CLI loop and
the dashboard loop (they share scan_once).
"""

import sys
from datetime import time as dtime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.paper_trader import PaperTrader


def _trader_with_position():
    t = PaperTrader(symbols=["RELIANCE.NS"], timeframe="1d",
                    initial_capital=10000.0, test_mode=True)
    t.broker.place_order("RELIANCE.NS", "BUY", 10, price=100.0,
                         stop_loss=90.0, target=115.0)
    return t


def test_eod_squares_off_once_after_close():
    t = _trader_with_position()
    with patch("src.paper_trader.fetch_market_data") as md:
        md.return_value.latest_price = 110.0
        closed = t._maybe_eod_square_off(now_ist=dtime(15, 20))
    assert len(closed) == 1
    assert closed[0]["reason"] == "eod-squareoff"
    assert closed[0]["result"] == "PASS"  # 100 -> 110
    assert t.broker.positions == {}
    assert t._eod_squared is True
    # idempotent: a second pass must not re-square or double-log
    assert t._maybe_eod_square_off(now_ist=dtime(15, 25)) == []
    assert len(t.broker.closed_trades) == 1


def test_no_eod_before_session_end():
    t = _trader_with_position()
    # Lunch break (12:30): market closed, but it is NOT end of day.
    assert t._maybe_eod_square_off(now_ist=dtime(12, 30)) == []
    assert "RELIANCE.NS" in t.broker.positions
    # Pre-open next morning: also untouched.
    assert t._maybe_eod_square_off(now_ist=dtime(8, 0)) == []
    assert "RELIANCE.NS" in t.broker.positions


def test_scan_once_after_close_triggers_eod():
    """Full wiring: the market-closed branch of scan_once settles the day.

    session_end patched to 00:00 so any wall-clock time counts as EOD —
    the test is deterministic whenever it runs.
    """
    t = _trader_with_position()
    with patch("src.screener.screen", return_value=[]), \
         patch.object(PaperTrader, "_session_open", return_value=False), \
         patch("src.paper_trader.load_config",
               return_value={"forward_test": {"session_end": "00:00"},
                             "trading": {"allow_shorts": False}}), \
         patch("src.paper_trader.fetch_market_data") as md:
        md.return_value.latest_price = 95.0
        md.return_value.ohlcv = None
        out = t.scan_once()
    assert out == []
    assert t.broker.positions == {}
    assert t.broker.closed_trades[-1]["reason"] == "eod-squareoff"
    assert t._eod_squared is True


def test_flag_rearms_when_next_session_opens():
    t = _trader_with_position()
    t._eod_squared = True
    with patch("src.screener.screen", return_value=[]), \
         patch.object(PaperTrader, "_session_open", return_value=True), \
         patch.object(PaperTrader, "_guard_open_positions",
                      return_value=[]), \
         patch.object(PaperTrader, "_manage_open_positions",
                      return_value=None):
        t.scan_once()
    assert t._eod_squared is False
