"""Tests for the shadow-only short-premium sleeve."""

from __future__ import annotations

import json

import pytest

from src.shadow_short_premium import (
    LOT_SIZE,
    SpreadPlan,
    append_record,
    plan_bull_put_spread,
    read_records,
    settle,
    summarise,
)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def test_plan_picks_strike_nearest_below_target():
    quotes = {24000.0: 6.0, 24500.0: 20.0, 24600.0: 25.0, 24400.0: 12.0}
    plan = plan_bull_put_spread(
        "2026-11-11", "2026-11-17", 25000.0, quotes,
        otm=0.015, width=100.0,
    )
    assert plan is not None
    # 25000 * 0.985 = 24625 -> highest strike <= target is 24600
    assert plan.short_strike == 24600.0
    assert plan.long_strike == 24500.0
    assert plan.credit == pytest.approx(5.0)


def test_plan_returns_none_when_no_strike_below_target():
    quotes = {24900.0: 10.0, 25000.0: 15.0}
    assert plan_bull_put_spread(
        "2026-11-11", "2026-11-17", 25000.0, quotes, otm=0.015
    ) is None


def test_plan_returns_none_when_long_leg_missing():
    quotes = {24600.0: 25.0}
    assert plan_bull_put_spread(
        "2026-11-11", "2026-11-17", 25000.0, quotes,
        otm=0.015, width=100.0,
    ) is None


def test_plan_rejects_non_positive_credit():
    quotes = {24600.0: 10.0, 24500.0: 12.0}
    assert plan_bull_put_spread(
        "2026-11-11", "2026-11-17", 25000.0, quotes,
        otm=0.015, width=100.0,
    ) is None


def test_plan_rejects_credit_at_or_above_width():
    quotes = {24600.0: 150.0, 24500.0: 10.0}
    assert plan_bull_put_spread(
        "2026-11-11", "2026-11-17", 25000.0, quotes,
        otm=0.015, width=100.0,
    ) is None


def test_plan_rejects_bad_inputs():
    assert plan_bull_put_spread("d", "e", 0.0, {24000.0: 1.0}) is None
    assert plan_bull_put_spread("d", "e", 25000.0, {}) is None
    assert plan_bull_put_spread(
        "d", "e", 25000.0, {24600.0: 1.0, 24500.0: 0.5}, width=0.0
    ) is None


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------


def _plan(credit=20.0, width=100.0, cost=2.0):
    return SpreadPlan(
        underlying="NIFTY", day="2026-11-11", expiry="2026-11-17",
        spot=25000.0, short_strike=24600.0, long_strike=24500.0,
        width=width, credit=credit, cost_points=cost,
    )


def test_settle_profitable_when_spread_decays():
    plan = _plan(credit=20.0)
    result = settle(plan, exit_value=10.0)
    assert result["gross_points"] == pytest.approx(10.0)
    assert result["net_points"] == pytest.approx(8.0)
    assert result["net_inr"] == pytest.approx(8.0 * LOT_SIZE)
    assert result["green"] is True


def test_settle_loses_when_spread_expands():
    plan = _plan(credit=20.0)
    result = settle(plan, exit_value=70.0)
    assert result["gross_points"] == pytest.approx(-50.0)
    assert result["net_points"] == pytest.approx(-52.0)
    assert result["green"] is False


def test_settle_break_even_win_rate_math():
    plan = _plan(credit=20.0, width=100.0)
    result = settle(plan, exit_value=20.0)
    # win 20, lose 80 -> break-even 80%
    assert result["break_even_win_pct"] == pytest.approx(80.0)


def test_settle_scales_with_lots():
    plan = _plan(credit=20.0)
    one = settle(plan, exit_value=10.0, lots=1)
    three = settle(plan, exit_value=10.0, lots=3)
    assert three["net_inr"] == pytest.approx(3 * one["net_inr"])


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def _record(day="2026-11-11", short=24600.0, net=8.0):
    return {
        "kind": "shadow_short_premium", "strategy": "bull_put_spread",
        "day": day, "expiry": "2026-11-17", "short_strike": short,
        "long_strike": short - 100.0, "credit": 20.0, "exit_value": 12.0,
        "net_points": net, "net_inr": net * LOT_SIZE, "green": net > 0,
    }


def test_append_is_idempotent(tmp_path):
    ledger = tmp_path / "shadow.jsonl"
    assert append_record(_record(), ledger) is True
    assert append_record(_record(), ledger) is False
    assert len(read_records(ledger)) == 1


def test_append_keeps_distinct_sessions(tmp_path):
    ledger = tmp_path / "shadow.jsonl"
    assert append_record(_record(day="2026-11-10"), ledger) is True
    assert append_record(_record(day="2026-11-11"), ledger) is True
    assert append_record(_record(day="2026-11-11", short=24500.0), ledger) is True
    assert len(read_records(ledger)) == 3


def test_read_records_skips_malformed_lines(tmp_path):
    ledger = tmp_path / "shadow.jsonl"
    ledger.write_text(
        json.dumps(_record()) + "\nnot json\n\n" + json.dumps(
            _record(day="2026-11-10")) + "\n",
        encoding="utf-8",
    )
    assert len(read_records(ledger)) == 2


def test_summarise_green_rate_and_totals():
    records = [
        {"net_points": 8.0}, {"net_points": 5.0},
        {"net_points": -52.0}, {"net_points": -30.0},
    ]
    stats = summarise(records)
    assert stats["sessions"] == 4
    assert stats["green"] == 2
    assert stats["green_pct"] == pytest.approx(50.0)
    assert stats["net_points"] == pytest.approx(-69.0)
    assert stats["net_inr"] == pytest.approx(-69.0 * LOT_SIZE)
    assert stats["max_loss_streak"] == 2


def test_summarise_empty():
    assert summarise([]) == {}
