"""
TRIO — MegaBull adapter tests (all HTTP mocked: no network, no key).
"""

import csv
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.broker.megabull import MegaBullBroker


class _Resp:
    def __init__(self, payload=None, status=200, text=""):
        self._payload = payload
        self.status_code = status
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Records requests, replays canned MegaBull responses."""

    def __init__(self):
        self.posts = []
        self.position_items = []
        self.open_orders = []   # PENDING remote orders
        self.executed_orders = []
        self.user = {"firstName": "Test", "virtualMoney": 500000.0,
                     "virtualMoneyBlocked": 0.0, "virtualMoneyLeft": 500000}
        self.next_id = 101

    def get(self, url, **kwargs):
        if url.endswith("/api/user/my"):
            return _Resp(self.user)
        if url.endswith("/api/position/my"):
            return _Resp({"value": list(self.position_items), "Count": len(self.position_items)})
        if url.endswith("/api/order/my"):
            return _Resp({"open": list(self.open_orders),
                          "executed": list(self.executed_orders)})
        if url.endswith("/api/marketwatch/instruments"):
            return _Resp({"downloadUrl": "http://x/instruments.csv"})
        if url.endswith("instruments.csv"):
            text = ('"tradingSymbol","instrumentName","instrumentToken"\n'
                    '"ONGC","Oil & Natural Gas Corporation Limited","633601"\n'
                    '"M&M","Mahindra & Mahindra Ltd","519937"\n'
                    '"HDFCBANK","HDFC Bank Ltd","341249"\n')
            return _Resp(None, text=text)
        return _Resp({}, status=404)

    _NAMES = {"633601": "Oil & Natural Gas Corporation Limited",
                "519937": "Mahindra & Mahindra Ltd",
                "341249": "HDFC Bank Ltd"}

    def _net(self, tok, side, qty, price):
        # Mirror the production API: instrumentName is the company name
        # (NOT the tradable symbol); refresh_positions re-derives the
        # symbol from tradingSymbol via the token map.
        name = {"633601": "Oil & Natural Gas Corporation Limited",
                "519937": "Mahindra & Mahindra Ltd",
                "341249": "HDFC Bank Ltd"}.get(tok, tok)
        try:
            price = float(price or 220.0)
        except (TypeError, ValueError):
            price = 220.0
        for it in self.position_items:
            if str(it.get("instrumentToken")) == tok:
                if side == "BUY":
                    old = it.get("qty", 0)
                    new = old + qty
                    if old < 0 and new >= 0:
                        # covering the short: entry side stays SELL
                        it["qty"] = new
                    else:
                        it["qty"] = new
                        it["type"] = side
                        it["priceAvg"] = price
                else:
                    it["qty"] = it.get("qty", 0) - qty
                break
        else:
            self.position_items.append({
                "instrumentToken": tok, "instrumentName": name,
                "tradingSymbol": {"633601": "ONGC", "519937": "M&M",
                                  "341249": "HDFCBANK"}.get(tok, tok),
                "qty": qty if side == "BUY" else -qty,
                "priceAvg": price,
                "pl": 0.0, "type": side,
                "duration": "MIS", "lotSize": 1,
                "avgBuyPrice": price if side == "BUY" else 0.0,
                "avgSellPrice": price if side == "SELL" else 0.0,
            })
        # drop zeroed legs like a real net-position book
        self.position_items = [it for it in self.position_items
                               if it.get("qty", 0) != 0]

    def post(self, url, **kwargs):
        body = kwargs.get("json")
        if isinstance(body, str):
            try:
                import json as _json
                body = _json.loads(body)
            except Exception:
                body = {}
        # Mirror the production API: only MKT fills on POST; a LIMIT (or
        # SL) POST creates a PENDING order and moves the position only
        # when it later executes — which is exactly the incident we
        # regression-test (exit must stay unconfirmed while PENDING).
        self.posts.append((url, dict(body or {})))
        body = body or {}
        side = body.get("type", "BUY")
        try:
            qty = int(body.get("qty", 0) or 0)
        except (TypeError, ValueError):
            qty = 0
        tok = str(body.get("instrumentToken", ""))
        otype = str(body.get("orderType", "MKT")).upper()
        if qty and otype == "MKT":
            self._net(tok, side, qty, body.get("price"))  # MKT fills now
        self.next_id += 1
        oid = self.next_id
        rec = {"id": oid, "instrumentToken": tok, "qty": qty,
               "type": side, "status": "EXECUTED"}
        if otype == "MKT":
            self.executed_orders.append(rec)  # MKT fills instantly
        else:
            self.open_orders.append(dict(rec, status="PENDING"))
        return _Resp(rec)


def _broker(tmp_path, **kw):
    sess = FakeSession()
    b = MegaBullBroker(
        api_key="test-key",
        initial_capital=500000.0,
        instrument_cache=Path(tmp_path) / "instruments.csv",
        session=sess,
        **kw,
    )
    return b, sess


def test_symbol_mapping_and_token_resolution(tmp_path):
    b, sess = _broker(tmp_path)
    assert b.to_trading_symbol("ONGC.NS") == "ONGC"
    assert b.to_trading_symbol("M&M.NS") == "M&M"
    assert b.token_for("ONGC.NS") == "633601"
    assert b.token_for("HDFCBANK.NS") == "341249"
    assert b.instrument_cache.exists()  # CSV cached for reuse


def test_place_order_body_shape_mis_limit(tmp_path):
    b, sess = _broker(tmp_path)
    order = b.place_order("ONGC.NS", "SELL", 45, price=222.0,
                          stop_loss=224.06, target=218.51)
    # Accepted remotely (verified against live HTTP earlier); the exchange
    # fills it when the limit is touched — therefore the local status is
    # FILLED-on-accept and the position mirror only nets executed legs.
    assert order.status == "FILLED"
    assert order.broker == "megabull"
    assert order.order_id.startswith("megabull_")
    url, body = sess.posts[0]
    assert url.endswith("/api/order/buysell")
    assert body == {"instrumentToken": "633601", "qty": 45, "type": "SELL",
                    "duration": "MIS", "orderType": "LIMIT", "price": 222.0}


def test_limit_without_price_rejected_locally(tmp_path):
    b, sess = _broker(tmp_path)
    order = b.place_order("ONGC.NS", "SELL", 45)
    assert order.status == "REJECTED"
    assert sess.posts == []


def test_mkt_exit_carries_a_price_field(tmp_path):
    """MegaBull quirk (live 2026-10-06): 'Price cannot be Blank' — MKT
    orders also need price, taken from the position's own mark."""
    b, sess = _broker(tmp_path)
    b.place_order("HDFCBANK.NS", "SELL", 28, price=709.0)
    sess._net("341249", "SELL", 28, 709.0)
    b.refresh_positions()
    with patch("src.alerts.send_exit_alert"):
        rec = b.close_position("HDFCBANK.NS", 699.0, "target-hit")
    assert rec["result"] == "PASS"
    exit_posts = [p for u, p in sess.posts if p.get("type") == "BUY"]
    assert exit_posts and "price" in exit_posts[-1]
    assert exit_posts[-1]["price"] == 699.0  # close caller's print


