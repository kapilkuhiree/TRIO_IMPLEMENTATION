"""
TRIO — Trade-log tests: every audit event lands in the JSONL file.
Author: Kapil Kuhire <kapilkuhire89@gmail.com>
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.paper_trader import PaperTrader
from src.screener import RankedCandidate
from src.signal_engine import TradeSignal


def _cand(action="BUY", symbol="RELIANCE.NS", size=10):
    sig = TradeSignal(symbol=symbol, action=action, entry_price=100.0,
                      stop_loss=95.0, target=107.5, position_size=size,
                      confidence=80, reasoning=["pullback ok"])
    return RankedCandidate(rank=90.0, signal=sig, edge_atr=2.0,
                           setup_name="pullback-long")


def _trader(tmp_path):
    t = PaperTrader(symbols=["RELIANCE.NS"], timeframe="1d",
                    initial_capital=10000.0)
    t.trade_log_path = Path(tmp_path) / "trade_log.jsonl"
    return t


def test_scan_closed_market_logs_scan_event(tmp_path):
    t = _trader(tmp_path)
    with patch.object(t, "_session_open", return_value=False):
        t.scan_once()
    lines = (Path(tmp_path) / "trade_log.jsonl").read_text(
        encoding="utf-8").strip().split("\n")
    rec = json.loads(lines[-1])
    assert rec["event"] == "scan"
    assert rec["session_open"] is False


def test_screen_and_order_logged(tmp_path):
    t = _trader(tmp_path)
    with patch("src.screener.screen", return_value=[_cand("BUY")]), \
         patch.object(t, "_session_open", return_value=True), \
         patch.object(t, "_guard_open_positions", return_value=[]), \
         patch.object(t, "_manage_open_positions", return_value=None):
        t.scan_once()
    events = [json.loads(l)["event"] for l in
              (Path(tmp_path) / "trade_log.jsonl").read_text(
                  encoding="utf-8").strip().split("\n")]
    assert "screen" in events
    assert "order" in events
    order = [json.loads(l) for l in
             (Path(tmp_path) / "trade_log.jsonl").read_text(
                 encoding="utf-8").strip().split("\n")
             if json.loads(l)["event"] == "order"][-1]
    assert order["symbol"] == "RELIANCE.NS"
    assert order["stop"] == 95.0 and order["target"] == 107.5


def test_close_logged_with_result(tmp_path):
    t = _trader(tmp_path)
    t.broker.place_order("RELIANCE.NS", "BUY", 10, price=100.0,
                         stop_loss=90.0, target=115.0)
    with patch("src.paper_trader.fetch_market_data") as md:
        md.return_value.latest_price = 85.0
        t._manage_open_positions()
    lines = (Path(tmp_path) / "trade_log.jsonl").read_text(
        encoding="utf-8").strip().split("\n")
    closes = [json.loads(l) for l in lines if json.loads(l)["event"] == "close"]
    assert closes and closes[-1]["reason"] == "stop-hit"


def test_log_writer_never_raises(tmp_path):
    t = _trader(tmp_path)
    t.trade_log_path = Path("/nonexistent-dir-xyz/deep/trade_log.jsonl")
    # read-only location must not break trading
    t._log_event("scan", {"session_open": True})
    t._write_trade_log_csv()


def test_csv_mirror_written(tmp_path):
    t = _trader(tmp_path)
    t._log_event("order", {"symbol": "X", "action": "BUY", "entry": 1.0})
    t._write_trade_log_csv()
    csv_path = Path(tmp_path) / "trade_log.csv"
    assert csv_path.exists()
    text = csv_path.read_text(encoding="utf-8")
    assert "symbol" in text.split("\n")[0] and "X" in text


def test_events_stamped_with_mode_and_broker(tmp_path):
    t = _trader(tmp_path)
    t._log_event("order", {"symbol": "X", "action": "BUY", "entry": 1.0})
    rec = json.loads((Path(tmp_path) / "trade_log.jsonl").read_text(
        encoding="utf-8").strip().split("\n")[-1])
    assert rec["mode"] == "paper"
    assert rec["broker"] == "paper"


def test_test_mode_writes_sidecar_log(tmp_path):
    """Smoke/test events must never pollute the live ledger (2026-10-06:
    price=100 test orders booked −700 into the live log)."""
    from src.paper_trader import PaperTrader
    t = PaperTrader(symbols=["RELIANCE.NS"], timeframe="1d",
                    initial_capital=10000.0, test_mode=True)
    t.trade_log_path = Path(tmp_path) / "trade_log.jsonl"
    t._log_event("order", {"symbol": "RELIANCE.NS", "action": "BUY",
                           "entry": 100.0})
    assert not (Path(tmp_path) / "trade_log.jsonl").exists()
    sidecar = Path(tmp_path) / "trade_log_test.jsonl"
    assert sidecar.exists()
    rec = json.loads(sidecar.read_text(encoding="utf-8").strip().split("\n")[-1])
    assert rec["mode"] == "test"
