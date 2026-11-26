"""Tests for the ORB-SMT sleeve and the SMT structure gate.

The sleeve is a strict subset of plain ORB: it can only reject a signal the
parent produced, never create one.  These tests pin that property plus the
fail-closed behaviour of the structure gate.
"""

from datetime import datetime, timedelta, timezone

import pytest

from candidate_pipeline import CandidatePipeline
from strategies.orb_smt_strategy import (
    OrbSmtStrategy,
    SMT_LOOKBACK_BARS,
    SMT_MIN_SCORE,
    SMT_SWINGS,
    smt_blocks,
    smt_structure,
)

IST = timezone(timedelta(hours=5, minutes=30))
SPECS = {"lot_size": 65, "strike_interval": 50}


def _candles(*, bullish=True, bars=20, flat=False):
    """09:15-09:30 opening range, then a trending or flat session."""
    base = datetime(2026, 11, 23, 9, 15, tzinfo=IST)
    rows = []
    for i in range(3):
        rows.append({
            "timestamp": (base + timedelta(minutes=5 * i)).isoformat(),
            "open": 105.0, "high": 110.0, "low": 100.0, "close": 105.0,
            "volume": 1000, "exchange": "NSE",
            "underlying_symbol": "NIFTY", "instrument_type": "index",
        })
    for i in range(3, bars):
        step = i - 3
        if flat:
            o, h, lo, c = 105.0, 110.0, 100.0, 105.0
        elif bullish:
            o = 110.0 + step * 3
            c = o + 2
            h, lo = c + 1, o - 1
        else:
            o = 100.0 - step * 3
            c = o - 2
            h, lo = o + 1, c - 1
        rows.append({
            "timestamp": (base + timedelta(minutes=5 * i)).isoformat(),
            "open": o, "high": h, "low": lo, "close": c,
            "volume": 1000, "exchange": "NSE",
            "underlying_symbol": "NIFTY", "instrument_type": "index",
        })
    return rows


# ---------------------------------------------------------------------------
# smt_structure
# ---------------------------------------------------------------------------

def test_smt_allows_rising_structure_for_bullish():
    allowed, reason, detail = smt_structure(_candles(bullish=True), "bullish")
    assert allowed is True
    assert reason == ""
    assert detail["score"] == 2
    assert detail["structure"] == "bullish"


def test_smt_allows_falling_structure_for_bearish():
    allowed, reason, detail = smt_structure(_candles(bullish=False), "bearish")
    assert allowed is True
    assert detail["structure"] == "bearish"


def test_smt_rejects_rising_structure_for_bearish():
    allowed, reason, _ = smt_structure(_candles(bullish=True), "bearish")
    assert allowed is False
    assert reason.startswith("smt_structure_against_trend")


def test_smt_rejects_falling_structure_for_bullish():
    allowed, reason, _ = smt_structure(_candles(bullish=False), "bullish")
    assert allowed is False
    assert reason.startswith("smt_structure_against_trend")


def test_smt_rejects_flat_market():
    allowed, reason, detail = smt_structure(_candles(flat=True), "bullish")
    assert allowed is False
    assert detail["score"] == 0
    assert detail["structure"] == "mixed"


def test_smt_empty_candles_fails_closed():
    allowed, reason, detail = smt_structure([], "bullish")
    assert allowed is False
    assert reason == "smt_no_candles"
    assert detail["bars"] == 0


def test_smt_insufficient_bars_fails_closed():
    allowed, reason, _ = smt_structure(_candles(bars=3)[:2], "bullish")
    assert allowed is False
    assert reason == "smt_insufficient_bars"


def test_smt_non_finite_bar_fails_closed():
    rows = _candles(bullish=True)
    rows[10]["high"] = 0.0
    allowed, reason, _ = smt_structure(rows, "bullish")
    assert allowed is False
    assert reason == "smt_non_finite_bar"


def test_smt_unknown_direction_fails_closed():
    allowed, reason, _ = smt_structure(_candles(bullish=True), "sideways")
    assert allowed is False
    assert reason == "smt_unknown_direction"


def test_smt_min_score_one_is_looser_than_two():
    # A flat-high but rising-low series scores exactly 1.
    rows = _candles(bullish=True)
    for row in rows:
        row["high"] = 110.0
    strict, _, strict_detail = smt_structure(rows, "bullish", min_score=2)
    loose, _, loose_detail = smt_structure(rows, "bullish", min_score=1)
    assert strict_detail["score"] == loose_detail["score"] == 1
    assert strict is False
    assert loose is True


def test_smt_detail_always_populated():
    keys = {"lookback", "swings", "min_score", "bars", "blocks",
            "highs_ascending", "lows_ascending", "score", "direction",
            "structure"}
    for candles, direction in (
        (_candles(bullish=True), "bullish"),
        (_candles(bullish=True), "bearish"),
        ([], "bullish"),
    ):
        _, _, detail = smt_structure(candles, direction)
        assert keys <= set(detail)


