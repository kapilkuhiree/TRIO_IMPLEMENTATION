
import pytest
from unittest.mock import MagicMock, patch
from src.broker.megabull import MegaBullBroker
from src.broker.base import BrokerOrder, Position
import time

def test_megabull_refresh_resolves_option_symbol_from_instrument_name(tmp_path):
    """A position row with no tradingSymbol must still resolve to a tradable symbol.

    Proved live 2026-10-12: /api/position/my returned only the descriptive
    instrumentName ("NIFTY 13 OCT 22500 CE") for an option leg, so the mirror
    key was unresolvable and the exit failed with "No MegaBull instrument
    for ...". The instrument cache CSV carries both columns, so we map it.
    """
    cache = tmp_path / "instruments.csv"
    cache.write_text(
        "instrumentToken,tradingSymbol,instrumentName\n"
        "11420674,NIFTY26O1322500CE,NIFTY 13 OCT 22500 CE\n",
        encoding="utf-8",
    )
    b = MegaBullBroker(api_key="fake", instrument_cache=cache)
    with patch.object(b, "_get", return_value={"value": [{
        "instrumentToken": "11420674",
        "instrumentName": "NIFTY 13 OCT 22500 CE",
        "qty": 65, "priceAvg": 98.4, "type": "BUY",
    }]}):
        b.refresh_positions()
    assert "NIFTY26O1322500CE.NS" in b.positions
    assert b.positions["NIFTY26O1322500CE.NS"].quantity == 65


def test_megabull_refresh_reports_network_failure_as_unhealthy():
    """A stale remote mirror must not look like a successful flat refresh."""
    b = MegaBullBroker(api_key="fake")
    b.positions["RELIANCE.NS"] = Position(
        symbol="RELIANCE.NS", side="LONG", quantity=2,
        avg_price=100.0, current_price=100.0)
    with patch.object(b, "_get", side_effect=TimeoutError("network down")):
        assert b.refresh_positions() is False
    assert "RELIANCE.NS" in b.positions


def test_megabull_refresh_accepts_explicit_empty_position_list_as_flat():
    """A recognized empty list is valid evidence of a flat remote book."""
    b = MegaBullBroker(api_key="fake")
    with patch.object(b, "_get", return_value=[]):
        assert b.refresh_positions() is True
    assert b.positions == {}


def test_megabull_close_position_matches_canonical_key(tmp_path):
    """close_position must find the position when caller omits the .NS suffix."""
    b = MegaBullBroker(api_key="fake")
    b.positions["NIFTY26O1322500CE.NS"] = Position(
        symbol="NIFTY26O1322500CE.NS", side="LONG", quantity=65,
        avg_price=98.4, current_price=98.4)
    with patch.object(b, "refresh_positions"), \
         patch.object(b, "place_order") as po:
        po.return_value = BrokerOrder(order_id="x1", symbol="s", side="SELL",
                                      quantity=65, order_type="MKT",
                                      price=98.4, status="FILLED",
                                      broker="megabull")
        with patch.object(b, "_exit_confirmed", return_value=True):
            rec = b.close_position("NIFTY26O1322500CE", 98.4, "t", 1.0)
    assert rec and rec["quantity"] == 65


def test_options_leg_quantity_is_lot_multiplied():
    """open_spread must send lots*lot_size units, not bare lots.

    Proved live 2026-10-12: NIFTY 13-Oct-2026 22500 CE REJECTED at qty=1
    (HTTP 400 lot size) and FILLED at qty=65. Passing bare `lots` caused
    every options rejection logged on 2026-10-08.
    """
    from src.broker.megabull_options import MegaBullOptionsBroker
    from src.broker.base import Position

    mega = MagicMock()
    mega.positions = {}
    mega.token_for.return_value = "1"
    placed = []

    def _fake_place_order(symbol, side, qty, **kw):
        placed.append(qty)
        mega.positions[symbol] = Position(symbol=symbol, side="LONG",
                                           quantity=qty, avg_price=50.0,
                                           current_price=50.0)
        return BrokerOrder(order_id=f"o{len(placed)}", symbol=symbol, side=side,
                           quantity=qty, order_type="MKT", price=50.0,
                           status="FILLED", broker="megabull")

    mega.place_order.side_effect = _fake_place_order
    ob = MegaBullOptionsBroker(mega, lot_size=65)
    cand = {
        "strategy": "bull-call-spread", "underlying": "NIFTY",
        "expiry": "13-Oct-2026", "net_debit": 40.0,
        "legs": [
            {"side": "BUY", "strike": 22500, "kind": "CE", "premium": 90.0},
            {"side": "SELL", "strike": 22600, "kind": "CE", "premium": 50.0},
        ],
    }
    res = ob.open_spread(cand, lots=1)
    assert res.status == "FILLED"
    assert placed == [65, 65]   # 1 lot -> 65 units per leg, NOT 1


def test_megabull_order_pending_lifecycle():
    """Verify that a LIMIT order stays PENDING if not confirmed immediately."""
    b = MegaBullBroker(api_key="fake")
    # Mock _post to return an order ID but no fill confirmation
    with patch.object(b, "token_for", return_value="123"), \
         patch.object(b, "_post", return_value={"id": "12345"}), \
         patch.object(b, "_fill_confirmed", return_value=False), \
         patch.object(b, "_order_row", return_value={"status": "ACCEPTED"}):
        
        order = b.place_order("X", "BUY", 10, price=100.0, order_type="LIMIT")
        
        assert order.status == "PENDING"
        assert order.order_id in b._pending_orders
        assert "X" not in b.positions # Mirror should not be seeded yet

def test_megabull_order_unknown_on_missing_row():
    """Verify that an order not visible in the remote book is marked UNKNOWN."""
    b = MegaBullBroker(api_key="fake")
    # Mock _post, but _order_row returns None (not visible)
    with patch.object(b, "token_for", return_value="123"), \
         patch.object(b, "_post", return_value={"id": "67890"}), \
         patch.object(b, "_fill_confirmed", return_value=False), \
         patch.object(b, "_order_row", return_value=None):
        
        order = b.place_order("X", "BUY", 10, price=100.0, order_type="LIMIT")
        
        # Based on new logic, this becomes UNKNOWN
        assert order.status == "UNKNOWN"
        assert order.order_id in b._pending_orders

def test_megabull_poll_pending_transitions_to_filled():
    """Verify poll_pending transitions PENDING to FILLED upon confirmation."""
    b = MegaBullBroker(api_key="fake")
    # Initial PENDING
    with patch.object(b, "token_for", return_value="123"), \
         patch.object(b, "_post", return_value={"id": "12345"}), \
         patch.object(b, "_fill_confirmed", return_value=False), \
         patch.object(b, "_order_row", return_value={"status": "ACCEPTED"}):
        order = b.place_order("X", "BUY", 10, price=100.0)
        assert order.status == "PENDING"

    # Now poll_pending sees it as COMPLETE
    with patch.object(b, "_order_row", return_value={"status": "COMPLETE"}):
        b.poll_pending()
        
    assert order.status == "FILLED"
    # Seeded under the CANONICAL "<SYM>.NS" mirror key so it matches the key
    # refresh_positions() rebuilds from the exchange book. Proved live
    # 2026-10-12: a seed under "NIFTY26O1322500CE" and a refresh under
    # "NIFTY26O1322500CE.NS" looked like two different positions.
    assert "X.NS" in b.positions
