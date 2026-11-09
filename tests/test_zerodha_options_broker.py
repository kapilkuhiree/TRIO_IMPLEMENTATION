"""Offline tests for the strict live Zerodha NFO options adapter.

The fake Kite client keeps a tiny net-position book so fill, partial-fill and
flatten behaviour can be asserted without any network access.
"""

from datetime import date
import pytest

from src.broker.zerodha_options import ZerodhaOptionsBroker


SYMBOL = "NIFTY26N0924550CE"


def _rows():
    return [{
        "tradingsymbol": SYMBOL,
        "instrument_token": 10241282,
        "name": "NIFTY",
        "expiry": date(2026, 11, 9),
        "strike": 24550.0,
        "lot_size": 65,
        "instrument_type": "CE",
        "tick_size": 0.05,
        "segment": "NFO-OPT",
        "exchange": "NFO",
    }]


class FakeKite:
    def __init__(self):
        self.orders_db = {}
        self.net = {}
        self.placed = []
        self.cancelled = []
        self.next_id = 1
        self.fill_plan = []

    def place_order(self, **params):
        order_id = f"o{self.next_id}"
        self.next_id += 1
        self.placed.append(dict(params))
        spec = self.fill_plan.pop(0) if self.fill_plan else (
            "COMPLETE", params["quantity"], params.get("price") or 100.0)
        status, filled, average = spec
        row = {
            "order_id": order_id,
            "tradingsymbol": params["tradingsymbol"],
            "transaction_type": params["transaction_type"],
            "status": status,
            "filled_quantity": filled,
            "average_price": average,
            "quantity": params["quantity"],
            "price": params.get("price") or 0.0,
            "status_message": "",
        }
        self.orders_db[order_id] = [row]
        self._apply_fill(params, filled, average)
        return order_id

    def _apply_fill(self, params, filled, average):
        if filled <= 0:
            return
        symbol = params["tradingsymbol"]
        signed = filled if params["transaction_type"] == "BUY" else -filled
        book = self.net.setdefault(symbol, {"quantity": 0, "avg": 0.0})
        before = book["quantity"]
        after = before + signed
        if signed > 0 and after > 0:
            book["avg"] = ((book["avg"] * max(before, 0)) +
                           average * signed) / after
        book["quantity"] = after
        if after == 0:
            book["avg"] = 0.0

    def order_history(self, order_id):
        return list(self.orders_db.get(order_id, []))

    def orders(self):
        return [history[-1] for history in self.orders_db.values()]

    def cancel_order(self, variety, order_id):
        self.cancelled.append(order_id)
        row = self.orders_db[order_id][-1]
        if row["status"] in {"OPEN", "PENDING", "TRIGGER PENDING"}:
            row["status"] = "CANCELLED"

    def positions(self):
        net = [{
            "tradingsymbol": symbol,
            "exchange": "NFO",
            "quantity": book["quantity"],
            "average_price": book["avg"],
            "last_price": book["avg"],
            "pnl": 0.0,
        } for symbol, book in self.net.items()]
        # Non-NFO holdings must be ignored by the strict adapter.
        net.append({"tradingsymbol": "RELIANCE", "exchange": "NSE",
                    "quantity": 10, "average_price": 100.0,
                    "last_price": 101.0, "pnl": 10.0})
        return {"net": net}

    def margins(self):
        return {"equity": {"net": 40000.0,
                           "available": {"cash": 40000.0, "live_balance": 40000.0},
                           "utilised": {"debits": 0.0}}}


class FakeMarketData:
    def __init__(self):
        self.kite = FakeKite()

    def instruments(self, exchange):
        assert exchange == "NFO"
        return _rows()

    def option_quote(self, instrument):
        return {"bid": 99.0, "ask": 100.0, "last_price": 100.0,
                "open_interest": 5000, "volume": 1000,
                "timestamp": "2026-11-06T16:00:00+05:30"}


def _broker(kite=None, **kwargs):
    market = FakeMarketData()
    if kite is not None:
        market.kite = kite
    return ZerodhaOptionsBroker(market, poll_seconds=0.0, **kwargs)


def _payload(ask=100.0):
    return {
        "strategy": "long-call",
        "underlying": "NIFTY",
        "option_underlying": "NIFTY",
        "expiry": "09-Nov-2026",
        "lot_size": 65,
        "legs": [{"side": "BUY", "kind": "CE", "strike": 24550.0,
                  "ask": ask, "premium": ask, "premium_src": "live"}],
    }


def test_instrument_rows_are_normalised_for_runtime_validation():
    broker = _broker()
    row = broker.instrument_for(SYMBOL)
    assert row["exchange"] == "NSE"
    assert "OPT" in row["instrumentType"]
    assert row["lot_size"] == 65
    assert row["instrument_token"] == 10241282
    assert [item["tradingsymbol"] for item in broker.option_instruments()] == [SYMBOL]


