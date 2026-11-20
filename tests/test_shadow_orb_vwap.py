"""Tests for the shadow-only ORB + true-VWAP sleeve."""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pytest

from src.shadow_orb_vwap import (
    DEFAULT_STOP_POINTS,
    IST,
    VARIANTS,
    FuturesUnavailable,
    add_indicators,
    append_record,
    compute_session_vwap,
    evaluate_session,
    group_sessions,
    normalise_bars,
    promotion_status,
    read_records,
    session_is_complete,
    summarise,
)

DAY = "2026-11-11"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _session(close_by_minute, *, volume=1000, day=DAY):
    """Build a full 09:15-15:25 session with symmetric OHLC around close."""
    start = datetime.fromisoformat(f"{day}T09:15:00+05:30")
    bars = []
    previous = None
    for index in range(75):
        stamp = start + timedelta(minutes=5 * index)
        minute = stamp.hour * 60 + stamp.minute
        close = float(close_by_minute(minute))
        open_ = close if previous is None else previous
        bars.append({
            "timestamp": stamp,
            "open": open_,
            "high": max(open_, close) + 0.5,
            "low": min(open_, close) - 0.5,
            "close": close,
            "volume": float(volume()),
        })
        previous = close
    return bars


def test_partial_session_is_not_complete():
    full = _session(_trend_path, volume=lambda: 1000)
    assert session_is_complete(full) is True
    partial = [bar for bar in full
               if bar["timestamp"].time() < time(15, 10)]
    assert session_is_complete(partial) is False
    assert session_is_complete([]) is False


def _trend_path(minute):
    """OR ~100/99, two-close breakdown after 10:00, then straight down to 92."""
    if minute == 9 * 60 + 15:
        return 100.0
    if minute == 9 * 60 + 20:
        return 101.0
    if minute == 9 * 60 + 25:
        return 99.0
    if minute < 10 * 60:
        return 100.0
    if minute == 10 * 60:
        return 97.0
    if minute == 10 * 60 + 5:
        return 96.5
    return 92.0


def _retest_path(minute):
    """Same breakdown, but pull back to 98.2 before resuming at 97.8."""
    if minute == 10 * 60 + 10:
        return 98.2
    if minute == 10 * 60 + 15:
        return 97.8
    return _trend_path(minute)


def _zero_volume():
    return 0


def _flat_volume():
    return 1000


# ---------------------------------------------------------------------------
# True VWAP
# ---------------------------------------------------------------------------


def test_true_vwap_is_volume_weighted_not_equal_weighted():
    bars = [
        {"high": 101, "low": 99, "close": 100, "volume": 900},
        {"high": 111, "low": 109, "close": 110, "volume": 100},
    ]
    result = compute_session_vwap(bars)
    assert result.valid is True
    assert result.reason == ""
    assert result.value == pytest.approx(
        ((100 * 900) + (110 * 100)) / 1000
    )
    # The equal-weighted mean would be 105; genuine VWAP is 101.
    assert result.value != pytest.approx(105.0)


def test_zero_volume_fails_closed_without_fallback():
    bars = [
        {"high": 101, "low": 99, "close": 100, "volume": 0},
        {"high": 102, "low": 100, "close": 101, "volume": 0},
    ]
    result = compute_session_vwap(bars)
    assert result.valid is False
    assert result.value is None
    assert result.reason == "no_volume"


def test_sparse_volume_fails_closed():
    bars = [
        {"high": 101, "low": 99, "close": 100, "volume": 1000},
        {"high": 101, "low": 99, "close": 100, "volume": 0},
        {"high": 101, "low": 99, "close": 100, "volume": 0},
        {"high": 101, "low": 99, "close": 100, "volume": 0},
    ]
    result = compute_session_vwap(bars)
    assert result.valid is False
    assert result.reason == "low_volume_coverage"
    assert result.coverage == pytest.approx(0.25)


