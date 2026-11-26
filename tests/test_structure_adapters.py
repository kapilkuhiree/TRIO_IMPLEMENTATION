"""Long straddle / long strangle adapter tests (offline, no HTTP).

Covers the two option broker adapters' two-leg BUY-only structures while
pinning the legacy single-leg strict path.
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.broker.base import Position
from src.broker.megabull_options import MegaBullOptionsBroker
from src.broker.options_paper import OptionsPaperBroker

CE_SYMBOL = "NIFTY26N2525000CE"
PE_SYMBOL = "NIFTY26N2525000PE"
EXPIRY = "25-Nov-2026"
LOT_SIZE = 75


class FakeOrder:
    """Minimal remote child-order print."""

    def __init__(self, order_id, symbol, side, quantity,
                 status="FILLED", price=0.0, error=""):
        self.order_id = order_id
        self.symbol = symbol
        self.side = side
        self.quantity = quantity
        self.status = status
        self.price = price
        self.error = error


class FakeMegaBroker:
    """Remote paper book with the adapter's instrument/order surface."""

    provider_name = "megabull"

    def __init__(self, lot_size=LOT_SIZE, fail_symbol=None):
        self.lot_size = lot_size
        self.fail_symbol = fail_symbol
        self.instruments = {}
        self.positions = {}
        self.orders = {}
        self.place_calls = []
        self.close_calls = []
        self.seq = 0

    def add_instrument(self, symbol, token, kind):
        row = {
            "tradingSymbol": symbol,
            "instrumentToken": str(token),
            "instrumentName": f"NIFTY {EXPIRY} {kind}",
            "instrumentType": "OPTIDX",
            "exchange": "NSE",
            "lotSize": str(self.lot_size),
            "expiry": EXPIRY,
            "strike": "25000",
            "optionType": kind,
        }
        self.instruments[symbol] = row
        return row

    def instrument_for(self, symbol):
        return self.instruments.get(str(symbol))

    def token_for(self, symbol):
        row = self.instrument_for(symbol)
        if row is None:
            raise KeyError(symbol)
        return row["instrumentToken"]

    def place_order(self, symbol, side, quantity, order_type="MKT",
                    price=None, reason="order", **kwargs):
        self.place_calls.append({"symbol": symbol, "side": side,
                                 "quantity": quantity, "price": price})
        if symbol == self.fail_symbol:
            return FakeOrder("rejected", symbol, side, quantity,
                             status="REJECTED", error="sim refused")
        self.seq += 1
        order = FakeOrder(f"remote-{self.seq}", symbol, side, quantity,
                          price=price or 0.0)
        self.orders[order.order_id] = order
        existing = self.positions.get(symbol)
        if existing is not None and existing.side == (
                "LONG" if side == "BUY" else "SHORT"):
            total = existing.quantity + quantity
            avg = ((existing.avg_price * existing.quantity
                    + (price or 0.0) * quantity) / total)
            self.positions[symbol] = Position(
                symbol=symbol, side=existing.side, quantity=total,
                avg_price=avg, current_price=price or 0.0)
        else:
            self.positions[symbol] = Position(
                symbol=symbol, side="LONG" if side == "BUY" else "SHORT",
                quantity=quantity, avg_price=price or 0.0,
                current_price=price or 0.0)
        return order

    def close_position(self, symbol, price, reason="close", fraction=1.0):
        self.close_calls.append({"symbol": symbol, "price": price,
                                 "reason": reason, "fraction": fraction})
        pos = self.positions.get(symbol)
        if pos is None or pos.quantity <= 0:
            return {}
        qty = max(1, int(pos.quantity * float(fraction)))
        remaining = pos.quantity - qty
        if remaining > 0:
            self.positions[symbol] = Position(
                symbol=symbol, side=pos.side, quantity=remaining,
                avg_price=pos.avg_price, current_price=pos.current_price)
        else:
            self.positions.pop(symbol, None)
        return {"symbol": symbol, "qty": qty, "exit": price}


def _contract(symbol, token, kind):
    return {
        "tradingSymbol": symbol,
        "instrumentToken": str(token),
        "instrumentName": f"NIFTY {EXPIRY} {kind}",
        "instrumentType": "OPTIDX",
        "exchange": "NSE",
        "lotSize": str(LOT_SIZE),
        "expiry": EXPIRY,
        "strike": "25000",
        "optionType": kind,
    }