def test_missing_key_rejects_without_network(tmp_path):
    b = MegaBullBroker(api_key="",
                       instrument_cache=Path(tmp_path) / "x.csv")
    order = b.place_order("ONGC.NS", "SELL", 1, price=222.0)
    assert order.status == "REJECTED"
    assert "MEGABULL_API_KEY" in order.error


def test_401_fails_fast_and_pins_local_mirror(tmp_path):
    """A bad/expired key must not stall every scan: first 401 sets the
    flag, subsequent balance/position/order calls short-circuit with
    zero retries and zero sleeps."""
    b, sess = _broker(tmp_path)
    b.api_key = "bad-key"

    class _Bad(_Resp):
        def __init__(self):
            super().__init__(None, status=401)

    class BadSession(FakeSession):
        def get(self, url, **kwargs):
            return _Bad()

        def post(self, url, **kwargs):
            return _Bad()

    b._session = BadSession()
    bal = b.get_balance()  # one attempt, sets the flag
    assert b._auth_failed is True
    posts_before = len(sess.posts)
    import time as _t
    start = _t.monotonic()
    bal2 = b.get_balance()
    elapsed = _t.monotonic() - start
    assert bal2 == {"total": 500000.0, "available": 500000.0,
                    "used_margin": 0.0}
    assert elapsed < 1.0  # no network, no retries, no sleep
    assert len(sess.posts) == posts_before


def test_balance_sums_virtual_money_plus_open_pnl(tmp_path):
    b, sess = _broker(tmp_path)
    b.place_order("ONGC.NS", "SELL", 45, price=222.0)
    # seed the position book directly: a LIMIT entry only nets when the
    # exchange executes it (see test_close_* for the MKT fill path)
    sess._net("633601", "SELL", 45, 222.0)
    sess.position_items[0]["pl"] = -450.0
    bal = b.get_balance()
    assert bal["total"] == 500000.0 - 450.0
    assert bal["available"] == 500000
    assert bal["used_margin"] >= 0