def test_empty_session_reports_reason():
    result = compute_session_vwap([])
    assert result.valid is False
    assert result.reason == "no_session_bars"


# ---------------------------------------------------------------------------
# Evaluation and fail-closed behaviour
# ---------------------------------------------------------------------------


def test_variant_a_trades_without_volume_variant_b_refuses():
    bars = _session(_trend_path, volume=_zero_volume)
    a = evaluate_session(bars, day=DAY, variant="A", stop_points=2.0,
                         cost_points=0.1)
    b = evaluate_session(bars, day=DAY, variant="B", stop_points=2.0,
                         cost_points=0.1)
    assert a["status"] == "ok"
    assert a["trade"]["direction"] == "short"
    assert b["status"] == "data_issue"
    assert b["data_issue"] == "no_volume"
    assert "trade" not in b


def test_variant_b_trades_on_true_vwap_and_matches_variant_a():
    bars = _session(_trend_path, volume=_flat_volume)
    a = evaluate_session(bars, day=DAY, variant="A", stop_points=2.0,
                         cost_points=0.1)
    b = evaluate_session(bars, day=DAY, variant="B", stop_points=2.0,
                         cost_points=0.1)
    assert a["status"] == "ok" and b["status"] == "ok"
    assert b["trade"]["entry"] == a["trade"]["entry"]
    assert b["trade"]["entry_time"] == a["trade"]["entry_time"]
    assert b["vwap_at_entry"] is not None
    assert b["trade"]["entry"] < b["vwap_at_entry"]


def test_opening_range_is_the_first_three_bars_only():
    bars = _session(_trend_path, volume=_flat_volume)
    record = evaluate_session(bars, day=DAY, variant="A", stop_points=2.0,
                              cost_points=0.1)
    assert record["or_high"] == pytest.approx(101.5)
    assert record["or_low"] == pytest.approx(98.5)


def test_variant_c_waits_for_retest_and_enters_later_than_a():
    bars = _session(_retest_path, volume=_flat_volume)
    a = evaluate_session(bars, day=DAY, variant="A", stop_points=2.0,
                         cost_points=0.1)
    c = evaluate_session(bars, day=DAY, variant="C", stop_points=2.0,
                         cost_points=0.1)
    assert a["status"] == "ok"
    assert c["status"] == "ok"
    assert c["trade"]["entry_time"] > a["trade"]["entry_time"]
    assert a["trade"]["entry_time"] == "10:05"
    assert c["trade"]["entry_time"] == "10:15"


def test_variant_d_records_without_error():
    bars = _session(_trend_path, volume=_flat_volume)
    d = evaluate_session(bars, day=DAY, variant="D", stop_points=2.0,
                         cost_points=0.1)
    assert d["variant"] == "D"
    assert d["status"] in {"ok", "no_signal"}


def test_no_signal_when_price_stays_inside_range():
    bars = _session(lambda minute: 100.0, volume=_flat_volume)
    record = evaluate_session(bars, day=DAY, variant="A", stop_points=2.0,
                              cost_points=0.1)
    assert record["status"] == "no_signal"
    assert "trade" not in record


def test_stop_and_target_geometry():
    bars = _session(_trend_path, volume=_flat_volume)
    record = evaluate_session(bars, day=DAY, variant="A", reward_risk=2.0,
                              stop_points=2.0, cost_points=0.1)
    trade = record["trade"]
    assert trade["direction"] == "short"
    # entry 96.5, stop 98.5, target 92.5 (2R)
    assert trade["entry"] == pytest.approx(96.5)
    assert trade["stop"] == pytest.approx(98.5)
    assert trade["target"] == pytest.approx(92.5)
    assert trade["exit_reason"] == "target"
    assert trade["gross_points"] == pytest.approx(4.0)
    assert trade["net_points"] == pytest.approx(3.9)