def _structure_payload(strategy="long_straddle"):
    ce_contract = _contract(CE_SYMBOL, "11000001", "CE")
    pe_contract = _contract(PE_SYMBOL, "11000002", "PE")
    legs = [
        {"side": "BUY", "kind": "CE", "strike": 25000.0,
         "premium": 120.0, "bid": 118.0, "ask": 122.0,
         "trading_symbol": CE_SYMBOL, "validated_contract": ce_contract},
        {"side": "BUY", "kind": "PE", "strike": 25000.0,
         "premium": 100.0, "bid": 98.0, "ask": 102.0,
         "trading_symbol": PE_SYMBOL, "validated_contract": pe_contract},
    ]
    return {
        "strategy": strategy,
        "underlying": "NIFTY",
        "expiry": EXPIRY,
        "lot_size": LOT_SIZE,
        "trading_symbol": CE_SYMBOL,  # top-level is the CE leg
        "validated_contract": ce_contract,
        "legs": legs,
        "net_debit": 220.0,  # 120 CE + 100 PE
        "maxLoss": 220.0 * LOT_SIZE,
        "maxGain": 0.0,
        "breakeven": 0.0,
        "strict_options_only": True,
    }


def _mega_for(payload):
    mega = FakeMegaBroker()
    for leg in payload["legs"]:
        contract = leg.get("validated_contract") or payload["validated_contract"]
        mega.add_instrument(
            leg.get("trading_symbol") or payload["trading_symbol"],
            contract["instrumentToken"], leg["kind"])
    return mega


def test_options_paper_opens_structure_with_combined_debit_and_no_short_leg(
        monkeypatch):
    from src.broker import options_paper as op

    # Pin the legacy mid model so the booked debit is the candidate's
    # combined net_debit rather than executable ask repricing.
    monkeypatch.setattr(op, "load_config",
                        lambda *a, **k: {"options": {"fill_model": "mid"}})
    payload = _structure_payload("long_strangle")  # underscore spelling
    broker = OptionsPaperBroker(initial_capital=500000.0, lot_size=LOT_SIZE)

    order = broker.open_spread(payload, lots=2)

    assert order.status == "FILLED"
    sid = order.symbol
    pos = broker.positions[sid]
    assert pos.avg_price == 220.0
    assert pos.quantity == 2
    assert broker.capital == 500000.0 - 220.0 * 2 * LOT_SIZE

    legs = broker.spreads[sid]["legs"]
    assert len(legs) == 2
    assert {leg["kind"] for leg in legs} == {"CE", "PE"}
    assert all(leg["side"] == "BUY" for leg in legs)
    assert not [leg for leg in legs if leg["side"] == "SELL"]

    with patch("src.alerts.send_exit_alert"):
        rec = broker.close_spread(sid, 250.0, "test-close")
    assert rec["entry"] == 220.0
    assert rec["gross_pnl"] == (250.0 - 220.0) * 2 * LOT_SIZE


