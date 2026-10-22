"""Offline stock/index ORB regressions at the pure options-signal boundary."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import socket

import pytest

from candidate_pipeline import CandidatePipeline
from strategies.orb_strategy import OrbStrategy


IST = timezone(timedelta(hours=5, minutes=30))
INSTRUMENTS = [
    ("RELIANCE", "stock", 250, 2500),
    ("TCS", "stock", 175, 3500),
    ("NIFTY", "index", 65, 23500),
    ("BANKNIFTY", "index", 30, 50000),
]


@pytest.fixture(autouse=True)
def pure_offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("ORB signal tests must not use network, stateful pipeline, or backtests")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(CandidatePipeline, "__init__", forbidden)
    for module in (
        "strategies.orb_strategy.run_orb_backtest",
        "strategies.trend_pullback_strategy.run_pullback_backtest",
        "strategies.momentum_continuation_strategy.run_momentum_backtest",
    ):
        monkeypatch.setattr(module, forbidden)


def _candles(symbol="RELIANCE", instrument_type="stock", base=2500, move=4,
             date="2026-10-14"):
    values = [
        ("09:15", base, base + 1, base - 1, base),
        ("09:20", base, base + 3, base - 2, base + 1),
        ("09:25", base + 1, base + 2, base - 3, base - 1),
        ("09:30", base, max(base + 1, base + move + 1),
         min(base - 1, base + move - 1), base + move),
    ]
    return [
        {
            "timestamp": f"{date}T{stamp}:00+05:30",
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1000,
            "exchange": "NSE",
            "underlying_symbol": symbol,
            "instrument_type": instrument_type,
        }
        for stamp, open_price, high, low, close in values
    ]


def _evaluate(rows, symbol="RELIANCE", specs=None, index=None):
    index = len(rows) - 1 if index is None else index
    return OrbStrategy().evaluate_signal(rows, index, specs or {}, symbol)


def _candidates(rows, symbol="RELIANCE", specs=None, index=None):
    return CandidatePipeline.evaluate_strategy_signals_pure(
        rows, "orb", underlying_symbol=symbol, contract_specs=specs, index=index,
    )


@pytest.mark.parametrize("symbol,instrument_type,lot_size,base", INSTRUMENTS)
@pytest.mark.parametrize("move,option_type,variant,signal_type", [
    (4, "CE", "bullish", "breakout"),
    (-4, "PE", "bearish", "breakdown"),
])
def test_stock_and_index_signals_keep_actual_underlying_and_orb_levels(
    symbol, instrument_type, lot_size, base, move, option_type, variant, signal_type,
):
    rows = _candles(symbol, instrument_type, base, move)
    original = deepcopy(rows)
    specs = {"lot_size": lot_size,
             "expiry_preference": "monthly" if instrument_type == "stock" else "weekly"}
    sign = 1 if move > 0 else -1

    signal = _evaluate(rows, symbol, specs)
    assert signal.allowed
    assert signal.underlying_symbol == symbol
    assert signal.option_type == option_type
    assert signal.variant == variant
    assert signal.signal_type == signal_type
    assert signal.entry_price == base + move
    assert signal.trigger_price == base + sign * 3
    assert signal.stop_loss == base - sign * 3
    assert signal.target == base + sign * 9
    assert signal.trigger_time == rows[-1]["timestamp"]
    assert signal.bar_index == 3
    assert signal.expiry == specs["expiry_preference"]
    assert f"underlying: {symbol}" in signal.reasoning

    candidates = _candidates(rows, symbol, specs)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.underlying_symbol == symbol
    assert candidate.exchange == "NSE"
    assert candidate.strategy_name == "ORB"
    assert candidate.direction == variant
    assert candidate.option_type == option_type
    assert candidate.signal_timestamp == signal.trigger_time
    assert candidate.underlying_price_at_signal == signal.entry_price
    assert candidate.entry_price == signal.entry_price
    assert candidate.stop_loss == signal.stop_loss
    assert candidate.target == signal.target
    assert candidate.selected_contract is None
    assert candidate.contract_eligibility is None
    assert not candidate.risk_approved
    assert not candidate.trade_executed
    assert candidate.position_size == 0
    assert rows == original


@pytest.mark.parametrize("symbol,instrument_type", [("RELIANCE", "stock"), ("NIFTY", "index")])
@pytest.mark.parametrize("bar_index", [1, 3], ids=["opening-range", "signal"])
@pytest.mark.parametrize("field,value", [
    ("underlying_symbol", "OTHER"),
    ("underlying_symbol", ""),
    ("underlying_symbol", None),
    ("exchange", "BSE"),
    ("exchange", "NFO"),
    ("exchange", ""),
    ("exchange", None),
    ("instrument_type", "option"),
    ("instrument_type", "future"),
    ("instrument_type", ""),
    ("instrument_type", None),
])
def test_invalid_opening_or_signal_identity_cannot_generate_options(
    symbol, instrument_type, bar_index, field, value,
):
    rows = _candles(symbol, instrument_type)
    if value is None:
        rows[bar_index].pop(field)
    else:
        rows[bar_index][field] = value

    assert not _evaluate(rows, symbol).allowed
    assert _candidates(rows, symbol) == []


@pytest.mark.parametrize("instrument_type,other_type", [("stock", "index"), ("index", "stock")])
@pytest.mark.parametrize("bar_index", [1, 3])
def test_opening_range_and_signal_must_have_the_same_instrument_type(
    instrument_type, other_type, bar_index,
):
    rows = _candles(instrument_type=instrument_type)
    rows[bar_index]["instrument_type"] = other_type
    assert not _evaluate(rows).allowed
    assert _candidates(rows) == []


def test_stock_and_index_histories_are_never_substituted_for_each_other():
    stock = _candles()
    index = _candles("NIFTY", "index")
    assert not OrbStrategy().evaluate_signal(stock, 3, {}).allowed
    assert CandidatePipeline.evaluate_strategy_signals_pure(stock, "orb") == []
    assert _candidates(index, "RELIANCE") == []
    assert _candidates(stock, "TCS") == []


@pytest.mark.parametrize("instrument_type", ["stock", "index"])
@pytest.mark.parametrize("move", [-3, -1, 0, 1, 3])
def test_wicks_and_closes_at_or_inside_range_do_not_relax_confirmation(instrument_type, move):
    rows = _candles(instrument_type=instrument_type, move=move)
    rows[-1].update(high=2520, low=2480)
    assert not _evaluate(rows).allowed
    assert _candidates(rows) == []


@pytest.mark.parametrize("move", [4, -4])
def test_stock_signal_uses_only_current_session_opening_range_and_causal_prefix(move):
    rows = _candles(move=move)
    prior = _candles(base=5000, date="2026-10-13")
    # A completed post-range candle must not alter the fixed opening levels.
    post_range = dict(rows[-1], high=2600, low=2400, close=2500)
    signal_bar = dict(rows[-1], timestamp="2026-10-14T09:35:00+05:30")
    prefix = prior + rows[:3] + [post_range, signal_bar]
    expected = _evaluate(prefix)
    sign = 1 if move > 0 else -1
    assert expected.allowed
    assert expected.trigger_price == 2500 + sign * 3
    assert expected.stop_loss == 2500 - sign * 3
    assert expected.target == 2500 + sign * 9

    future = dict(rows[-1], timestamp="2026-10-14T09:40:00+05:30",
                  high=99999, low=1, close=50000)
    full = prefix + [future]
    assert _evaluate(full, index=len(prefix) - 1) == expected
    assert _candidates(full, index=len(prefix) - 1) == _candidates(prefix)


def test_previous_session_range_cannot_replace_missing_stock_opening_bar():
    prior = _candles(date="2026-10-13")
    current = _candles()
    rows = prior + [current[0], current[2], current[3]]
    assert not _evaluate(rows).allowed
    assert _candidates(rows) == []


@pytest.mark.parametrize("index", [-1, 0, 1, 2, 4])
def test_stock_signal_requires_full_opening_range_and_valid_completed_bar_index(index):
    rows = _candles()
    assert not _evaluate(rows, index=index).allowed
    assert _candidates(rows, index=index) == []


@pytest.mark.parametrize("stamp,allowed", [("14:30", True), ("14:35", False)])
@pytest.mark.parametrize("instrument_type", ["stock", "index"])
def test_stock_and_index_keep_existing_entry_cutoff(instrument_type, stamp, allowed):
    rows = _candles(instrument_type=instrument_type)
    rows[-1]["timestamp"] = f"2026-10-14T{stamp}:00+05:30"
    assert _evaluate(rows).allowed is allowed
    assert bool(_candidates(rows)) is allowed


@pytest.mark.parametrize("move,option_type", [(4, "CE"), (-4, "PE")])
def test_stock_completed_5m_boundary_releases_signal_only_at_close(move, option_type):
    # This is the scanner/planner's pure completion filter, with injected bars
    # only. ORB has no wall clock and consumes the already-completed prefix.
    from src.options_intraday_plan import _normalise_candles

    rows = _candles(move=move)
    forming = dict(rows[-1], timestamp="2026-10-14T09:35:00+05:30",
                   high=99999, low=1, close=50000)
    now = datetime(2026, 10, 14, 9, 35, tzinfo=IST)
    before, before_diag = _normalise_candles(
        rows + [forming], now - timedelta(seconds=1),
        expected_underlying="RELIANCE", expected_instrument_type="stock",
    )
    assert len(before) == 3
    assert not before_diag["valid"]
    assert _candidates(before) == []

    completed, diagnostic = _normalise_candles(
        rows + [forming], now,
        expected_underlying="RELIANCE", expected_instrument_type="stock",
    )
    assert diagnostic["valid"]
    assert diagnostic["latest_completion_timestamp"] == now.isoformat()
    assert diagnostic["ignored_rows"]["future_or_incomplete"] == 1
    assert len(completed) == 4
    candidates = _candidates(completed)
    assert len(candidates) == 1
    assert candidates[0].underlying_symbol == "RELIANCE"
    assert candidates[0].option_type == option_type
    assert candidates[0].entry_price == 2500 + move
