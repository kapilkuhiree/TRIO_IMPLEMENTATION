
import pytest
from unittest.mock import MagicMock, patch
from src.broker.megabull import MegaBullBroker
from src.broker.base import BrokerOrder
import time

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
    assert "X" in b.positions # Mirror should now be seeded