def test_smt_blocks_split_contiguously():
    blocks = smt_blocks(_candles(bullish=True, bars=20), 2)
    assert len(blocks) == 2
    # Rising series: second block sits entirely above the first.
    assert blocks[1][0] > blocks[0][0]
    assert blocks[1][1] > blocks[0][1]


# ---------------------------------------------------------------------------
# OrbSmtStrategy
# ---------------------------------------------------------------------------

def test_strategy_allows_when_structure_agrees():
    rows = _candles(bullish=True)
    signal = OrbSmtStrategy(lot_size=65, orb_confirm_closes=2).evaluate_signal(
        rows, len(rows) - 1, SPECS, "NIFTY")
    assert signal.allowed is True
    assert signal.strategy == "orb_smt"
    assert signal.option_type == "CE"
    assert signal.smt is not None and signal.smt["allowed"] is True


def test_strategy_rejects_when_structure_opposes():
    rows = _candles(bullish=False)
    # Force a bullish breakout on falling structure: parent fires, gate blocks.
    rows[-1] = dict(rows[-1], close=200.0, high=201.0, low=199.0)
    signal = OrbSmtStrategy(lot_size=65, orb_confirm_closes=1).evaluate_signal(
        rows, len(rows) - 1, SPECS, "NIFTY")
    assert signal.allowed is False
    assert signal.smt is not None
    assert signal.smt["allowed"] is False
    assert "rejected" in (signal.reasoning or "")


def test_strategy_never_creates_a_signal_the_parent_rejected():
    # Flat market: plain ORB has no breakout, so orb_smt must also be silent.
    rows = _candles(flat=True)
    plain = OrbSmtStrategy(lot_size=65, orb_confirm_closes=1).evaluate_signal(
        rows, len(rows) - 1, SPECS, "NIFTY")
    assert plain.allowed is False
    assert plain.smt is None


def test_strategy_inherits_confirm_close_behaviour():
    rows = _candles(bullish=True)
    # The first post-range bar has only one close beyond the range.
    early = OrbSmtStrategy(lot_size=65, orb_confirm_closes=2).evaluate_signal(
        rows, 3, SPECS, "NIFTY")
    assert early.allowed is False


# ---------------------------------------------------------------------------
# Pipeline dispatch
# ---------------------------------------------------------------------------

def test_pipeline_dispatches_orb_smt():
    rows = _candles(bullish=True)
    generated = CandidatePipeline.evaluate_strategy_signals_pure(
        rows, "orb_smt", underlying_symbol="NIFTY",
        contract_specs=SPECS, index=len(rows) - 1, orb_confirm_closes=2)
    assert len(generated) == 1
    assert generated[0].strategy_name == "ORB-SMT"
    assert generated[0].option_type == "CE"


def test_pipeline_orb_smt_matches_orb_geometry():
    """Same candles: orb_smt may only remove candidates, never add or alter."""
    rows = _candles(bullish=True)
    orb = CandidatePipeline.evaluate_strategy_signals_pure(
        rows, "orb", underlying_symbol="NIFTY",
        contract_specs=SPECS, index=len(rows) - 1, orb_confirm_closes=2)
    smt = CandidatePipeline.evaluate_strategy_signals_pure(
        rows, "orb_smt", underlying_symbol="NIFTY",
        contract_specs=SPECS, index=len(rows) - 1, orb_confirm_closes=2)
    assert len(orb) == len(smt) == 1
    assert orb[0].option_type == smt[0].option_type
    assert orb[0].entry_price == smt[0].entry_price
    assert orb[0].stop_loss == smt[0].stop_loss
    assert orb[0].target == smt[0].target


def test_pipeline_unknown_strategy_still_returns_empty():
    rows = _candles(bullish=True)
    generated = CandidatePipeline.evaluate_strategy_signals_pure(
        rows, "not_a_strategy", underlying_symbol="NIFTY",
        contract_specs=SPECS, index=len(rows) - 1)
    assert generated == []


def test_orb_smt_is_in_allowed_strategies():
    from src.options_intraday_plan import ALLOWED_STRATEGIES
    assert "orb_smt" in ALLOWED_STRATEGIES


def test_orb_smt_canonical_name_mapping():
    from src.options_live_scan import _candidate_strategy_name
    assert _candidate_strategy_name({"strategy": "ORB-SMT"}) == "orb_smt"


def test_defaults_are_importable_and_sane():
    assert SMT_LOOKBACK_BARS == 20
    assert SMT_SWINGS == 2
    assert SMT_MIN_SCORE == 2
