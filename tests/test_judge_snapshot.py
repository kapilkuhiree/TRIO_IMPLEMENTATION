"""Tests for the read-only judge snapshot builder in options_live_scan."""

from __future__ import annotations

from src.options_live_scan import _judge_snapshot


def _bar(stamp: str, o: float, h: float, l: float, c: float, v: float = 0.0):
    return {
        "timestamp": f"2026-11-11T{stamp}:00+05:30",
        "open": o, "high": h, "low": l, "close": c, "volume": v,
        "underlying_symbol": "NIFTY", "exchange": "NSE", "instrument_type": "index",
    }


def _candles():
    rows = [
        _bar("09:15", 25330.0, 25399.10, 25310.0, 25350.0),
        _bar("09:20", 25350.0, 25380.0, 25263.80, 25300.0),
        _bar("09:25", 25300.0, 25340.0, 25280.0, 25320.0),
    ]
    # A handful of later bars so ATR and the tail have data.
    price = 25320.0
    for minute in range(30, 30 + 20 * 5, 5):
        hour, rem = divmod(minute, 60)
        stamp = f"{hour + 9:02d}:{rem:02d}"
        price -= 8.0
        rows.append(_bar(stamp, price + 4, price + 9, price - 9, price))
    return rows


def _candidate(direction="bearish", strategy="ORB"):
    return {
        "strategy": strategy,
        "direction": direction,
        "option_type": "PE",
        "signal_timestamp": "2026-11-11T13:35:00+05:30",
        "option_legs": {"legs": [{"ask": 220.55, "bid": 220.0, "strike": 25150}]},
    }


def test_snapshot_for_orb_short_has_levels_atr_and_extension():
    snapshot = _judge_snapshot(_candidate(), "NIFTY", _candles())

    assert snapshot["underlying"] == "NIFTY"
    assert snapshot["strategy"] == "orb"
    assert snapshot["direction"] == "bearish"
    assert snapshot["option_type"] == "PE"
    assert snapshot["orb_high"] == 25399.10
    assert snapshot["orb_low"] == 25263.80
    assert snapshot["atr"] > 0
    # Price is below the OR low, so a short is extended in its own direction.
    assert snapshot["extension_atr"] > 0
    assert snapshot["trigger"] == 25263.80
    assert snapshot["premium"] == 220.55
    assert snapshot["vwap"] is not None
    # Tail is capped so the prompt cannot grow without bound.
    assert len(snapshot["bars"]) == 16
    assert snapshot["bars"][-1]["t"] == _candles()[-1]["timestamp"][11:16]


def test_snapshot_for_orb_long_extension_is_positive_above_or_high():
    candles = _candles()
    for row in candles[3:]:
        row["open"] += 400
        row["high"] += 400
        row["low"] += 400
        row["close"] += 400
    snapshot = _judge_snapshot(_candidate(direction="bullish"), "NIFTY", candles)
    assert snapshot["trigger"] == 25399.10
    assert snapshot["extension_atr"] > 0


def test_shadow_sleeves_are_not_judged():
    assert _judge_snapshot(_candidate(strategy="MeanReversion"), "NIFTY", _candles()) == {}
    assert _judge_snapshot(_candidate(strategy="SMC Liquidity"), "NIFTY", _candles()) == {}


def test_incomplete_opening_range_yields_no_snapshot():
    candles = _candles()[:2]  # no 09:25 bar
    assert _judge_snapshot(_candidate(), "NIFTY", candles) == {}


def test_empty_candles_yield_no_snapshot():
    assert _judge_snapshot(_candidate(), "NIFTY", []) == {}
