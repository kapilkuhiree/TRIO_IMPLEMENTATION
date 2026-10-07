"""
TRIO — Paper Broker Tests
Author: Kapil Kuhire <kapilkuhire89@gmail.com>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.broker.paper import PaperBroker


def test_place_fill_and_balance():
    broker = PaperBroker(initial_capital=10000)
    order = broker.place_order("RELIANCE", "BUY", 10, price=100.0)
    assert order.status == "FILLED"
    bal = broker.get_balance()
    assert bal["available"] < 10000
    assert len(broker.get_positions()) == 1


def test_insufficient_capital():
    broker = PaperBroker(initial_capital=100)
    order = broker.place_order("RELIANCE", "BUY", 100, price=100.0)
    assert order.status == "REJECTED"
    assert order.error == "Insufficient capital"


def test_sell_closes_position():
    broker = PaperBroker(initial_capital=10000)
    broker.place_order("RELIANCE", "BUY", 10, price=100.0)
    order = broker.place_order("RELIANCE", "SELL", 10, price=110.0)
    assert order.status == "FILLED"
    pos = broker.get_positions()
    assert len(pos) == 0
    bal = broker.get_balance()
    assert bal["available"] > 10000  # profit


def test_cancel_order():
    broker = PaperBroker(initial_capital=10000)
    order = broker.place_order("RELIANCE", "BUY", 10, price=100.0)
    cancel = broker.cancel_order(order.order_id)
    assert cancel.status == "CANCELLED"


def test_cancel_not_found():
    broker = PaperBroker(initial_capital=10000)
    cancel = broker.cancel_order("nope")
    assert cancel.status == "NOT_FOUND"


def test_get_order_status():
    broker = PaperBroker(initial_capital=10000)
    order = broker.place_order("RELIANCE", "BUY", 5, price=100.0)
    st = broker.get_order_status(order.order_id)
    assert st.status == "FILLED"
    assert st.order_id == order.order_id


def test_close_position_records_pass_fail():
    broker = PaperBroker(initial_capital=10000)
    broker.place_order("RELIANCE", "BUY", 10, price=100.0)
    rec = broker.close_position("RELIANCE", 110.0, "target-hit")
    assert rec["result"] == "PASS"
    assert rec["pnl"] == 100.0
    assert rec["reason"] == "target-hit"
    assert len(broker.closed_trades) == 1

    broker.place_order("RELIANCE", "BUY", 10, price=100.0)
    rec2 = broker.close_position("RELIANCE", 90.0, "stop-hit")
    assert rec2["result"] == "FAIL"
    assert rec2["pnl"] == -100.0
    assert len(broker.closed_trades) == 2


def test_close_missing_symbol_returns_empty():
    broker = PaperBroker(initial_capital=10000)
    assert broker.close_position("NOTHING", 10.0) == {}
    assert broker.closed_trades == []


# ---------------------------------------------------------------------------
# Regressions from the 2026-10-06 live session
# ---------------------------------------------------------------------------

def test_repeat_sell_adds_to_short_not_deletes():
    """A second SELL on a SHORT used to silently delete the position."""
    broker = PaperBroker(initial_capital=100000)
    broker.place_order("ONGC.NS", "SELL", 90, price=221.84)
    broker.place_order("ONGC.NS", "SELL", 90, price=221.90)
    pos = broker.get_positions()
    assert len(pos) == 1
    assert pos[0].side == "SHORT"
    assert pos[0].quantity == 180
    # weighted average entry
    assert abs(pos[0].avg_price - (221.84 + 221.90) / 2) < 0.01
    assert broker.closed_trades == []  # nothing was closed


def test_short_balance_never_phantom():
    """45 ONGC @222 on a 10k account must never report lakhs of equity."""
    broker = PaperBroker(initial_capital=10000)
    order = broker.place_order("ONGC.NS", "SELL", 45, price=222.0)
    assert order.status == "FILLED"  # 45*222 = 9990, inside the margin pool
    bal = broker.get_balance()
    # Equity stays within one order of capital — the old bug reported ~8.5L.
    assert 9000 < bal["total"] < 11000  # equity == capital + unrealized (~0)
    assert abs(bal["used_margin"] - 45 * 222.0) < 1
    assert bal["available"] < 11000

    # beyond the margin pool must be rejected, not silently credited
    too_big = broker.place_order("HDFCBANK.NS", "SELL", 28, price=709.05)
    assert too_big.status == "REJECTED"
    assert len(broker.get_positions()) == 1


def test_buy_covers_short_and_records():
    broker = PaperBroker(initial_capital=100000)
    broker.place_order("HDFCBANK.NS", "SELL", 28, price=709.0)
    rec = broker.close_position("HDFCBANK.NS", 699.0, "target-hit")
    assert rec["result"] == "PASS"
    assert rec["side"] == "SHORT"
    assert rec["pnl"] == 28 * 10.0
    assert broker.get_positions() == []
    # cash ends up higher by the profit
    assert abs(broker.get_balance()["total"] - (100000 + 280)) < 1


def test_short_loss_reduces_equity():
    broker = PaperBroker(initial_capital=100000)
    broker.place_order("HDFCBANK.NS", "SELL", 28, price=709.0)
    rec = broker.close_position("HDFCBANK.NS", 716.44, "stop-hit")
    assert rec["result"] == "FAIL"
    assert rec["pnl"] < 0
    assert broker.get_balance()["total"] < 100000


def test_close_record_carries_entry_time_and_holding():
    """Exit alerts need entry/exit timestamps + holding duration, so the
    record must carry entry_time (earliest fill), closed_at and the
    whole-minute delta between them."""
    from unittest.mock import patch
    broker = PaperBroker(initial_capital=100000)
    with patch("src.broker.paper.utc_now",
               return_value="2026-10-07T04:05:00+00:00"):
        broker.place_order("HDFCBANK.NS", "SELL", 28, price=709.0)
    with patch("src.broker.paper.utc_now",
               return_value="2026-10-07T06:35:00+00:00"):
        rec = broker.close_position("HDFCBANK.NS", 699.0, "target-hit")
    assert rec["entry_time"] == "2026-10-07T04:05:00+00:00"
    assert rec["closed_at"] == "2026-10-07T06:35:00+00:00"
    assert rec["holding_minutes"] == 150
