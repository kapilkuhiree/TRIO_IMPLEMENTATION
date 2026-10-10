"""
TRIO — Unit tests for the Active Manager (Guard logic).
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.paper_trader import PaperTrader
from src.indicators import IndicatorReading
from types import SimpleNamespace


def _order_for(sym, stop, target):
    return SimpleNamespace(symbol=sym, status="FILLED",
                           stop_loss=stop, target=target)


def _trader():
    t = PaperTrader(symbols=["RELIANCE.NS"], timeframe="1d",
                       initial_capital=10000.0)
    # Give it a "filled" order record so _manage_open_positions can read SL/TP
    t.broker.orders["test_id"] = _order_for(
        "RELIANCE.NS", 90.0, 115.0)
    return t

def _md(close=100.0):
    # ohlcv present (may be None) so the guard's _atr() helper can reach
    # compute_indicators instead of AttributeError-ing out of the trail arm.
    return SimpleNamespace(latest_price=close, ohlcv=None)

def test_guard_stop_hit():
    t = _trader()
    # Manually add a position
    t.broker.positions["RELIANCE.NS"] = SimpleNamespace(
        quantity=10, side="LONG", avg_price=100.0, order_id="test_id"
    )
    with patch("src.paper_trader.fetch_market_data", return_value=_md(89.0)):
        t._manage_open_positions()
    
    assert "RELIANCE.NS" not in t.broker.positions
    assert len(t.broker.closed_trades) == 1
    assert t.broker.closed_trades[0]["reason"].startswith("stop-hit")

def test_guard_target_hit():
    t = _trader()
    t.broker.positions["RELIANCE.NS"] = SimpleNamespace(
        quantity=10, side="LONG", avg_price=100.0, order_id="test_id",
        halved=False, booked_pnl=0.0
    )
    with patch("src.paper_trader.fetch_market_data", return_value=_md(116.0)):
        t._manage_open_positions()

    assert "RELIANCE.NS" not in t.broker.positions
    assert len(t.broker.closed_trades) == 1
    assert t.broker.closed_trades[0]["reason"].startswith("target-hit")


def _paper_trader():
    from src.broker.paper import PaperBroker
    from src.paper_trader import PaperTrader as PT
    t2 = PaperTrader(symbols=["RELIANCE.NS"], timeframe="1d",
                     initial_capital=10000.0)
    t2.broker = PaperBroker(initial_capital=10000.0)
    t2.broker_name = "paper"
    return t2


def test_guard_target_hit_still_closes_when_called_directly():
    # Loose guard (not ladder): target-hit closes in isolation. The guard
    # path now skips MegaBull mirrors (remote safety); this test pins the
    # local paper broker so the full-close path must still fire.
    t2 = _paper_trader()
    t2.broker.place_order("RELIANCE.NS", "BUY", 10, price=100.0,
                          stop_loss=90.0, target=115.0)
    assert t2.broker.get_positions()[0].halved is False
    with patch("src.paper_trader.fetch_market_data", return_value=_md(116.0)):
        t2._manage_open_positions()
    assert t2.broker.positions == {}
    assert t2.broker.closed_trades[-1]["reason"] == "target-hit"


def test_ladder_t1_partial_and_breakeven():
    """Ladder Phase 1: +0.8R takes 50% and moves stop to breakeven (risk-free).
    Entry 100, stop 90 => risk 10, T1 at 0.8R is 108. Price 109 should fire."""
    t = _paper_trader()
    t.broker.orders["test_id"] = _order_for("RELIANCE.NS", 90.0, 115.0)
    t.broker.positions["RELIANCE.NS"] = SimpleNamespace(
        quantity=10, side="LONG", avg_price=100.0,
        halved=False, current_price=100.0, pnl=0.0, pnl_pct=0.0)
    from unittest.mock import patch as _p
    with _p("src.paper_trader.fetch_market_data", return_value=_md(109.0)), \
         _p("src.paper_trader.load_config",
            return_value={"risk_management": {
                "partial_at_r": 0.8, "partial_fraction": 0.5}}):
        with _p("src.alerts.send_exit_alert"):
            t._guard_open_positions()
    assert t.broker.positions["RELIANCE.NS"].quantity == 5
    assert t.broker.positions["RELIANCE.NS"].halved is True
    order = t.broker.orders["test_id"]
    assert order.stop_loss == 100.0  # breakeven


def test_ladder_targets_derived():
    from src.risk_manager import ladder_targets
    assert ladder_targets(100, 90, "BUY")["T1"] == 108.0
    assert ladder_targets(100, 110, "SELL")["T1"] == 92.0


# ---------------------------------------------------------------------------
# Phase 1.1 — ladder / guard regressions (pt_r + tgt NameError fix)
# ---------------------------------------------------------------------------

def _phase2_cfg(t2=True):
    return {"risk_management": {
        "partial_at_r": 0.8, "partial_fraction": 0.5,
        "ladder": {"enabled_phase2": t2, "t1_at_r": 0.8, "t2_at_r": 1.5,
                   "t3_at_r": 2.5, "t1_fraction": 0.5, "t2_fraction": 0.30,
                   "trailing_after_t2": {"atr_multiplier": 3.0}}}}


def _atr_reader(val=2.0):
    return SimpleNamespace(indicators={"atr_14": SimpleNamespace(value=val)})


def _pos(qty=10, side="LONG", avg=100.0):
    return SimpleNamespace(quantity=qty, side=side, avg_price=avg,
                           halved=False, booked_pnl=0.0, current_price=avg,
                           pnl=0.0, pnl_pct=0.0)


def test_guard_t2_partial_and_trailing():
    """T2 at +1.5R closes 30% of remaining and arms the ATR trail."""
    t = _paper_trader()
    t.broker.orders["test_id"] = _order_for("RELIANCE.NS", 90.0, 300.0)
    pos = _pos(qty=10)
    t.broker.positions["RELIANCE.NS"] = pos
    with patch("src.paper_trader.fetch_market_data", return_value=_md(109.0)), \
         patch("src.paper_trader.load_config", return_value=_phase2_cfg()), \
         patch("src.alerts.send_exit_alert"):
        t._guard_open_positions()               # T1
    assert pos.halved is True and pos.quantity == 5
    with patch("src.paper_trader.fetch_market_data", return_value=_md(116.0)), \
         patch("src.paper_trader.compute_indicators",
               return_value=_atr_reader(2.0)), \
         patch("src.paper_trader.load_config", return_value=_phase2_cfg()), \
         patch("src.alerts.send_exit_alert"):
        t._guard_open_positions()               # T2
    assert getattr(pos, "ladder_t2_hit", False) is True
    assert pos.quantity == 4                    # int(5 * 0.30) = 1 closed
    assert t.broker.orders["test_id"].stop_loss == 110.0  # 116 - 3*2


def test_guard_stop_after_t1_uses_breakeven():
    t = _paper_trader()
    t.broker.orders["test_id"] = _order_for("RELIANCE.NS", 90.0, 300.0)
    t.broker.positions["RELIANCE.NS"] = _pos(qty=10)
    with patch("src.paper_trader.fetch_market_data", return_value=_md(109.0)), \
         patch("src.paper_trader.load_config", return_value=_phase2_cfg()), \
         patch("src.alerts.send_exit_alert"):
        t._guard_open_positions()               # T1 -> stop to breakeven 100
    with patch("src.paper_trader.fetch_market_data", return_value=_md(100.0)), \
         patch("src.paper_trader.load_config", return_value=_phase2_cfg()), \
         patch("src.alerts.send_exit_alert"):
        t._guard_open_positions()               # breakeven stop hit
    assert "RELIANCE.NS" not in t.broker.positions
    assert any(c["reason"].startswith("stop-hit")
               for c in t.broker.closed_trades)


def test_guard_target_after_t1_still_reachable():
    """The pt_r/tgt NameError used to kill the hard stop/target block for any
    post-T1 runner below T2. It must still fire."""
    t = _paper_trader()
    t.broker.orders["test_id"] = _order_for("RELIANCE.NS", 90.0, 120.0)
    t.broker.positions["RELIANCE.NS"] = _pos(qty=10)
    with patch("src.paper_trader.fetch_market_data", return_value=_md(109.0)), \
         patch("src.paper_trader.load_config", return_value=_phase2_cfg()), \
         patch("src.alerts.send_exit_alert"):
        t._guard_open_positions()               # T1
    with patch("src.paper_trader.fetch_market_data", return_value=_md(121.0)), \
         patch("src.paper_trader.load_config", return_value=_phase2_cfg()), \
         patch("src.alerts.send_exit_alert"):
        t._guard_open_positions()               # target-hit on the runner
    assert "RELIANCE.NS" not in t.broker.positions
    assert any(c["reason"].startswith("target-hit")
               for c in t.broker.closed_trades)


def test_guard_repeated_calls_do_not_duplicate_exits():
    t = _paper_trader()
    t.broker.orders["test_id"] = _order_for("RELIANCE.NS", 90.0, 300.0)
    pos = _pos(qty=10)
    t.broker.positions["RELIANCE.NS"] = pos
    for _ in range(3):
        with patch("src.paper_trader.fetch_market_data",
                   return_value=_md(109.0)), \
             patch("src.paper_trader.load_config",
                   return_value=_phase2_cfg()), \
             patch("src.alerts.send_exit_alert"):
            t._guard_open_positions()
    # exactly one T1 partial (10 -> 5), never 2 -> 2.5
    assert pos.quantity == 5
    partials = [c for c in t.broker.closed_trades
                if c["reason"].startswith("partial@T1")]
    assert len(partials) == 1


def test_guard_one_unit_position_skips_partial_keeps_full():
    """int(1 * 0.5) == 0 -> no quantity closed -> ladder state must NOT move
    and the position stays fully open."""
    t = _paper_trader()
    t.broker.orders["test_id"] = _order_for("RELIANCE.NS", 90.0, 300.0)
    pos = _pos(qty=1)
    t.broker.positions["RELIANCE.NS"] = pos
    with patch("src.paper_trader.fetch_market_data", return_value=_md(109.0)), \
         patch("src.paper_trader.load_config", return_value=_phase2_cfg()), \
         patch("src.alerts.send_exit_alert"):
        t._guard_open_positions()
    assert pos.quantity == 1
    assert pos.halved is False
    assert getattr(pos, "ladder_t1_hit", False) is False
    assert t.broker.closed_trades == []


def test_guard_short_ladder_and_stop():
    t = _paper_trader()
    t.broker.orders["test_id"] = _order_for("RELIANCE.NS", 110.0, 50.0)
    pos = _pos(qty=10, side="SHORT", avg=100.0)
    t.broker.positions["RELIANCE.NS"] = pos
    # SHORT T1 at +0.8R: (100 - 91)/10 = 0.9
    with patch("src.paper_trader.fetch_market_data", return_value=_md(91.0)), \
         patch("src.paper_trader.load_config", return_value=_phase2_cfg()), \
         patch("src.alerts.send_exit_alert"):
        t._guard_open_positions()
    assert pos.halved is True and pos.quantity == 5
    assert t.broker.orders["test_id"].stop_loss == 100.0  # breakeven
    # price back to 101 >= breakeven 100 -> stop hit
    with patch("src.paper_trader.fetch_market_data", return_value=_md(101.0)), \
         patch("src.paper_trader.load_config", return_value=_phase2_cfg()), \
         patch("src.alerts.send_exit_alert"):
        t._guard_open_positions()
    assert "RELIANCE.NS" not in t.broker.positions
    assert any(c["reason"].startswith("stop-hit")
               for c in t.broker.closed_trades)
