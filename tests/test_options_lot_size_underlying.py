"""Per-underlying lot validation for the live Muhurat session.

The global ``options.lot_size`` is NIFTY's 65.  BANKNIFTY trades in 30-lots,
so validating every contract against 65 silently rejected every BANKNIFTY
candidate after the planner had already selected the correct contract.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from src.options_intraday import load_options_intraday_config, validate_option_candidate
from src.utils import load_config


IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 11, 8, 14, 30, tzinfo=IST)
EXPIRY = "09-Nov-2026"
ROOT = Path(__file__).resolve().parent.parent


def _cfg():
    with (ROOT / "config" / "options_intraday_muhurat.yaml").open(
            encoding="utf-8") as handle:
        mode = (yaml.safe_load(handle) or {})["options_intraday"]
    cfg = load_config()
    cfg["options_intraday"] = deepcopy(mode)
    return load_options_intraday_config(cfg)


def _quote():
    return {
        "bid": 99.0,
        "ask": 100.0,
        "openInterest": 5000,
        "totalTradedVolume": 1200,
        "quote_timestamp": "2026-11-08T14:29:30+05:30",
    }


def _candidate(underlying, lot_size):
    quote = _quote()
    leg = {
        "side": "BUY", "kind": "CE", "strike": 24550.0, "expiry": EXPIRY,
        "bid": 99.0, "ask": 100.0, "premium": 99.5, "premium_src": "live",
        "trading_symbol": f"{underlying}26N0924550CE",
    }
    option = {
        "strategy": "long-call",
        "underlying": underlying,
        "expiry": EXPIRY,
        "lot_size": lot_size,
        "exchange": "NSE",
        "legs": [leg],
    }
    return {
        "asset": "options",
        "action": "BUY",
        "direction": "bullish",
        "signal_underlying": underlying,
        "option_underlying": underlying,
        "option_legs": option,
    }


def _plan(underlying, expiry=EXPIRY):
    quote = _quote()
    row = {"strike": 24550.0, "CE": dict(quote), "PE": dict(quote)}
    return {"option_chain": {
        "symbol": underlying,
        "exchange": "NSE",
        "expiries": [expiry],
        "by_expiry": {expiry: [row]},
        "quote_timestamp": "2026-11-08T14:29:30+05:30",
    }}


def test_nifty_and_banknifty_lots_are_validated_per_underlying(monkeypatch):
    monkeypatch.setenv("TRIO_OPTIONS_LIVE", "1")
    monkeypatch.setenv("TRIO_BROKER_PROVIDER", "zerodha")
    monkeypatch.setenv("PAPER_TRADING", "false")
    cfg = _cfg()

    for underlying, lot_size in (("NIFTY", 65), ("BANKNIFTY", 30)):
        valid, reason = validate_option_candidate(
            _candidate(underlying, lot_size), _plan(underlying), cfg, now=NOW)
        assert valid is True, f"{underlying}: {reason}"


def test_wrong_lot_for_banknifty_is_still_rejected(monkeypatch):
    monkeypatch.setenv("TRIO_OPTIONS_LIVE", "1")
    monkeypatch.setenv("TRIO_BROKER_PROVIDER", "zerodha")
    monkeypatch.setenv("PAPER_TRADING", "false")
    cfg = _cfg()

    valid, reason = validate_option_candidate(
        _candidate("BANKNIFTY", 65), _plan("BANKNIFTY"), cfg, now=NOW)
    assert valid is False
    assert reason == "contract lot size mismatch"
