"""Live wiring: the runtime must accept the normalized Zerodha NFO adapter."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

import src.options_intraday as oi
from src.broker.zerodha_options import ZerodhaOptionsBroker
from src.options_intraday_runtime import OptionsIntradayRuntime
from src.utils import load_config


IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent.parent
SYMBOL = "NIFTY26N0924550CE"


class _Kite:
    def positions(self):
        return {"net": []}

    def margins(self):
        return {"equity": {"net": 40000.0,
                           "available": {"cash": 40000.0},
                           "utilised": {"debits": 0.0}}}

    def orders(self):
        return []


class _Market:
    def __init__(self):
        self.kite = _Kite()

    def instruments(self, exchange):
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

    def option_quote(self, row):
        return {"bid": 99.0, "ask": 100.0, "last_price": 100.0,
                "timestamp": "2026-11-08T14:30:00+05:30"}


class _Trader:
    def __init__(self, broker):
        self.broker = broker
        self.opt_broker = broker
        self.trading_mode = "options_intraday"
        self.execution_class = "live"
        self.options_only = True


def _mode(tmp_path):
    with (ROOT / "config" / "options_intraday_muhurat.yaml").open(
            encoding="utf-8") as handle:
        mode = (yaml.safe_load(handle) or {})["options_intraday"]
    mode = deepcopy(mode)
    mode["state_path"] = str(tmp_path / "state.json")
    return mode


def test_runtime_accepts_zerodha_adapter_and_normalized_contract(monkeypatch, tmp_path):
    monkeypatch.setenv("TRIO_OPTIONS_LIVE", "1")
    monkeypatch.setenv("TRIO_BROKER_PROVIDER", "zerodha")
    monkeypatch.setenv("PAPER_TRADING", "false")

    fixed = datetime(2026, 11, 8, 14, 30, tzinfo=IST)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(oi, "datetime", FixedDateTime)

    broker = ZerodhaOptionsBroker(_Market(), poll_seconds=0.0)
    cfg = {"options_intraday": _mode(tmp_path),
           "market": load_config()["market"],
           "options": {"lot_size": 65, "exchange": "NSE"}}
    trader = _Trader(broker)
    runtime = OptionsIntradayRuntime(trader, cfg)

    assert runtime.execution_class == "live"
    assert runtime._require_broker() is broker

    option = {
        "underlying": "NIFTY",
        "expiry": "09-Nov-2026",
        "lot_size": 65,
        "exchange": "NSE",
        "legs": [{"side": "BUY", "kind": "CE", "strike": 24550.0,
                  "trading_symbol": SYMBOL}],
    }
    candidate = {"option_underlying": "NIFTY"}
    symbol, row = runtime._validate_instrument(
        candidate, option, option["legs"][0], broker)
    assert symbol == SYMBOL
    assert row["exchange"] == "NSE"
    assert row["lot_size"] == 65
    assert row["instrumentType"] == "OPTIDX"


def test_paper_runtime_still_rejects_the_live_adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("TRIO_OPTIONS_LIVE", "0")
    monkeypatch.setenv("TRIO_BROKER_PROVIDER", "megabull")
    monkeypatch.setenv("TRIO_REMOTE_REQUIRED", "1")
    monkeypatch.setenv("PAPER_TRADING", "true")

    from src.options_intraday import load_options_intraday_config
    paper = load_options_intraday_config({"options_intraday": {
        "execution_class": "remote_paper", "PAPER_TRADING": True,
        "state_path": str(tmp_path / "state.json")}})
    broker = ZerodhaOptionsBroker(_Market(), poll_seconds=0.0)
    trader = _Trader(broker)
    trader.execution_class = "remote_paper"
    runtime = OptionsIntradayRuntime(trader, paper)
    try:
        runtime._require_broker(broker)
    except Exception as exc:
        assert "unapproved options broker" in str(exc)
    else:  # pragma: no cover - must never be reached
        raise AssertionError("paper runtime accepted a live Zerodha adapter")
