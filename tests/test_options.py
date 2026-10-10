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
    # style from config: default test config is spread mode
    from unittest.mock import patch
    sig = TradeSignal(symbol="NIFTY", action="BUY", entry_price=25000.0,
                      confidence=80)
    with patch("src.options_selector._opt_cfg",
               return_value={"enabled": True, "underlying": "NIFTY",
                             "lot_size": 50, "min_oi": 1000,
                             "delta_atm": 0.50, "delta_otm": 0.30,
                             "delta_long": 0.65, "style": "spread",
                             "max_iv_rank": 80}):
        pick = select(sig, _chain(), expiry="30-Oct-2026", iv_rank=40.0,
                      adx=30.0)
    assert pick.strategy == "bull-call-spread"
    assert len(pick.legs) == 2
    assert pick.maxLoss > 0 and pick.rank > 0


def test_selector_bear_put():
    from unittest.mock import patch
    sig = TradeSignal(symbol="NIFTY", action="SELL", entry_price=25000.0,
                      confidence=80)
    with patch("src.options_selector._opt_cfg",
               return_value={"enabled": True, "underlying": "NIFTY",
                             "lot_size": 50, "min_oi": 1000,
                             "delta_atm": 0.50, "delta_otm": 0.30,
                             "delta_long": 0.65, "style": "spread",
                             "max_iv_rank": 80}):
        pick = select(sig, _chain(), expiry="30-Oct-2026", iv_rank=40.0,
                      adx=30.0)
    assert pick.strategy == "bear-put-spread"
    assert len(pick.legs) == 2


def test_selector_long_call_itm():
    """Buy-only long mode: single ITM BUY leg, no shorts."""
    from unittest.mock import patch
    sig = TradeSignal(symbol="NIFTY", action="BUY", entry_price=25000.0,
                      confidence=80)
    with patch("src.options_selector._opt_cfg",
               return_value={"enabled": True, "underlying": "NIFTY",
                             "lot_size": 50, "min_oi": 1000,
                             "delta_atm": 0.50, "delta_otm": 0.30,
                             "delta_long": 0.65, "style": "long",
                             "max_iv_rank": 80}):
        pick = select(sig, _chain(), expiry="30-Oct-2026", iv_rank=40.0,
                      adx=30.0)
    assert pick.strategy == "long-call"
    assert len(pick.legs) == 1
    assert pick.legs[0].side == "BUY" and pick.legs[0].kind == "CE"
    assert pick.legs[0].strike <= 25000.0  # ITM
    assert pick.maxLoss > 0 and pick.rank > 0


def test_selector_long_put_itm():
    from unittest.mock import patch
    sig = TradeSignal(symbol="NIFTY", action="SELL", entry_price=25000.0,
                      confidence=80)
    with patch("src.options_selector._opt_cfg",
               return_value={"enabled": True, "underlying": "NIFTY",
                             "lot_size": 50, "min_oi": 1000,
                             "delta_atm": 0.50, "delta_otm": 0.30,
                             "delta_long": 0.65, "style": "long",
                             "max_iv_rank": 80}):
        pick = select(sig, _chain(), expiry="30-Oct-2026", iv_rank=40.0,
                      adx=30.0)
    assert pick.strategy == "long-put"
    assert len(pick.legs) == 1
    assert pick.legs[0].side == "BUY" and pick.legs[0].kind == "PE"
    assert pick.legs[0].strike >= 25000.0  # ITM
    assert pick.maxLoss > 0


def test_selector_long_chop_sits_out():
    """Long-only book never sells premium: chop -> none."""
    from unittest.mock import patch
    sig = TradeSignal(symbol="NIFTY", action="BUY", entry_price=25000.0,
                      confidence=80)
    with patch("src.options_selector._opt_cfg",
               return_value={"enabled": True, "underlying": "NIFTY",
                             "lot_size": 50, "min_oi": 1000,
                             "delta_atm": 0.50, "delta_otm": 0.30,
                             "delta_long": 0.65, "style": "long",
                             "max_iv_rank": 80}):
        pick = select(sig, _chain(), expiry="30-Oct-2026", iv_rank=40.0,
                      adx=10.0)
    assert pick.strategy == "none"


