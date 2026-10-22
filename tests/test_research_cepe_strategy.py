"""Offline acceptance tests for the research-only CE/PE candidate strategy."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from src.research_cepe_strategy import (
    STRATEGY_NAME,
    evaluate_signal,
    generate_candidates,
)


IST = timezone(timedelta(hours=5, minutes=30))


def _candle(number, close, *, open_price=None, high=None, low=None,
            volume=1000, symbol="RELIANCE", instrument_type="stock",
            exchange="NSE", completed=True):
    timestamp = datetime(2026, 10, 14, 9, 15, tzinfo=IST) + timedelta(minutes=5 * number)
    open_price = close if open_price is None else open_price
    high = max(open_price, close) + 0.4 if high is None else high
    low = min(open_price, close) - 0.4 if low is None else low
    return {
        "timestamp": timestamp.isoformat(),
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "exchange": exchange,
        "underlying_symbol": symbol,
        "instrument_type": instrument_type,
        "completed": completed,
    }


def _bullish_rows(symbol="RELIANCE", instrument_type="stock", volume=1000):
    rows = []
    for number in range(53):
        close = 100.0 + number * 0.4
        rows.append(_candle(number, close, open_price=close - 0.1,
                            high=close + 0.4, low=close - 0.4,
                            symbol=symbol, instrument_type=instrument_type))

    # The prior completed bar overlaps EMA20 and closes near it.
    rows.append(_candle(53, 116.9, open_price=117.4, high=117.8, low=115.8,
                        symbol=symbol, instrument_type=instrument_type))
    # Strong close over the previous completed candle.  Its wider range also
    # makes the volume fallback deterministic when volume is absent/zero.
    rows.append(_candle(54, 121.5, open_price=117.3, high=122.0, low=116.9,
                        volume=volume, symbol=symbol, instrument_type=instrument_type))
    return rows


def _bearish_rows(symbol="RELIANCE", instrument_type="stock", volume=1000):
    rows = []
    for number in range(53):
        close = 200.0 - number * 0.4
        rows.append(_candle(number, close, open_price=close + 0.1,
                            high=close + 0.4, low=close - 0.4,
                            symbol=symbol, instrument_type=instrument_type))

    rows.append(_candle(53, 183.1, open_price=182.6, high=184.2, low=182.2,
                        symbol=symbol, instrument_type=instrument_type))
    rows.append(_candle(54, 178.5, open_price=182.7, high=183.1, low=178.0,
                        volume=volume, symbol=symbol, instrument_type=instrument_type))
    return rows


def _candidates(rows, index=None, **kwargs):
    signal_index = len(rows) - 1 if index is None else index
    return generate_candidates(rows, index=signal_index, **kwargs)


@pytest.mark.parametrize(
    ("factory", "option_type", "direction"),
    [(_bullish_rows, "CE", "bullish"), (_bearish_rows, "PE", "bearish")],
)
def test_trend_pullback_breakout_emits_only_the_directional_buy_candidate(
    factory, option_type, direction,
):
    rows = factory()
    candidates = _candidates(rows, underlying_symbol="RELIANCE")

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["underlying"] == "RELIANCE"
    assert candidate["option_type"] == option_type
    assert candidate["direction"] == direction
    assert candidate["action"] == "BUY"
    assert candidate["strategy_name"] == STRATEGY_NAME
    assert candidate["signal_timestamp"] == rows[-1]["timestamp"]
    assert candidate["entry"] > 0
    if direction == "bullish":
        assert candidate["stop"] < candidate["entry"] < candidate["target"]
    else:
        assert candidate["target"] < candidate["entry"] < candidate["stop"]
    assert "EMA20" in candidate["reason"]


@pytest.mark.parametrize("instrument_type,symbol", [
    ("stock", "RELIANCE"),
    ("index", "NIFTY"),
])
def test_stock_and_index_support_preserves_actual_underlying(instrument_type, symbol):
    rows = _bullish_rows(symbol=symbol, instrument_type=instrument_type, volume=0)
    rows[-1].pop("volume")

    candidates = _candidates(
        rows,
        underlying_symbol=symbol,
        expected_instrument_type=instrument_type,
    )

    assert len(candidates) == 1
    assert candidates[0]["underlying"] == symbol
    assert candidates[0]["option_type"] == "CE"
    assert candidates[0]["action"] == "BUY"
    assert "range-expansion fallback" in candidates[0]["reason"]


def test_prefix_only_evaluation_has_no_lookahead_and_rejects_forming_signal_bar():
    rows = _bullish_rows()
    expected = _candidates(rows, underlying_symbol="RELIANCE")
    signal_index = len(rows) - 1

    future = _candle(55, 999.0, open_price=900.0, high=1200.0, low=1.0,
                     symbol="RELIANCE")
    with_future = rows + [future]
    assert _candidates(
        with_future, index=signal_index, underlying_symbol="RELIANCE",
    ) == expected

    forming = deepcopy(rows)
    forming[-1]["completed"] = False
    assert _candidates(forming, underlying_symbol="RELIANCE") == []

    signal_start = datetime.fromisoformat(rows[-1]["timestamp"])
    assert _candidates(
        rows,
        underlying_symbol="RELIANCE",
        as_of=signal_start,
    ) == []
    assert _candidates(
        rows,
        underlying_symbol="RELIANCE",
        as_of=signal_start + timedelta(minutes=5),
    ) == expected


@pytest.mark.parametrize("field,value", [
    ("underlying_symbol", "OTHER"),
    ("exchange", "BSE"),
    ("instrument_type", "option"),
    ("instrument_type", "future"),
])
def test_wrong_identity_is_rejected(field, value):
    rows = _bullish_rows()
    rows[-1][field] = value
    assert _candidates(rows, underlying_symbol="RELIANCE") == []


def test_insufficient_history_and_noise_do_not_create_candidates():
    assert _candidates(_bullish_rows()[:49], underlying_symbol="RELIANCE") == []

    noise = [
        _candle(number, 150.0, open_price=149.9, high=150.2, low=149.8,
                symbol="RELIANCE")
        for number in range(55)
    ]
    assert _candidates(noise, underlying_symbol="RELIANCE") == []


def test_output_is_deterministic_and_input_is_not_mutated():
    rows = _bullish_rows()
    original = deepcopy(rows)

    first = _candidates(rows, underlying_symbol="RELIANCE")
    second = _candidates(rows, underlying_symbol="RELIANCE")

    assert first == second
    assert rows == original
    assert set(first[0]) == {
        "signal_timestamp", "underlying", "direction", "option_type",
        "action", "entry", "stop", "target", "entry_price", "stop_loss",
        "target_price", "reason", "strategy_name",
    }


def test_candidate_route_contains_no_non_buy_or_non_underlying_fields():
    candidate = _candidates(_bearish_rows(), underlying_symbol="RELIANCE")[0]

    assert candidate["action"] == "BUY"
    assert candidate["option_type"] in {"CE", "PE"}
    assert candidate["direction"] in {"bullish", "bearish"}
    assert not {"sell", "short", "equity", "futures", "premium", "contract"}.intersection(
        key.lower() for key in candidate
    )
