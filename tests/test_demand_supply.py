"""Acceptance tests for causal demand/supply confirmation."""

from datetime import datetime, timedelta, timezone

import pandas as pd

from src.demand_supply import (
    confirm_demand_supply,
    filter_completed_frame,
)
from src.options_intraday_plan import build_intraday_plan
from src.signal_engine import TradeSignal
from src.screener import _demand_supply_passes


IST = timezone(timedelta(hours=5, minutes=30))


def _candle(stamp, open_price, high, low, close):
    return {
        "timestamp": f"2026-10-15T{stamp}:00+05:30",
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1000,
    }


def _demand_sequence():
    return [
        _candle("09:15", 100, 101, 99, 100),
        _candle("09:20", 100, 101, 99, 100),
        _candle("09:25", 100, 101, 99, 100),
        _candle("09:30", 100, 104, 99.5, 103),
        _candle("09:35", 102, 105, 100.2, 104),
    ]


def _supply_sequence():
    return [
        _candle("09:15", 100, 101, 99, 100),
        _candle("09:20", 100, 101, 99, 100),
        _candle("09:25", 100, 101, 99, 100),
        _candle("09:30", 100, 100.5, 96, 97),
        _candle("09:35", 98, 99.8, 95, 96),
    ]


def _enabled_cfg():
    return {
        "demand_supply": {
            "enabled": True,
            "lookback_bars": 20,
            "max_zone_age_bars": 20,
        }
    }


def test_bullish_signal_requires_and_finds_demand_reaction():
    result = confirm_demand_supply(
        _demand_sequence(), "bullish", cfg=_enabled_cfg())

    assert result.confirmed is True
    assert result.zone_type == "demand"
    assert result.zone_low == 99.0
    assert result.zone_high == 100.0
    assert result.trigger_timestamp.endswith("09:35:00+05:30")


def test_bearish_signal_requires_and_finds_supply_reaction():
    result = confirm_demand_supply(
        _supply_sequence(), "bearish", cfg=_enabled_cfg())

    assert result.confirmed is True
    assert result.zone_type == "supply"
    assert result.zone_low == 100.0
    assert result.zone_high == 101.0


def test_wrong_zone_direction_is_rejected():
    result = confirm_demand_supply(
        _demand_sequence(), "bearish", cfg=_enabled_cfg())

    assert result.confirmed is False
    assert "supply" in result.reason


def test_confirmation_does_not_look_ahead():
    candles = _demand_sequence()
    before = confirm_demand_supply(candles, "bullish", cfg=_enabled_cfg(), index=4)
    candles.append(_candle("09:40", 104, 105, 94, 95))
    after = confirm_demand_supply(candles, "bullish", cfg=_enabled_cfg(), index=4)

    assert after.to_dict() == before.to_dict()


def test_disabled_filter_does_not_block_existing_signal():
    result = confirm_demand_supply(
        [{"timestamp": "2026-10-15T09:15:00+05:30",
          "open": 100, "high": 101, "low": 99, "close": 100}],
        "bullish",
        cfg={"demand_supply": {"enabled": False}},
    )

    assert result.confirmed is True
    assert result.reason == "demand/supply disabled"


def test_intraday_filter_drops_out_of_session_and_forming_rows():
    index = pd.to_datetime([
        "2026-10-15T08:00:00+05:30",
        "2026-10-15T09:15:00+05:30",
        "2026-10-15T09:45:00+05:30",
        "2026-10-15T10:00:00+05:30",
    ])
    frame = pd.DataFrame({
        "Open": [1, 100, 101, 102],
        "High": [1, 101, 102, 103],
        "Low": [1, 99, 100, 101],
        "Close": [1, 100.5, 101.5, 102.5],
        "Volume": [1, 10, 10, 10],
    }, index=index)

    filtered, diagnostic = filter_completed_frame(
        frame, "15m", now=datetime(2026, 10, 15, 10, 0, tzinfo=IST))

    assert list(filtered.index.strftime("%H:%M")) == ["09:15", "09:45"]
    assert diagnostic["dropped"]["outside_session"] == 1
    assert diagnostic["dropped"]["incomplete"] == 1


def test_options_normalizer_ignores_historical_off_session_and_forming_rows():
    from src.options_intraday_plan import _normalise_candles

    rows = _options_candles()
    rows.extend([
        {**rows[0], "timestamp": "2026-10-14T08:00:00+05:30"},
        {**rows[-1], "timestamp": "2026-10-15T09:40:00+05:30"},
    ])
    normalized, diagnostic = _normalise_candles(
        rows,
        datetime(2026, 10, 15, 9, 40, tzinfo=IST),
        max_age_minutes=15,
        require_orb_opening_range=True,
    )

    assert diagnostic["valid"] is True
    assert diagnostic["latest_timestamp"].endswith("09:35:00+05:30")
    assert diagnostic["ignored_rows"]["outside_session"] == 1
    assert diagnostic["ignored_rows"]["future_or_incomplete"] == 1
    assert len(normalized) == 5