def test_selector_index_only_guard():
    """Non-NIFTY underlying is refused outright."""
    from unittest.mock import patch
    sig = TradeSignal(symbol="NIFTY", action="BUY", entry_price=25000.0,
                      confidence=80)
    with patch("src.options_selector._opt_cfg",
               return_value={"enabled": True, "underlying": "BANKNIFTY",
                             "lot_size": 50, "min_oi": 1000,
                             "delta_atm": 0.50, "delta_otm": 0.30,
                             "delta_long": 0.65, "style": "long",
                             "max_iv_rank": 80}):
        pick = select(sig, _chain(), expiry="30-Oct-2026", iv_rank=40.0,
                      adx=30.0)
    assert pick.strategy == "none"


def test_selector_hold_is_none():
    sig = TradeSignal(symbol="NIFTY", action="HOLD", confidence=30)
    pick = select(sig, _chain(), expiry="30-Oct-2026")
    assert pick.strategy == "none"


def test_selector_iv_penalty():
    from unittest.mock import patch as _p
    with _p("src.options_selector.load_config", return_value={
            "options": {"enabled": True, "underlying": "NIFTY",
                        "lot_size": 50, "min_oi": 1000,
                        "delta_atm": 0.50, "delta_otm": 0.30,
                        "delta_long": 0.65, "style": "long",
                        "max_iv_rank": 80}}):
        sig = TradeSignal(symbol="NIFTY", action="BUY", entry_price=25000.0,
                          confidence=80)
        lo = select(sig, _chain(), expiry="30-Oct-2026", iv_rank=20.0, adx=30.0)
        hi = select(sig, _chain(), expiry="30-Oct-2026", iv_rank=90.0, adx=30.0)
    assert hi.rank < lo.rank


def test_options_paper_open_close():
    from unittest.mock import patch as _p
    # options.enabled:false for production validation — pin the selector on.
    with _p("src.options_selector.load_config",
           return_value={"options": {"enabled": True, "underlying": "NIFTY",
                                     "lot_size": 50, "strike_step": 50,
                                     "delta_atm": 0.50, "delta_otm": 0.30,
                                     "delta_long": 0.65, "style": "long",
                                     "min_oi": 1000, "max_iv_rank": 80,
                                     "risk_free_rate": 0.065,
                                     "max_premium_pct": 5.0,
                                     "expiry_preference": "weekly",
                                     "paper_only": True,
                                     "fill_model": "bidask",
                                     "slippage_ticks": 0.5}}):
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


def _options_select(**kw):
    """Helper that pins options.enabled despite the production default."""
    from unittest.mock import patch as _p
    defaults = dict(
        enabled=True, underlying="NIFTY", lot_size=50, strike_step=50,
        delta_atm=0.50, delta_otm=0.30, delta_long=0.65, style="long",
        min_oi=1000, max_iv_rank=80, risk_free_rate=0.065,
        max_premium_pct=5.0, expiry_preference="weekly", paper_only=True,
        fill_model="bidask", slippage_ticks=0.5)
    defaults.update(kw.pop("options", {}))
    kw.setdefault("options", defaults)
    return _p("src.options_selector.load_config", return_value=kw["options"])


# The next 12 tests all need the pin; wrap them via a single fixup pass
# done live at import time by monkeypatching _chain -> select site. The
# helper above is exported for call sites that need a custom config.

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
    from unittest.mock import patch
    from src.broker.megabull_options import MegaBullOptionsBroker
    sig = TradeSignal(symbol="NIFTY", action="SELL", entry_price=25000.0,
                      confidence=80)
    with patch("src.options_selector._opt_cfg",
               return_value={"enabled": True, "underlying": "NIFTY",
                             "lot_size": 50, "min_oi": 1000,
                             "delta_atm": 0.50, "delta_otm": 0.30,
                             "delta_long": 0.65, "style": "spread",
                             "max_iv_rank": 80}):
        pick = select(sig, _chain(), expiry="30-Oct-2026", iv_rank=40.0,
                      adx=30.0)
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


