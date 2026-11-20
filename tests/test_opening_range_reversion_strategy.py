"""Tests for the Opening Range Reversion Scalp (ORRS) signal module."""

from __future__ import annotations

import pytest

from candidate_pipeline import CandidatePipeline
from strategies.opening_range_reversion_strategy import (
    ENTRY_CUTOFF,
    ORRS_EXIT_PROFILE,
    POKE_FRACTION,
    WIDTH_MAX_FRACTION,
    WIDTH_MIN_FRACTION,
    OpeningRangeReversionStrategy,
)

DATE = "2026-11-11"


def _bar(clock: str, open_: float, high: float, low: float, close: float,
         *, symbol: str = "NIFTY", exchange: str = "NSE",
         instrument_type: str = "index"):
    return {
        "timestamp": f"{DATE}T{clock}:00+05:30",
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1000,
        "underlying_symbol": symbol,
        "exchange": exchange,
        "instrument_type": instrument_type,
    }


def _opening_range(symbol: str = "NIFTY"):
    """A valid OR: high 100.0, low 99.6, W 0.4, 09:25 close 99.8 (0.40% of spot)."""
    return [
        _bar("09:15", 99.8, 99.9, 99.6, 99.7, symbol=symbol),
        _bar("09:20", 99.7, 100.0, 99.7, 99.9, symbol=symbol),
        _bar("09:25", 99.9, 99.95, 99.75, 99.8, symbol=symbol),
    ]


def _evaluate(candles, *, symbol: str = "NIFTY"):
    strategy = OpeningRangeReversionStrategy()
    return strategy.evaluate_signal(
        candles, len(candles) - 1, {"lot_size": 65}, underlying_symbol=symbol
    )


# ---------------------------------------------------------------------------
# Signal geometry
# ---------------------------------------------------------------------------

def test_failed_upside_break_fades_with_pe():
    candles = _opening_range() + [_bar("09:30", 99.9, 100.2, 99.85, 99.95)]
    signal = _evaluate(candles)

    assert signal.allowed is True
    assert signal.option_type == "PE"
    assert signal.variant == "bearish"
    assert signal.entry_price == pytest.approx(99.95)
    # Invalidation sits above the range high; target sits below the entry.
    assert signal.stop_loss == pytest.approx(100.0 + 0.25 * 0.4)
    assert signal.stop_loss > signal.entry_price
    assert signal.target < signal.entry_price
    assert signal.or_high == pytest.approx(100.0)
    assert signal.or_low == pytest.approx(99.6)
    assert signal.or_width == pytest.approx(0.4)


def test_failed_downside_break_fades_with_ce():
    candles = _opening_range() + [_bar("09:30", 99.7, 99.75, 99.5, 99.65)]
    signal = _evaluate(candles)

    assert signal.allowed is True
    assert signal.option_type == "CE"
    assert signal.variant == "bullish"
    assert signal.stop_loss == pytest.approx(99.6 - 0.25 * 0.4)
    assert signal.stop_loss < signal.entry_price
    assert signal.target > signal.entry_price


def test_clean_breakout_is_not_a_reversion():
    """A break that closes outside the range belongs to ORB, not ORRS."""
    candles = _opening_range() + [_bar("09:30", 99.9, 100.2, 99.85, 100.15)]
    assert _evaluate(candles).allowed is False


def test_poke_without_reclaim_is_rejected():
    candles = _opening_range() + [_bar("09:30", 99.9, 100.0, 99.85, 99.99)]
    assert _evaluate(candles).allowed is False


def test_bar_inside_the_range_never_signals():
    candles = _opening_range() + [_bar("09:30", 99.9, 99.95, 99.75, 99.85)]
    assert _evaluate(candles).allowed is False


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def test_range_too_narrow_is_skipped():
    candles = [
        _bar("09:15", 99.80, 99.81, 99.78, 99.80),
        _bar("09:20", 99.80, 99.82, 99.79, 99.81),
        _bar("09:25", 99.81, 99.82, 99.78, 99.80),
        _bar("09:30", 99.80, 99.95, 99.79, 99.80),
    ]
    width_fraction = (99.82 - 99.78) / 99.80
    assert width_fraction < WIDTH_MIN_FRACTION
    assert _evaluate(candles).allowed is False


def test_range_too_wide_is_skipped():
    candles = [
        _bar("09:15", 99.80, 100.40, 99.20, 99.80),
        _bar("09:20", 99.80, 100.40, 99.20, 99.85),
        _bar("09:25", 99.85, 100.00, 99.30, 99.80),
        _bar("09:30", 99.80, 100.30, 99.75, 99.79),
    ]
    width_fraction = (100.40 - 99.20) / 99.80
    assert width_fraction > WIDTH_MAX_FRACTION
    assert _evaluate(candles).allowed is False


def test_entry_after_cutoff_is_rejected():
    candles = _opening_range() + [_bar("14:05", 99.9, 100.2, 99.85, 99.95)]
    assert _evaluate(candles).allowed is False
    assert ENTRY_CUTOFF.hour == 14


def test_incomplete_opening_range_is_rejected():
    candles = _opening_range()[:2] + [_bar("09:30", 99.9, 100.2, 99.85, 99.95)]
    assert _evaluate(candles).allowed is False


def test_pre_range_bar_is_rejected():
    candles = _opening_range()[:2] + [_bar("09:25", 99.9, 100.2, 99.85, 99.95)]
    assert _evaluate(candles).allowed is False


def test_other_underlying_candles_are_ignored():
    candles = _opening_range() + [_bar("09:30", 99.9, 100.2, 99.85, 99.95)]
    assert _evaluate(candles, symbol="BANKNIFTY").allowed is False


def test_previous_session_range_cannot_be_reused():
    """Candles from another day must never form today's opening range."""
    stale = [_bar(clock, *levels, symbol="NIFTY") for clock, levels in (
        ("09:15", (99.8, 99.9, 99.6, 99.7)),
        ("09:20", (99.7, 100.0, 99.7, 99.9)),
        ("09:25", (99.9, 99.95, 99.75, 99.8)),
    )]
    for bar in stale:
        bar["timestamp"] = bar["timestamp"].replace(DATE, "2026-11-10")
    candles = stale + [_bar("09:30", 99.9, 100.2, 99.85, 99.95)]
    assert _evaluate(candles).allowed is False


# ---------------------------------------------------------------------------
# Shared contract with the replay / execution layers
# ---------------------------------------------------------------------------

def test_exit_profile_matches_the_validated_configuration():
    assert POKE_FRACTION == pytest.approx(0.15)
    assert ORRS_EXIT_PROFILE["target_fraction"] == pytest.approx(0.22)
    assert ORRS_EXIT_PROFILE["stop_fraction"] == pytest.approx(0.32)
    assert ORRS_EXIT_PROFILE["time_stop_minutes"] == 45
    assert ORRS_EXIT_PROFILE["max_per_index_per_session"] == 2


def test_pipeline_emits_a_candidate_with_the_expected_name():
    candles = _opening_range() + [_bar("09:30", 99.9, 100.2, 99.85, 99.95)]
    candidates = CandidatePipeline.evaluate_strategy_signals_pure(
        candles,
        "opening_range_reversion",
        underlying_symbol="NIFTY",
        contract_specs={"lot_size": 65, "strike_interval": 50.0},
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.strategy_name == "OpeningRangeReversion"
    assert candidate.option_type == "PE"
    assert candidate.direction == "bearish"
    assert candidate.stop_loss > candidate.entry_price