def test_close_records_pass_fail_and_alerts(tmp_path):
    b, sess = _broker(tmp_path)
    b.place_order("HDFCBANK.NS", "SELL", 28, price=709.0)
    sess._net("341249", "SELL", 28, 709.0)  # exchange executed the entry
    b.refresh_positions()
    assert "HDFCBANK.NS" in b.positions, \
        f"entry must be visible before close: {b.positions}"
    with patch("src.alerts.send_exit_alert") as alert:
        rec = b.close_position("HDFCBANK.NS", 699.0, "target-hit")
    assert rec["result"] == "PASS"
    assert rec["side"] == "SHORT"
    assert abs(rec["pnl"] - 28 * 10.0) < 0.01
    assert alert.call_count == 1
    closes = [p for u, p in sess.posts if p.get("type") == "BUY"]
    assert closes and closes[-1]["instrumentToken"] == "341249"
    # exits go MKT so they actually fill (LIMIT sat PENDING live)
    assert closes[-1]["orderType"] == "MKT"
    # MegaBull quirk: MKT also needs the price field (filled at sim price);
    # we send our own print (the stop/target level that fired)
    assert "price" in closes[-1] and closes[-1]["price"] == 699.0
    # exit alerts need the full audit trail on remote closes too
    assert rec["entry_time"] and rec["closed_at"]
    assert isinstance(rec["holding_minutes"], int)


def test_fill_confirmed_by_remote_id_row(tmp_path):
    """Entry confirmation matches the order's own remote id — never
    side+qty (proved live: SELL 1 #771071 and BUY 1 #771070 looked
    identical). A row still PENDING, or absent, is NOT a fill."""
    b, sess = _broker(tmp_path)
    order = b.place_order("ONGC.NS", "SELL", 45, price=222.0)
    assert order.status == "FILLED"  # accepted LIMIT (fills when touched)
    remote = str(order.order_id).split("megabull_")[-1]
    row = b._order_row(remote)
    assert row is not None and row["status"] == "PENDING"
    assert b._fill_confirmed(order) is False
    # the exchange executes it later: row flips COMPLETE
    for rec in sess.open_orders:
        if str(rec["id"]) == remote:
            rec["status"] = "COMPLETE"
            sess.executed_orders.append(rec)
    sess.open_orders = [r for r in sess.open_orders
                        if str(r["id"]) != remote]
    assert b._fill_confirmed(order) is True


def test_close_unconfirmed_when_exit_still_pending(tmp_path):
    """Live lesson 2026-10-06: the offsetting LIMIT sat PENDING while we
    booked the trade as closed. A PENDING exit must keep the position
    open and record nothing. Confirmation is the remote net position
    flattening — not matching order-book rows (ids/legs are ambiguous)."""
    from types import MethodType
    b, sess = _broker(tmp_path)
    b.place_order("HDFCBANK.NS", "SELL", 28, price=709.0)
    sess._net("341249", "SELL", 28, 709.0)  # exchange executed the entry
    b.refresh_positions()
    assert "HDFCBANK.NS" in b.positions
    orig = MethodType(MegaBullBroker.place_order, b)
    with patch("src.alerts.send_exit_alert") as alert, \
         patch.object(b, "place_order",
                      side_effect=lambda *a, **k:
                      orig(*a, **{**k, "order_type": "LIMIT"})), \
         patch("src.broker.megabull.sleep", return_value=None):
        rec = b.close_position("HDFCBANK.NS", 699.0, "target-hit")
    assert rec == {}
    assert b.closed_trades == []
    assert alert.call_count == 0
    b.refresh_positions()
    assert "HDFCBANK.NS" in b.positions  # still open remotely


def test_provider_selected_from_env_precedence(tmp_path):
    """TRIO_BROKER_PROVIDER beats config — ops can flip providers
    without editing config.yaml, and the test suite relies on this."""
    from src.paper_trader import PaperTrader
    cfg = {"symbols": ["ONGC.NS"], "timeframes": {"default": "15m"},
           "risk_management": {"capital": 500000.0},
           "logging": {"trade_log": str(Path(tmp_path) / "t.jsonl")},
           "broker": {"provider": "paper"}}
    with patch("src.paper_trader.load_config", return_value=cfg), \
         patch.dict("os.environ", {"TRIO_BROKER_PROVIDER": "megabull",
                                   "MEGABULL_API_KEY": "env-key"}):
        t = PaperTrader()
    assert t.broker_name == "megabull"
    assert t.broker.api_key == "env-key"

    # ...and falls back to config when the env var is absent.
    with patch("src.paper_trader.load_config", return_value=cfg), \
         patch.dict("os.environ", {}, clear=False):
        import os
        os.environ.pop("TRIO_BROKER_PROVIDER", None)
        t2 = PaperTrader()
    assert t2.broker_name == "paper"