def test_equity_confirmation_is_applied_at_screener_boundary():
    frame = pd.DataFrame(_demand_sequence())
    signal = TradeSignal(symbol="TEST.NS", action="BUY", confidence=80)

    assert _demand_supply_passes(frame, signal, "15m", _enabled_cfg()) is True
    assert any("Demand/supply" in item for item in signal.reasoning)


def test_combined_equity_plan_gets_live_15m_confirmation(monkeypatch):
    from types import SimpleNamespace
    from src.paper_trader import PaperTrader

    trader = object.__new__(PaperTrader)
    trader.events = []
    trader._log_event = lambda event, payload: trader.events.append((event, payload))
    frame = pd.DataFrame(_demand_sequence())
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    frame.set_index("timestamp", inplace=True)
    monkeypatch.setattr(
        "src.paper_trader.fetch_market_data",
        lambda symbol, timeframe: SimpleNamespace(ohlcv=frame),
    )
    cfg = {
        "demand_supply": {
            "enabled": True,
            "equity_timeframe": "15m",
        }
    }
    plan = {"candidates": [{"symbol": "TEST.NS", "action": "BUY"}]}

    filtered = trader._filter_equity_plan_by_demand_supply(plan, cfg)

    assert len(filtered["candidates"]) == 1
    assert filtered["candidates"][0]["demand_supply"]["confirmed"] is True


def _options_cfg(enabled=True):
    return {
        "options_intraday": {
            "strategies": ["orb"],
            "demand_supply": {"enabled": enabled},
            "max_quote_age_minutes": 15,
            "min_oi": 1000,
            "max_spread_pct": 5,
            "contract_specs": {
                "lot_size": 65,
                "provenance": "test broker instrument snapshot",
                "valid_from": "2026-01-01",
                "valid_until": "2026-12-31",
            },
        },
        "options": {"underlying": "NIFTY", "lot_size": 65},
        "risk_management": {"capital": 500000},
    }


def _options_candles():
    rows = _demand_sequence()
    for row in rows:
        row.update({"exchange": "NSE", "underlying_symbol": "NIFTY",
                    "instrument_type": "index"})
    return rows


def _options_chain():
    expiry = "16-Oct-2026"
    quote_time = "2026-10-15T09:39:00+05:30"
    quote = {
        "bidPrice": 9.8, "askPrice": 10.0,
        "openInterest": 5000, "volume": 1000,
        "quote_timestamp": quote_time,
    }
    return {
        "symbol": "NIFTY", "underlying": 104.0,
        "expiries": [expiry], "source": "test",
        "retrieved_at": quote_time,
        "diagnostics": {"retrieval": {"success": True}},
        "by_expiry": {expiry: [{
            "strike": 100, "expiry": expiry,
            "underlying_symbol": "NIFTY", "exchange": "NSE",
            "CE": dict(quote), "PE": dict(quote),
        }]},
    }


def test_strict_options_plan_records_demand_confirmation(monkeypatch):
    from strategies.orb_strategy import OrbStrategy

    monkeypatch.setattr(
        OrbStrategy, "evaluate_signal",
        lambda *args, **kwargs: {
            "allowed": True, "signal_type": "breakout",
            "option_type": "CE", "entry_price": 100,
            "stop_loss": 99, "target": 105,
            "reasoning": "test demand breakout",
        },
    )
    plan = build_intraday_plan(
        _options_cfg(True),
        now=datetime(2026, 10, 15, 9, 40, tzinfo=IST),
        candles=_options_candles(), chain=_options_chain(),
    )

    assert len(plan["option_candidates"]) == 1
    confirmation = plan["option_candidates"][0]["option_diagnostic"][
        "demand_supply"]
    assert confirmation["confirmed"] is True
    assert confirmation["zone_type"] == "demand"


def test_strict_options_plan_rejects_without_demand_confirmation(monkeypatch):
    from strategies.orb_strategy import OrbStrategy

    monkeypatch.setattr(
        OrbStrategy, "evaluate_signal",
        lambda *args, **kwargs: {
            "allowed": True, "signal_type": "breakout",
            "option_type": "CE", "entry_price": 100,
            "stop_loss": 99, "target": 105,
            "reasoning": "test no-zone breakout",
        },
    )
    candles = _options_candles()
    candles[-2]["high"] = 110
    candles[-2]["close"] = 109
    candles[-1]["open"] = 104
    candles[-1]["low"] = 102.5
    candles[-1]["close"] = 103
    plan = build_intraday_plan(
        _options_cfg(True),
        now=datetime(2026, 10, 15, 9, 40, tzinfo=IST),
        candles=candles, chain=_options_chain(),
    )

    assert plan["option_candidates"] == []
    rejected = plan["option_chain_diagnostic"].get("rejected_signals", [])
    assert any(item["reason"] == "demand_supply_confirmation_failed"
               for item in rejected)