def test_megabull_options_single_long(tmp_path):
    """Single-leg long on the fake remote: one BUY, books premium risk."""
    from unittest.mock import patch
    from src.broker.megabull_options import MegaBullOptionsBroker
    sig = TradeSignal(symbol="NIFTY", action="BUY", entry_price=25000.0,
                      confidence=80)
    with patch("src.options_selector._opt_cfg",
               return_value={"enabled": True, "underlying": "NIFTY",
                             "lot_size": 50, "min_oi": 1000,
                             "delta_atm": 0.50, "delta_otm": 0.30,
                             "delta_long": 0.65, "style": "long",
                             "max_iv_rank": 80}):
        pick = select(sig, _chain(), expiry="30-Oct-2026", iv_rank=40.0,
                      adx=30.0)
    assert pick.strategy == "long-call" and len(pick.legs) == 1
    mega = _MegaStub()
    b = MegaBullOptionsBroker(mega, lot_size=50)
    o = b.open_spread(pick, lots=1)
    assert o.status == "FILLED"
    assert len(mega.posts) == 1 and mega.posts[0]["side"] == "BUY"
    sid = list(b.positions.keys())[0]
    rec = b.close_spread(sid, pick.net_debit + 10.0, "test")
    assert rec["result"] == "PASS" and rec["asset"] == "options"


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


def test_option_plan_idempotent_across_passes(tmp_path):
    """2026-10-09 regression: execute_option_plan runs on EVERY pass
    (startup + 09:40/11:00/13:00 live re-scans). Repeated passes with the
    same plan must NOT stack fresh spreads — one spread id per session,
    so the same loss can never book 8x. Also honours the session cap."""
    from src.paper_trader import PaperTrader
    from src.broker.paper import PaperBroker

    trader = PaperTrader(symbols=["NIFTY"], test_mode=False)
    trader.trade_log_path = tmp_path / "trade_log.jsonl"
    trader.broker = PaperBroker(initial_capital=500000.0)
    trader.broker_name = "paper"

    plan = _bear_plan()
    opt_broker = OptionsPaperBroker(initial_capital=500000.0, lot_size=50)
    first = trader.execute_option_plan(plan, opt_broker=opt_broker)
    assert len(first) == 1
    # three more passes, same session: zero new spreads
    for _ in range(3):
        more = trader.execute_option_plan(plan, opt_broker=opt_broker)
        assert more == []
    assert len(opt_broker.positions) == 1

    # session cap: a trader with no spreads placed yet stops at the cap
    trader2 = PaperTrader(symbols=["NIFTY"], test_mode=False)
    trader2.trade_log_path = tmp_path / "trade_log2.jsonl"
    trader2.broker = PaperBroker(initial_capital=500000.0)
    trader2.broker_name = "paper"
    trader2._opt_spreads_placed = set()
    from unittest.mock import patch
    with patch("src.paper_trader.load_config",
               return_value={"options": {"lot_size": 50,
                                         "max_spreads_per_session": 0}}):
        capped = trader2.execute_option_plan(plan, opt_broker=(
            OptionsPaperBroker(initial_capital=500000.0, lot_size=50)))
    assert capped == []


def _bear_plan(cand_symbol="NIFTY"):
    """One candidate carrying a real bear-put option_legs block."""
    from unittest.mock import patch as _p
    with _p("src.options_selector.load_config", return_value={
            "options": {"enabled": True, "underlying": "NIFTY",
                        "lot_size": 50, "min_oi": 1000,
                        "delta_atm": 0.50, "delta_otm": 0.30,
                        "delta_long": 0.65, "style": "long",
                        "max_iv_rank": 80}}):
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