def test_options_paper_bidask_fill_sums_both_buy_asks(monkeypatch):
    from src.broker import options_paper as op

    cfg = {"options": {"fill_model": "bidask", "slippage_ticks": 0.5}}
    monkeypatch.setattr(op, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr("src.options_pricing.load_config",
                        lambda *a, **k: cfg)
    broker = OptionsPaperBroker(initial_capital=500000.0, lot_size=LOT_SIZE)

    order = broker.open_spread(_structure_payload(), lots=1)

    # Each BUY fills at ask + 0.5 tick (0.025): 122.03 + 102.03.
    assert abs(broker.positions[order.symbol].avg_price - 224.06) < 0.01


def test_megabull_strict_opens_both_structure_legs_and_closes_both():
    payload = _structure_payload("long-straddle")  # hyphen spelling
    mega = _mega_for(payload)
    broker = MegaBullOptionsBroker(mega, lot_size=LOT_SIZE)
    broker.strict_options_only = True

    order = broker.open_spread(payload, lots=2)

    sid = f"OPT_{CE_SYMBOL}_{PE_SYMBOL}"
    assert order.status == "FILLED"
    assert order.symbol == sid
    assert [call["symbol"] for call in mega.place_calls] == [
        CE_SYMBOL, PE_SYMBOL]
    assert all(call["quantity"] == 2 * LOT_SIZE
               for call in mega.place_calls)
    remote_legs = broker.spreads[sid]["remote_legs"]
    assert len(remote_legs) == 2
    assert {leg["mega_symbol"] for leg in remote_legs} == {
        CE_SYMBOL, PE_SYMBOL}

    with patch("src.alerts.send_exit_alert"):
        rec = broker.close_spread(sid, 260.0, "test-close")
    assert rec["result"] == "PASS"
    assert rec["strategy"] == "long-straddle"
    assert not mega.positions
    assert sorted(call["symbol"] for call in mega.close_calls) == sorted(
        [CE_SYMBOL, PE_SYMBOL])


def test_megabull_shared_symbol_close_only_own_units():
    """Two trades may hold one symbol; each close must sell only its own units."""
    payload = _structure_payload("long_straddle")
    mega = _mega_for(payload)
    broker = MegaBullOptionsBroker(mega, lot_size=LOT_SIZE)
    broker.strict_options_only = True
    ce_contract = payload["validated_contract"]

    with patch("src.alerts.send_exit_alert"):
        broker.open_spread(payload, lots=1)
        single = {
            "strategy": "long-call",
            "underlying": "NIFTY",
            "expiry": EXPIRY,
            "lot_size": LOT_SIZE,
            "trading_symbol": CE_SYMBOL,
            "validated_contract": ce_contract,
            "legs": [{
                "side": "BUY", "kind": "CE", "strike": 25000.0,
                "premium": 120.0, "trading_symbol": CE_SYMBOL,
                "validated_contract": ce_contract,
            }],
            "net_debit": 120.0,
            "strict_options_only": True,
        }
        assert broker.open_spread(single, lots=1).status == "FILLED"
        assert mega.positions[CE_SYMBOL].quantity == 2 * LOT_SIZE

        # The single CE trade closes first.  Only its 75 units may be sold;
        # the straddle's CE units must remain in the remote book.
        broker.close_spread(f"OPT_{CE_SYMBOL}", 140.0, "single-close")
        assert mega.positions[CE_SYMBOL].quantity == LOT_SIZE
        assert PE_SYMBOL in mega.positions

        sid = f"OPT_{CE_SYMBOL}_{PE_SYMBOL}"
        broker.close_spread(sid, 250.0, "structure-close")

    assert not mega.positions
    fractions = [call["fraction"] for call in mega.close_calls
                 if call["symbol"] == CE_SYMBOL]
    assert fractions == [0.5, 1.0]


def test_megabull_strict_structure_ce_may_fall_back_to_top_level():
    payload = _structure_payload()
    ce_leg = payload["legs"][0]
    ce_leg.pop("trading_symbol")
    ce_leg.pop("validated_contract")
    mega = FakeMegaBroker()
    mega.add_instrument(CE_SYMBOL, "11000001", "CE")
    pe_leg = payload["legs"][1]
    mega.add_instrument(PE_SYMBOL,
                        pe_leg["validated_contract"]["instrumentToken"], "PE")
    broker = MegaBullOptionsBroker(mega, lot_size=LOT_SIZE)
    broker.strict_options_only = True

    order = broker.open_spread(payload, lots=1)

    assert order.status == "FILLED"
    assert order.symbol == f"OPT_{CE_SYMBOL}_{PE_SYMBOL}"
    assert [call["symbol"] for call in mega.place_calls] == [
        CE_SYMBOL, PE_SYMBOL]


def test_megabull_strict_structure_unwinds_confirmed_leg_on_failure():
    payload = _structure_payload()
    mega = _mega_for(payload)
    mega.fail_symbol = PE_SYMBOL  # second leg fails after CE confirmed
    broker = MegaBullOptionsBroker(mega, lot_size=LOT_SIZE)
    broker.strict_options_only = True

    order = broker.open_spread(payload, lots=1)

    assert order.status == "REJECTED"
    assert not broker.positions
    assert not mega.positions
    assert [call["symbol"] for call in mega.close_calls] == [CE_SYMBOL]


def test_megabull_strict_structure_requires_per_leg_contract_metadata():
    payload = _structure_payload()
    payload["legs"][1].pop("validated_contract")  # PE has no fallback
    mega = _mega_for(payload)
    broker = MegaBullOptionsBroker(mega, lot_size=LOT_SIZE)
    broker.strict_options_only = True

    order = broker.open_spread(payload, lots=1)

    assert order.status == "REJECTED"
    assert mega.place_calls == []


def test_megabull_strict_single_leg_still_uses_top_level_symbol():
    contract = _contract(CE_SYMBOL, "11000001", "CE")
    payload = {
        "strategy": "long-call",
        "underlying": "NIFTY",
        "expiry": EXPIRY,
        "lot_size": LOT_SIZE,
        "trading_symbol": CE_SYMBOL,
        "validated_contract": contract,
        "net_debit": 120.0,
        "strict_options_only": True,
        "legs": [{"side": "BUY", "kind": "CE", "strike": 25000.0,
                  "premium": 120.0, "bid": 118.0, "ask": 122.0}],
    }
    mega = FakeMegaBroker()
    mega.add_instrument(CE_SYMBOL, "11000001", "CE")
    broker = MegaBullOptionsBroker(mega, lot_size=LOT_SIZE)
    broker.strict_options_only = True

    order = broker.open_spread(payload, lots=1)

    assert order.status == "FILLED"
    assert order.symbol == f"OPT_{CE_SYMBOL}"
    assert [call["symbol"] for call in mega.place_calls] == [CE_SYMBOL]
    assert mega.place_calls[0]["quantity"] == LOT_SIZE
    sid = order.symbol
    with patch("src.alerts.send_exit_alert"):
        rec = broker.close_spread(sid, 130.0, "test-close")
    assert rec["gross_pnl"] == 10.0 * LOT_SIZE
    assert not mega.positions