def test_costs_reduce_net_but_not_gross():
    bars = _session(_trend_path, volume=_flat_volume)
    record = evaluate_session(bars, day=DAY, variant="A", stop_points=2.0,
                              cost_points=0.5)
    trade = record["trade"]
    assert trade["gross_points"] - trade["net_points"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Normalisation / grouping
# ---------------------------------------------------------------------------


def test_normalise_bars_sorts_and_keeps_volume():
    rows = [
        {"date": f"{DAY} 10:00:00+05:30", "open": 2, "high": 3, "low": 1,
         "close": 2.5, "volume": 500},
        {"date": f"{DAY} 09:15:00+05:30", "open": 1, "high": 2, "low": 1,
         "close": 1.5, "volume": 100},
    ]
    bars = normalise_bars(rows)
    assert len(bars) == 2
    assert bars[0]["timestamp"] < bars[1]["timestamp"]
    assert bars[0]["volume"] == 100


def test_group_sessions_drops_out_of_session_bars():
    bars = [
        {"timestamp": datetime.fromisoformat(f"{DAY}T08:00:00+05:30"),
         "open": 1, "high": 1, "low": 1, "close": 1, "volume": 10},
        {"timestamp": datetime.fromisoformat(f"{DAY}T10:00:00+05:30"),
         "open": 1, "high": 1, "low": 1, "close": 1, "volume": 10},
    ]
    grouped = group_sessions(bars)
    assert list(grouped) == [DAY]
    assert len(grouped[DAY]) == 1


def test_add_indicators_is_causal():
    bars = _session(_trend_path, volume=_flat_volume)
    enriched = add_indicators(bars)
    assert enriched[0]["ema20"] == pytest.approx(enriched[0]["close"])
    assert enriched[0]["ema20_5ago"] is None
    assert enriched[-1]["atr"] > 0


# ---------------------------------------------------------------------------
# Ledger and promotion gate
# ---------------------------------------------------------------------------


def test_ledger_is_idempotent(tmp_path):
    bars = _session(_trend_path, volume=_flat_volume)
    record = evaluate_session(bars, day=DAY, variant="A", stop_points=2.0,
                              cost_points=0.1)
    ledger = tmp_path / "ledger.jsonl"
    assert append_record(record, ledger) is True
    assert append_record(record, ledger) is False
    assert len(read_records(ledger)) == 1


def test_summarise_reports_required_metrics():
    bars = _session(_trend_path, volume=_flat_volume)
    records = [
        evaluate_session(bars, day=DAY, variant="A", stop_points=2.0,
                         cost_points=0.1)
    ]
    stats = summarise(records, variant="A")
    assert stats["trades"] == 1
    assert stats["wins"] == 1
    assert stats["win_pct"] == 100.0
    assert stats["net_points"] > 0
    assert stats["avg_win_points"] > 0
    assert stats["profit_factor"] is None  # no losses yet -> undefined


def test_promotion_gate_ignores_backfilled_sessions():
    bars = _session(_trend_path, volume=_flat_volume)
    records = []
    for index in range(10):
        day = f"2026-11-{index + 1:02d}"
        item = evaluate_session(bars, day=DAY, variant="A", stop_points=2.0,
                                cost_points=0.1)
        item["day"] = day
        item["source"] = "backfill"
        records.append(item)
    status = promotion_status(records, variant="A")
    assert status["eligible"] is False
    assert status["checks"]["forward_sessions"]["met"] is False


def test_promotion_gate_can_be_met_with_forward_evidence():
    bars = _session(_trend_path, volume=_flat_volume)
    records = []
    for index in range(10):
        day = f"2026-11-{index + 1:02d}"
        item = evaluate_session(bars, day=DAY, variant="A", stop_points=2.0,
                                cost_points=0.1)
        item["day"] = day
        item["source"] = "forward"
        records.append(item)
    status = promotion_status(records, variant="A")
    assert status["eligible"] is True
    assert all(check["met"] for check in status["checks"].values())


def test_promotion_gate_fails_on_losing_book():
    bars = _session(_trend_path, volume=_flat_volume)
    records = []
    for index in range(10):
        item = evaluate_session(bars, day=DAY, variant="A",
                                stop_points=2.0, cost_points=99.0)
        item["day"] = f"2026-11-{index + 1:02d}"
        item["source"] = "forward"
        records.append(item)
    status = promotion_status(records, variant="A")
    assert status["eligible"] is False
    assert status["checks"]["positive_net"]["met"] is False


# ---------------------------------------------------------------------------
# Data adapter contract
# ---------------------------------------------------------------------------


def test_futures_unavailable_is_importable():
    assert issubclass(FuturesUnavailable, RuntimeError)


def test_variants_are_the_pre_registered_specifications():
    # A-D are the original four; E is the per-leg variant added after the
    # audit showed repeats (not VWAP) were the live book's real defect.
    assert VARIANTS == ("A", "B", "C", "D", "E")
    assert DEFAULT_STOP_POINTS > 0


# ---------------------------------------------------------------------------
# Variant E: one entry per leg, re-arm allowed
# ---------------------------------------------------------------------------


def _leg_path(minute):
    """Break, exit at target, re-arm inside the range, break again."""
    path = {
        10 * 60: 97.0,
        10 * 60 + 5: 96.5,
        10 * 60 + 10: 94.0,
        10 * 60 + 15: 99.0,
        10 * 60 + 20: 97.0,
        10 * 60 + 25: 96.0,
    }
    if minute in path:
        return path[minute]
    if minute > 10 * 60 + 25:
        return 92.0
    if minute == 9 * 60 + 15:
        return 100.0
    if minute == 9 * 60 + 20:
        return 101.0
    if minute == 9 * 60 + 25:
        return 99.0
    return 100.0


def test_variant_e_refuses_a_repeat_without_a_rearm():
    bars = _session(_trend_path, volume=_flat_volume)
    record = evaluate_session(bars, day=DAY, variant="E", stop_points=1.0,
                              cost_points=0.1, max_trades=0)
    assert record["status"] == "ok"
    # The breakdown runs to 92 and never closes back inside 98.5-101.5, so
    # there is one leg and exactly one trade.
    assert len(record["trades"]) == 1
    assert record["trades"][0]["direction"] == "short"


def test_variant_e_takes_a_second_leg_after_a_rearm():
    bars = _session(_leg_path, volume=_flat_volume)
    record = evaluate_session(bars, day=DAY, variant="E", stop_points=1.0,
                              cost_points=0.1, max_trades=0)
    assert [trade["entry_time"] for trade in record["trades"]] == [
        "10:05", "10:25",
    ]
    assert record["trades"][0]["direction"] == "short"
    assert record["trades"][1]["direction"] == "short"


def test_variant_e_respects_the_daily_cap():
    bars = _session(_leg_path, volume=_flat_volume)
    record = evaluate_session(bars, day=DAY, variant="E", stop_points=1.0,
                              cost_points=0.1, max_trades=1)
    assert len(record["trades"]) == 1
    assert record["max_trades"] == 1


def test_variant_e_is_fail_closed_on_zero_volume():
    bars = _session(_trend_path, volume=_zero_volume)
    record = evaluate_session(bars, day=DAY, variant="E", stop_points=1.0,
                              cost_points=0.1)
    assert record["status"] == "data_issue"
    assert record["data_issue"] == "no_volume"
    assert "trades" not in record


def test_summarise_flattens_multi_leg_records():
    bars = _session(_leg_path, volume=_flat_volume)
    record = evaluate_session(bars, day=DAY, variant="E", stop_points=1.0,
                              cost_points=0.1, max_trades=0) | {
        "source": "backfill"
    }
    stats = summarise([record], variant="E", source="backfill")
    assert stats["trades"] == 2
    assert stats["net_points"] == pytest.approx(
        record["trades"][0]["net_points"] + record["trades"][1]["net_points"],
        abs=0.01,
    )
