"""ORB consecutive-close confirmation at the pure signal boundary.

The live scan constructs a fresh ``OrbStrategy`` for every scan, so the
confirmation count must come from candle history rather than instance state.
These tests use a new instance for every evaluation on purpose.
"""

from datetime import datetime, timedelta, timezone

from candidate_pipeline import CandidatePipeline
from strategies.orb_strategy import OrbStrategy


IST = timezone(timedelta(hours=5, minutes=30))
SPECS = {"lot_size": 65, "expiry_preference": "weekly"}


def _rows(post_closes, symbol="NIFTY", instrument_type="index"):
    """Three opening-range bars (high 101 / low 99) plus post-range closes."""
    values = [
        (101, 99, 100),
        (101, 99, 100),
        (101, 99, 100),
        *[(close + 1, close - 1, close) for close in post_closes],
    ]
    start = datetime(2026, 11, 11, 9, 15, tzinfo=IST)
    rows = []
    for position, (high, low, close) in enumerate(values):
        rows.append({
            "timestamp": (start + timedelta(minutes=5 * position)).isoformat(),
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1000,
            "exchange": "NSE",
            "underlying_symbol": symbol,
            "instrument_type": instrument_type,
        })
    return rows


def _signal(rows, index, closes=2, symbol="NIFTY"):
    return OrbStrategy(orb_confirm_closes=closes).evaluate_signal(
        rows, index, SPECS, symbol)


def test_default_single_close_still_releases_on_the_first_bar():
    rows = _rows([102])
    signal = _signal(rows, 3, closes=1)
    assert signal.allowed
    assert signal.consecutive_closes == 1


def test_two_closes_are_required_and_a_fresh_instance_sees_the_history():
    rows = _rows([102, 103])

    first = _signal(rows, 3)
    assert not first.allowed
    assert "only 1" in first.reasoning

    # A brand-new instance must still count both completed closes.
    second = _signal(rows, 4)
    assert second.allowed
    assert second.consecutive_closes == 2
    assert second.bar_index == 4
    assert second.option_type == "CE"


def test_a_close_back_inside_the_range_resets_consecutive_progress():
    rows = _rows([102, 100, 103])
    signal = _signal(rows, 5)
    assert not signal.allowed
    assert signal.consecutive_closes == 0


def test_bearish_breakdown_requires_two_consecutive_closes_below_or_low():
    rows = _rows([98, 97])
    assert not _signal(rows, 3).allowed
    signal = _signal(rows, 4)
    assert signal.allowed
    assert signal.consecutive_closes == 2
    assert signal.option_type == "PE"

    # An inside close between breakdowns resets the count.
    reset = _rows([98, 100, 97])
    assert not _signal(reset, 5).allowed


def test_pure_pipeline_honours_the_configured_confirmation_count():
    rows = _rows([102, 103])
    specs = {"lot_size": 65, "expiry_preference": "weekly"}

    # Default (single close) keeps the historical behaviour.
    assert len(CandidatePipeline.evaluate_strategy_signals_pure(
        rows, "orb", underlying_symbol="NIFTY", contract_specs=specs, index=3,
    )) == 1

    assert CandidatePipeline.evaluate_strategy_signals_pure(
        rows, "orb", underlying_symbol="NIFTY", contract_specs=specs, index=3,
        orb_confirm_closes=2,
    ) == []
    candidates = CandidatePipeline.evaluate_strategy_signals_pure(
        rows, "orb", underlying_symbol="NIFTY", contract_specs=specs, index=4,
        orb_confirm_closes=2,
    )
    assert len(candidates) == 1
    assert candidates[0].option_type == "CE"
