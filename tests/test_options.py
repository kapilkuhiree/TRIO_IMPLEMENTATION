"""
TRIO — Options module tests (chain parse, BS pricing, selector, paper sim).
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

All network-free: chain rows are hand-built, Kite is never touched.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.options_pricing import (bs_price, greeks, implied_vol,
                                 spread_metrics)
from src.options_selector import select, OptionTradeCandidate
from src.broker.options_paper import OptionsPaperBroker
from src.signal_engine import TradeSignal


def _rows(spot=25000.0):
    rows = []
    for k in (24800.0, 24900.0, 25000.0, 25100.0, 25200.0):
        rows.append({
            "strike": k, "expiry": "30-Oct-2026",
            "CE": {"lastPrice": 120.0, "bidPrice": 118.0, "askPrice": 122.0,
                   "openInterest": 5000, "changeOI": 100,
                   "iv": 12.0, "volume": 2000},
            "PE": {"lastPrice": 110.0, "bidPrice": 108.0, "askPrice": 112.0,
                   "openInterest": 6000, "changeOI": 120,
                   "iv": 12.5, "volume": 2100},
        })
    return rows


def _chain():
    return {"underlying": 25000.0, "expiries": ["30-Oct-2026"],
            "by_expiry": {"30-Oct-2026": _rows()}, "source": "test"}


def test_bs_call_put_parity_bounds():
    c = bs_price(25000, 25000, 7 / 365, 0.15, "CE")
    p = bs_price(25000, 25000, 7 / 365, 0.15, "PE")
    assert c > 0 and p > 0
    # degenerate intrinsic floor
    assert bs_price(100, 90, 0, 0.2, "CE") == 10.0
    assert bs_price(100, 110, 0, 0.2, "PE") == 10.0


def test_greeks_delta_sign():
    g_call = greeks(25000, 25000, 7 / 365, 0.15, "CE")
    g_put = greeks(25000, 25000, 7 / 365, 0.15, "PE")
    assert 0 < g_call["delta"] < 1
    assert -1 < g_put["delta"] < 0
    assert g_call["gamma"] > 0


def test_iv_roundtrip():
    px = bs_price(25000, 25100, 7 / 365, 0.18, "CE")
    iv = implied_vol(px, 25000, 25100, 7 / 365, "CE")
    assert iv is not None and abs(iv - 0.18) < 0.01


def test_spread_metrics():
    m = spread_metrics(120.0, 40.0, 200.0, lots=1, lot_size=50)
    assert m["net_debit"] == 80.0
    assert m["maxLoss"] == 4000.0
    assert m["maxGain"] == 6000.0


def test_selector_bull_call():
    sig = TradeSignal(symbol="NIFTY", action="BUY", entry_price=25000.0,
                      confidence=80)
    pick = select(sig, _chain(), expiry="30-Oct-2026", iv_rank=40.0, adx=30.0)
    assert pick.strategy == "bull-call-spread"
    assert len(pick.legs) == 2
    assert pick.maxLoss > 0 and pick.rank > 0


def test_selector_bear_put():
    sig = TradeSignal(symbol="NIFTY", action="SELL", entry_price=25000.0,
                      confidence=80)
    pick = select(sig, _chain(), expiry="30-Oct-2026", iv_rank=40.0, adx=30.0)
    assert pick.strategy == "bear-put-spread"
    assert len(pick.legs) == 2


def test_selector_hold_is_none():
    sig = TradeSignal(symbol="NIFTY", action="HOLD", confidence=30)
    pick = select(sig, _chain(), expiry="30-Oct-2026")
    assert pick.strategy == "none"


def test_selector_iv_penalty():
    sig = TradeSignal(symbol="NIFTY", action="BUY", entry_price=25000.0,
                      confidence=80)
    lo = select(sig, _chain(), expiry="30-Oct-2026", iv_rank=20.0, adx=30.0)
    hi = select(sig, _chain(), expiry="30-Oct-2026", iv_rank=90.0, adx=30.0)
    assert hi.rank < lo.rank


def test_options_paper_open_close():
    sig = TradeSignal(symbol="NIFTY", action="BUY", entry_price=25000.0,
                      confidence=80)
    pick = select(sig, _chain(), expiry="30-Oct-2026", iv_rank=40.0, adx=30.0)
    b = OptionsPaperBroker(initial_capital=500000.0, lot_size=50)
    o = b.open_spread(pick, lots=1)
    assert o.status == "FILLED"
    sid = list(b.positions.keys())[0]
    # mark up 10 points -> profit
    b.mark_spread(sid, pick.net_debit + 10.0)
    rec = b.close_spread(sid, pick.net_debit + 10.0, "test")
    assert rec["result"] == "PASS" and rec["pnl"] > 0
    assert rec["asset"] == "options"


def test_megabull_option_symbol_mapping():
    """Leg -> MegaBull trading symbol, proven from their live instrument
    list (1070 NIFTY rows): NIFTY13OCT22250PE is 'NIFTY26O1322250PE'."""
    from src.broker.megabull_options import option_symbol
    assert option_symbol("NIFTY", "13-Oct-2026", 22250, "PE") == \
        "NIFTY26O1322250PE"
    assert option_symbol("NIFTY", "13-Oct-2026", 22000, "PE") == \
        "NIFTY26O1322000PE"
    assert option_symbol("NIFTY", "13-Oct-2026", 22250, "CE") == \
        "NIFTY26O1322250CE"
    assert option_symbol("NIFTY", "27-Oct-2026", 23000, "CE") == \
        "NIFTY26O2723000CE"


class _MegaStub:
    """Offline stand-in for MegaBullBroker (no network, no key)."""

    def __init__(self, fail_all=False):
        self.positions = {}
        self.orders = {}
        self.fail_all = fail_all
        self.posts = []
        self.capital = 500000.0
        self.provider_name = "megabull"

    def token_for(self, symbol):
        if self.fail_all:
            raise RuntimeError("sim refused option orders")
        return "9" + str(abs(hash(symbol)) % 10**7)

    def place_order(self, symbol, side, quantity, order_type="MKT",
                    price=None, reason="order", **kw):
        self.posts.append({"symbol": symbol, "side": side,
                           "qty": quantity})
        if self.fail_all:
            from src.broker.base import BrokerOrder
            return BrokerOrder(order_id="rej", symbol=symbol, side=side,
                               quantity=quantity, status="REJECTED",
                               broker="megabull", error="sim refused")
        from src.broker.base import BrokerOrder, Position
        from src.utils import utc_now
        o = BrokerOrder(order_id=f"mb_{len(self.posts)}", symbol=symbol,
                        side=side, quantity=quantity, status="FILLED",
                        broker="megabull", placed_at=utc_now(),
                        filled_at=utc_now())
        self.orders[o.order_id] = o
        self.positions[symbol] = Position(
            symbol=symbol, side="LONG" if side == "BUY" else "SHORT",
            quantity=quantity, avg_price=price or 0.0,
            current_price=price or 0.0)
        return o

    def close_position(self, symbol, price, reason="close", fraction=1.0):
        pos = self.positions.pop(symbol, None)
        if pos is None:
            return {}
        return {"symbol": symbol, "qty": pos.quantity, "exit": price}


def test_megabull_options_opens_both_legs(tmp_path):
    """Happy path on the fake remote: both legs placed by MegaBull symbol."""
    from src.broker.megabull_options import MegaBullOptionsBroker
    sig = TradeSignal(symbol="NIFTY", action="SELL", entry_price=25000.0,
                      confidence=80)
    pick = select(sig, _chain(), expiry="30-Oct-2026", iv_rank=40.0, adx=30.0)
    mega = _MegaStub()
    b = MegaBullOptionsBroker(mega, lot_size=50)
    o = b.open_spread(pick, lots=1)
    assert o.status == "FILLED"
    syms = [p["symbol"] for p in mega.posts]
    assert len(syms) == 2
    assert all(s.startswith("NIFTY26O30") and
               (s.endswith("CE") or s.endswith("PE")) for s in syms)
    sid = list(b.positions.keys())[0]
    b.mark_spread(sid, pick.net_debit + 10.0)
    rec = b.close_spread(sid, pick.net_debit + 10.0, "test")
    assert rec["result"] == "PASS" and rec["asset"] == "options"
    assert not mega.positions, "remote legs must flatten on close"


def test_megabull_options_rejects_without_network(tmp_path):
    """The 2026-10-08 silent-fallback: when MegaBull refuses option legs
    (endpoint rejects all lots/units variants), open_spread reports
    REJECTED and leaves NO orphan leg behind — the session then books the
    local shadow sim and keeps running."""
    from src.broker.megabull_options import MegaBullOptionsBroker
    from src.paper_trader import PaperTrader
    from src.broker.paper import PaperBroker
    sig = TradeSignal(symbol="NIFTY", action="SELL", entry_price=25000.0,
                      confidence=80)
    pick = select(sig, _chain(), expiry="30-Oct-2026", iv_rank=40.0, adx=30.0)

    mega = _MegaStub(fail_all=True)
    b = MegaBullOptionsBroker(mega, lot_size=50)
    o = b.open_spread(pick, lots=1)
    assert o.status == "REJECTED"
    assert not b.positions, "partial spread must not linger"

    # full session wiring: remote rejects -> local shadow books the P&L
    trader = PaperTrader(symbols=["NIFTY"], test_mode=False)
    trader.trade_log_path = tmp_path / "trade_log.jsonl"
    trader.broker = PaperBroker(initial_capital=500000.0)
    trader.broker_name = "paper"
    plan = _bear_plan()
    placed = trader.execute_option_plan(plan, opt_broker=b)
    shadows = [r for r in placed if r.get("shadow")]
    assert len(shadows) == 1
    assert len(getattr(trader, "_opt_shadows", [])) == 1
    lines = [json.loads(x) for x in
             (tmp_path / "trade_log.jsonl").read_text().splitlines() if x]
    orders = [r for r in lines if r.get("event") == "order"]
    assert any(o.get("asset") == "options" for o in orders)


def _bear_plan(cand_symbol="NIFTY"):
    """One candidate carrying a real bear-put option_legs block."""
    sig = TradeSignal(symbol=cand_symbol, action="SELL",
                      entry_price=25000.0, confidence=80)
    pick = select(sig, _chain(), expiry="30-Oct-2026", iv_rank=40.0, adx=30.0)
    return {"date": "2026-10-08", "candidates": [{
        "symbol": cand_symbol, "action": "BUY", "entry_price": 25000.0,
        "stop_loss": 24500.0, "target": 26000.0, "position_size": 1,
        "confidence": 80, "rank": 90.0, "setup_name": "pullback",
        "risk_amount": 100000.0, "option_legs": pick.to_dict(),
    }]}


def test_equity_and_options_open_together(tmp_path, monkeypatch):
    """Both books open from the same plan in one session — the whole point.

    Equity order and options spread must BOTH land in the audit ledger
    (tagged asset=options) so the digest can split them.
    """
    from src.paper_trader import PaperTrader
    from src.broker.paper import PaperBroker

    # test_mode=False + an explicit tmp ledger keeps the live log pristine
    # (the *_test sidecar only kicks in when the path IS the live ledger).
    trader = PaperTrader(symbols=["NIFTY"], test_mode=False)
    trader.trade_log_path = tmp_path / "trade_log.jsonl"
    trader.broker = PaperBroker(initial_capital=500000.0)
    trader.broker_name = "paper"

    plan = _bear_plan()
    eq = trader.execute_plan(plan)
    opt = trader.execute_option_plan(plan)

    assert len(eq) == 1, "equity leg did not open"
    assert len(opt) == 1, "options spread did not open"

    lines = [json.loads(x) for x in
             (tmp_path / "trade_log.jsonl").read_text().splitlines() if x]
    orders = [r for r in lines if r.get("event") == "order"]
    assert any(o.get("asset") == "options" for o in orders)
    assert any(o.get("asset", "equity") != "options" for o in orders)


def test_squareoff_options_logs_close_to_ledger(tmp_path, monkeypatch):
    """EOD options square-off must write a close into the shared ledger."""
    import scripts.session as session_mod
    from src.paper_trader import PaperTrader
    from src.broker.paper import PaperBroker

    # fixed mid avoids the live NSE chain during tests
    monkeypatch.setattr(session_mod, "_chain_mid_for",
                        lambda *a, **k: 100.0)

    trader = PaperTrader(symbols=["NIFTY"], test_mode=False)
    trader.trade_log_path = tmp_path / "trade_log.jsonl"
    trader.broker = PaperBroker(initial_capital=500000.0)
    trader.broker_name = "paper"

    opt_broker = OptionsPaperBroker(initial_capital=500000.0, lot_size=50)
    plan = _bear_plan()
    placed = trader.execute_option_plan(plan, opt_broker=opt_broker)
    assert len(placed) == 1

    n = session_mod._squareoff_options(opt_broker, "eod-squareoff",
                                       trader=trader)
    assert n == 1

    lines = [json.loads(x) for x in
             (tmp_path / "trade_log.jsonl").read_text().splitlines() if x]
    closes = [r for r in lines if r.get("event") == "close"]
    assert any(c.get("asset") == "options" for c in closes)

    # digest must book the options P&L separately from equities
    import scripts.digest as digest
    from datetime import datetime
    monkeypatch.setattr(digest, "LOG_PATH", tmp_path / "trade_log.jsonl")
    today = datetime.now(digest.IST).strftime("%Y-%m-%d")
    d = digest.collect(today)
    assert d["opt"]["n_closes"] == 1
    assert d["eq"]["n_closes"] == 0
    text = digest.format_digest(d)
    assert "Options:" in text