def test_entry_full_fill_creates_spread_and_mirror():
    broker = _broker()
    order = broker.open_spread(_payload(), lots=1, reason="test")
    assert order.status == "FILLED"
    assert order.quantity == 65
    assert order.price == 102.0  # ask 100 + 2% slippage, tick-rounded
    assert SYMBOL in broker.spreads
    assert broker.spreads[SYMBOL]["net_debit"] == 102.0
    assert broker.spreads[SYMBOL]["legs"][0]["units"] == 65
    assert broker._symbol_quantity(SYMBOL) == 65
    assert broker.kite.placed[0]["exchange"] == "NFO"
    assert broker.kite.placed[0]["product"] == "NRML"
    assert broker.kite.placed[0]["transaction_type"] == "BUY"


def test_entry_partial_fill_is_cancelled_and_unwound():
    kite = FakeKite()
    kite.fill_plan = [("CANCELLED", 30, 100.0)]  # entry fills 30/65
    broker = _broker(kite)
    order = broker.open_spread(_payload(), lots=1, reason="test")
    assert order.status == "REJECTED"
    assert "filled 30/65" in order.error
    assert SYMBOL not in broker.spreads
    assert broker._symbol_quantity(SYMBOL) == 0
    assert any(row["transaction_type"] == "SELL" for row in kite.placed)
    sell = next(row for row in kite.placed if row["transaction_type"] == "SELL")
    assert sell["quantity"] == 30
    assert sell["exchange"] == "NFO"


def test_entry_rejection_creates_no_book():
    kite = FakeKite()
    kite.fill_plan = [("REJECTED", 0, 0.0)]
    broker = _broker(kite)
    order = broker.open_spread(_payload(), lots=1, reason="test")
    assert order.status == "REJECTED"
    assert not broker.spreads
    assert broker._symbol_quantity(SYMBOL) == 0
    assert not any(row["transaction_type"] == "SELL" for row in kite.placed)


def test_dry_run_sends_no_order():
    broker = _broker(dry_run=True)
    order = broker.open_spread(_payload(), lots=1, reason="test")
    assert order.status == "REJECTED"
    assert "dry-run" in order.error
    assert broker.kite.placed == []


def test_close_books_only_after_a_full_sell():
    broker = _broker()
    broker.open_spread(_payload(), lots=1, reason="test")
    record = broker.close_spread(SYMBOL, 110.0, "target")
    assert record["exit"] == 110.0
    assert record["pnl"] > 0
    assert SYMBOL not in broker.spreads
    assert SYMBOL not in broker.positions
    assert len(broker.closed_trades) == 1
    sell = [row for row in broker.kite.placed
            if row["transaction_type"] == "SELL"]
    assert sell and sell[0]["quantity"] == 65


def test_close_partial_sell_escalates_only_the_remainder():
    kite = FakeKite()
    broker = _broker(kite)
    broker.open_spread(_payload(), lots=1, reason="test")
    kite.fill_plan = [("CANCELLED", 20, 110.0)]  # 20/65 sold
    record = broker.close_spread(SYMBOL, 110.0, "target")
    assert record
    sells = [row for row in kite.placed if row["transaction_type"] == "SELL"]
    assert sells[0]["quantity"] == 65
    assert sells[-1]["order_type"] == "MARKET"
    assert sells[-1]["quantity"] == 45
    assert broker._symbol_quantity(SYMBOL) == 0


def test_flatten_all_cancels_and_closes_every_holding():
    kite = FakeKite()
    broker = _broker(kite)
    broker.open_spread(_payload(), lots=1, reason="test")
    kite.orders_db["resting"] = [{
        "order_id": "resting", "status": "OPEN", "tradingsymbol": SYMBOL,
        "exchange": "NFO", "transaction_type": "BUY", "filled_quantity": 0,
        "average_price": 0.0, "quantity": 65, "price": 1.0,
        "status_message": "",
    }]
    # An unrelated equity order must survive the NFO-only flatten.
    kite.orders_db["equity"] = [{
        "order_id": "equity", "status": "OPEN", "tradingsymbol": "RELIANCE",
        "exchange": "NSE", "transaction_type": "BUY", "filled_quantity": 0,
        "average_price": 0.0, "quantity": 1, "price": 100.0,
        "status_message": "",
    }]
    result = broker.flatten_all("test")
    assert result["remaining"] == []
    assert result["closed"][SYMBOL] is True
    assert "resting" in result["cancelled"]
    assert "equity" not in result["cancelled"]
    assert result["positions_ok"] is True


def test_flatten_never_claims_flat_when_positions_are_unreadable():
    kite = FakeKite()
    broker = _broker(kite)
    broker.open_spread(_payload(), lots=1, reason="test")

    def unavailable():
        raise RuntimeError("Kite positions endpoint is down")

    kite.positions = unavailable
    result = broker.flatten_all("test")
    assert result["positions_ok"] is False
    assert SYMBOL in result["remaining"]


def test_balance_reads_equity_available():
    broker = _broker()
    assert broker.get_balance()["available"] == pytest.approx(40000.0)