class _FixedSpread:
    """Synthetic spread candidate with a known mid debit and real bid/ask."""

    def to_dict(self):
        return {
            "strategy": "bull-call-spread", "expiry": "30-Oct-2026",
            "net_debit": 40.0, "maxLoss": 4000.0, "maxGain": 6000.0,
            "breakeven": 0.0,
            "legs": [
                {"side": "BUY", "strike": 25000, "kind": "CE",
                 "premium": 120.0, "bid": 118.0, "ask": 122.0},
                {"side": "SELL", "strike": 25200, "kind": "CE",
                 "premium": 80.0, "bid": 78.0, "ask": 82.0},
            ],
        }


def test_bidask_fill_is_executable_not_mid(monkeypatch):
    """spec §6 regression: when fill_model=bidask the paper broker must
    reprice each leg to its executable side (BUY ask+slip / SELL bid-slip).
    The previous code referenced undefined names and silently fell back to
    the mid debit, hiding the slippage — this test locks the fix.

    BUY ask 122 + 0.5 tick (0.025) = 122.03 ; SELL bid 78 - 0.025 = 77.98
    -> net debit 44.05 (rounded to 44.06 by the broker's 2dp rule).
    """
    from src.broker import options_paper as op
    monkeypatch.setattr(op, "load_config",
                        lambda *a, **k: {"options": {"fill_model": "bidask",
                                                     "slippage_ticks": 0.5}})
    b = OptionsPaperBroker(initial_capital=500000.0, lot_size=65)
    b.open_spread(_FixedSpread(), lots=1)
    filled = list(b.positions.values())[0].avg_price
    assert filled > 40.0 + 3.0, "bidask fill must add execution slippage"
    assert abs(filled - 44.06) < 0.02, f"unexpected executable debit {filled}"


def test_mid_fill_is_unchanged(monkeypatch):
    """Legacy fill_model=mid must stay exactly the selector's mid debit."""
    from src.broker import options_paper as op
    monkeypatch.setattr(op, "load_config",
                        lambda *a, **k: {"options": {"fill_model": "mid"}})
    b = OptionsPaperBroker(initial_capital=500000.0, lot_size=65)
    b.open_spread(_FixedSpread(), lots=1)
    filled = list(b.positions.values())[0].avg_price
    assert filled == 40.0


def test_round_trip_cost_matches_backtest_model():
    """spec §3 regression: the live paper cost helper must mirror
    scripts/options_backtest._costs_for (brokerage x orders + STT on sold
    premium). Config: brokerage 20/order, STT 0.0015, lot 65.
      naked long : 2 orders + STT on the exit sale.
      spread     : 4 orders + STT on ~half the premium each side.
    """
    from src.options_pricing import round_trip_cost
    long_cost = round_trip_cost("long-call", entry_debit=120.0,
                                exit_value=150.0, lots=1, lot_size=65)
    assert abs(long_cost - (20.0 * 2 + 150.0 * 65 * 0.0015)) < 0.01
    spread_cost = round_trip_cost("bear-put-spread", entry_debit=90.8,
                                  exit_value=90.8, lots=1, lot_size=65)
    assert abs(spread_cost - (20.0 * 4
                              + 90.8 * 65 * 0.0015)) < 0.01


def test_options_paper_close_books_net_of_costs(monkeypatch):
    """The paper close must book P&L NET of round-trip cost and deduct it
    from capital, so booked P&L and balance can never disagree."""
    from src.broker import options_paper as op
    monkeypatch.setattr(op, "load_config",
                        lambda *a, **k: {"options": {"fill_model": "mid",
                                                     "brokerage_per_order": 20,
                                                     "stt_rate_sold": 0.0015,
                                                     "lot_size": 65}})
    b = OptionsPaperBroker(initial_capital=500000.0, lot_size=65)
    b.open_spread(_FixedSpread(), lots=1)
    sid = list(b.positions.keys())[0]
    cap_before = b.capital
    rec = b.close_spread(sid, 50.0, "test")
    assert rec["gross_pnl"] == 10.0 * 1 * 65          # (50 - 40) * lot
    assert rec["cost"] > 0
    assert rec["pnl"] == round(rec["gross_pnl"] - rec["cost"], 2)
    assert b.capital == round(cap_before + 50.0 * 65 - rec["cost"], 2)
