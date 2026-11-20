"""Tests for the live-entry audit against the corrected ORB + true-VWAP rules."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from src.live_vwap_audit import (
    LiveEntry,
    append_record,
    audit_session,
    load_entry_times_from_logs,
    load_live_entries,
    load_live_pnl,
    load_state_entries,
    read_records,
    summarise,
)

DAY = "2026-11-11"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _session(close_by_minute, *, volume=1000, day=DAY):
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
            "volume": float(volume),
        })
        previous = close
    return bars


def _entry(hhmm, *, direction="short", day=DAY, position_id=None,
           strategy="orb"):
    stamp = datetime.fromisoformat(f"{day}T{hhmm}:00+05:30")
    return LiveEntry(
        day=day,
        stamp=stamp,
        strategy=strategy,
        direction=direction,
        position_id=position_id or f"OPT_{hhmm}",
        entry_premium=200.0,
        kind="PE" if direction == "short" else "CE",
        strike=25200.0,
    )


def _breakdown(minute):
    """OR 100/101/99 -> high 101.5, low 98.5; break at 10:05."""
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


def _flat(minute):
    return 100.0


def _rearm(minute):
    """Break at 10:05, close back inside at 10:20, break again at 10:35."""
    if minute == 10 * 60 + 10:
        return 96.0
    if minute == 10 * 60 + 20:
        return 99.5
    if minute == 10 * 60 + 25:
        return 99.8
    if minute == 10 * 60 + 30:
        return 97.5
    if minute == 10 * 60 + 35:
        return 96.0
    if minute == 10 * 60 + 40:
        return 95.5
    if minute > 10 * 60 + 40:
        return 95.0
    return _breakdown(minute)


def _dip_then_pop(minute):
    """Dump below the range before 10:00, pop back above VWAP after the floor."""
    path = {
        9 * 60 + 30: 96.0,
        9 * 60 + 35: 94.0,
        9 * 60 + 40: 92.0,
        9 * 60 + 45: 91.0,
        9 * 60 + 50: 90.0,
        9 * 60 + 55: 90.0,
        10 * 60: 96.0,
        10 * 60 + 5: 96.5,
    }
    if minute in path:
        return path[minute]
    if minute > 10 * 60 + 5:
        return 96.5
    if minute == 9 * 60 + 15:
        return 100.0
    if minute == 9 * 60 + 20:
        return 101.0
    if minute == 9 * 60 + 25:
        return 99.0
    return 100.0


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_first_corrected_entry_survives():
    bars = _session(_breakdown)
    records = audit_session([_entry("10:05")], bars)
    assert len(records) == 1
    row = records[0]
    assert row["classification"] == "survives"
    assert row["orb_confirms"] is True
    assert row["vwap_ok"] is True
    assert row["corrected_entry_time"] == "10:05"
    assert "delay_minutes" not in row


def test_later_entry_on_same_leg_is_duplicate():
    bars = _session(_breakdown)
    records = audit_session([_entry("10:05"), _entry("10:10")], bars)
    assert [row["classification"] for row in records] == [
        "survives", "duplicate_entry",
    ]
    duplicate = records[1]
    assert duplicate["repeat_of"] == "live@10:05"
    assert duplicate["delay_minutes"] == 5


def test_first_live_entry_after_the_signal_is_late():
    bars = _session(_breakdown)
    records = audit_session([_entry("10:10")], bars)
    row = records[0]
    assert row["classification"] == "late_entry"
    assert row["delay_minutes"] == 5


def test_rearmed_leg_is_second_setup_not_duplicate():
    bars = _session(_rearm)
    records = audit_session(
        [_entry("10:05"), _entry("10:35")], bars
    )
    assert records[0]["classification"] == "survives"
    assert records[1]["classification"] == "second_setup"
    assert "one-trade-per-session" in records[1]["detail"]


def test_entry_before_floor_is_outside_window():
    bars = _session(_breakdown)
    records = audit_session([_entry("09:55")], bars)
    assert records[0]["classification"] == "outside_window"


def test_entry_after_cutoff_is_outside_window():
    bars = _session(_breakdown)
    records = audit_session([_entry("14:35")], bars)
    assert records[0]["classification"] == "outside_window"


def test_no_breakout_is_no_confirmation():
    bars = _session(_flat)
    records = audit_session([_entry("10:10")], bars)
    assert records[0]["classification"] == "no_confirmation"
    assert records[0]["orb_confirms"] is False


def test_breakout_on_wrong_vwap_side_is_vwap_blocked():
    bars = _session(_dip_then_pop)
    records = audit_session([_entry("10:00")], bars)
    row = records[0]
    assert row["orb_confirms"] is True
    assert row["vwap_ok"] is False
    assert row["classification"] == "vwap_blocked"


def test_spot_only_signal_is_basis_mismatch():
    futures = _session(_flat)
    spot = _session(_breakdown)
    records = audit_session([_entry("10:10")], futures, spot_bars=spot)
    row = records[0]
    assert row["orb_confirms"] is False
    assert row["spot_confirms"] is True
    assert row["classification"] == "basis_mismatch"
    assert summarise(records)["basis_flags"] == 1


def test_zero_volume_is_data_issue_without_fallback():
    bars = _session(_breakdown, volume=0)
    records = audit_session([_entry("10:05")], bars)
    assert records[0]["classification"] == "data_issue"
    assert records[0]["detail"] == "no_volume"


def test_missing_futures_session_is_data_issue():
    bars = _session(_breakdown, day="2026-11-10")
    records = audit_session([_entry("10:05")], bars)
    assert records[0]["classification"] == "data_issue"
    assert records[0]["detail"] == "no_session_bars"


def test_long_entries_use_the_upper_range():
    def rally(minute):
        if minute == 9 * 60 + 15:
            return 100.0
        if minute == 9 * 60 + 20:
            return 99.0
        if minute == 9 * 60 + 25:
            return 101.0
        if minute < 10 * 60:
            return 100.0
        if minute == 10 * 60:
            return 103.0
        if minute == 10 * 60 + 5:
            return 104.0
        return 108.0

    bars = _session(rally)
    rows = audit_session([_entry("10:05", direction="long")], bars)
    assert rows[0]["classification"] == "survives"
    assert rows[0]["orb_confirms"] is True


def test_mixed_days_are_rejected():
    bars = _session(_breakdown)
    with pytest.raises(ValueError):
        audit_session(
            [_entry("10:05"), _entry("10:10", day="2026-11-10")], bars
        )


def test_pnl_is_attached_when_available():
    bars = _session(_breakdown)
    entry = _entry("10:05")
    rows = audit_session([entry], bars,
                         pnl_by_entry={entry.stamp.isoformat(): 1234.5})
    assert rows[0]["pnl_inr"] == pytest.approx(1234.5)


# ---------------------------------------------------------------------------
# Trade-log reader
# ---------------------------------------------------------------------------


def _write_log(path, rows):
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_loader_dedupes_positions_and_reads_direction(tmp_path):
    log = tmp_path / "trade_log.jsonl"
    _write_log(log, [
        {"ts": "2026-11-11T04:35:20+00:00", "event": "order",
         "asset": "options", "position_id": "P1", "strategy": "orb",
         "signal_action": "SELL", "entry_premium": 210.0, "kind": "PE",
         "strike": 25200.0},
        # exact restart duplicate: same position, same timestamp
        {"ts": "2026-11-11T04:35:20+00:00", "event": "order",
         "asset": "options", "position_id": "P1", "strategy": "orb",
         "signal_action": "SELL", "entry_premium": 210.0},
        {"ts": "2026-11-11T06:00:00+00:00", "event": "order",
         "asset": "options", "position_id": "P2", "strategy": "orb_retest",
         "direction": "bullish", "entry_premium": 150.0},
        {"ts": "2026-11-11T05:00:00+00:00", "event": "scan",
         "asset": "options", "position_id": "P3"},
    ])
    entries = load_live_entries(log)
    assert [entry.position_id for entry in entries] == ["P1", "P2"]
    assert entries[0].direction == "short"
    assert entries[0].day == "2026-11-11"
    assert entries[0].stamp.strftime("%H:%M") == "10:05"
    assert entries[1].direction == "long"


def test_same_contract_reentered_later_is_two_entries(tmp_path):
    log = tmp_path / "trade_log.jsonl"
    _write_log(log, [
        {"ts": "2026-11-11T04:35:20+00:00", "event": "order",
         "asset": "options", "position_id": "P1", "direction": "bearish"},
        {"ts": "2026-11-11T08:50:20+00:00", "event": "order",
         "asset": "options", "position_id": "P1", "direction": "bearish"},
    ])
    entries = load_live_entries(log)
    assert len(entries) == 2
    assert [entry.stamp.strftime("%H:%M") for entry in entries] == ["10:05", "14:20"]


def test_loader_filters_other_underlyings(tmp_path):
    log = tmp_path / "trade_log.jsonl"
    _write_log(log, [
        {"ts": "2026-11-11T04:35:20+00:00", "event": "order",
         "asset": "options", "position_id": "OPT_NIFTY26N1725200PE",
         "signal_underlying": "NIFTY", "direction": "bearish"},
        {"ts": "2026-11-11T05:35:20+00:00", "event": "order",
         "asset": "options", "position_id": "OPT_BANKNIFTY26NOV55700CE",
         "option_underlying": "BANKNIFTY", "direction": "bullish"},
        {"ts": "2026-11-11T06:35:20+00:00", "event": "order",
         "asset": "options", "position_id": "OPT_NIFTY26N1725300PE",
         "direction": "bearish"},
    ])
    entries = load_live_entries(log, underlying="NIFTY")
    assert [entry.position_id for entry in entries] == [
        "OPT_NIFTY26N1725200PE", "OPT_NIFTY26N1725300PE",
    ]
    assert all(entry.underlying == "NIFTY" for entry in entries)


def test_pnl_pairs_each_entry_with_the_next_exit(tmp_path):
    log = tmp_path / "trade_log.jsonl"
    _write_log(log, [
        {"ts": "2026-11-11T04:35:20+00:00", "event": "order",
         "asset": "options", "position_id": "P1", "direction": "bearish"},
        {"ts": "2026-11-11T08:50:20+00:00", "event": "order",
         "asset": "options", "position_id": "P1", "direction": "bearish"},
        {"ts": "2026-11-11T04:51:00+00:00", "event": "exit",
         "asset": "options", "position_id": "P1", "pnl": 100.0},
        {"ts": "2026-11-11T09:28:00+00:00", "event": "exit",
         "asset": "options", "position_id": "P1", "pnl": 250.0},
    ])
    entries = load_live_entries(log)
    pnl = load_live_pnl(entries, log)
    assert pnl[entries[0].stamp.isoformat()] == pytest.approx(100.0)
    assert pnl[entries[1].stamp.isoformat()] == pytest.approx(250.0)


# ---------------------------------------------------------------------------
# Live runtime state reader
# ---------------------------------------------------------------------------


def _state(closed, **extra):
    payload = {"session_date": DAY, "intents": {}, "closed": closed}
    payload.update(extra)
    return payload


def _closed_leg(**overrides):
    row = {
        "asset": "options",
        "position_id": "NIFTY26N1725350CE",
        "direction": "bullish",
        "signal_action": "BUY",
        "strategy": "orb",
        "entry_premium": 208.0,
        "kind": "CE",
        "strike": 25350.0,
        "option_underlying": "NIFTY",
        "pnl": 174.5,
        "closed_at": f"{DAY}T05:07:10+00:00",
    }
    row.update(overrides)
    return row


def _write_state(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_state_loader_reads_closed_legs_with_entry_ts(tmp_path):
    path = tmp_path / f"live_{DAY}.json"
    _write_state(path, _state([
        _closed_leg(entry_ts=f"{DAY}T04:44:00+00:00"),
        _closed_leg(entry_premium=217.65, pnl=-2959.12,
                    entry_ts=f"{DAY}T05:12:12+00:00",
                    closed_at=f"{DAY}T05:43:52+00:00"),
    ]))
    entries, pnl, unresolved = load_state_entries(path)
    assert unresolved == []
    assert [entry.stamp.strftime("%H:%M") for entry in entries] == [
        "10:14", "10:42",
    ]
    assert all(entry.direction == "long" for entry in entries)
    assert entries[0].strategy == "orb"
    assert entries[0].day == DAY
    assert pnl[entries[0].stamp.isoformat()] == pytest.approx(174.5)
    assert pnl[entries[1].stamp.isoformat()] == pytest.approx(-2959.12)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_state_loader_recovers_entry_time_from_the_runtime_log(tmp_path, encoding):
    path = tmp_path / f"live_{DAY}.json"
    _write_state(path, _state([_closed_leg()]))
    log = tmp_path / "session_20261112_err.log"
    line = (
        "[2026-11-12 10:14:00] INFO     trio.options_intraday_runtime "
        "\ufffd ENTRY BUY NIFTY 17-Nov-2026 25350CE | 1 lots x 65 = 65 qty "
        "@ 208.00 | stop 166.40 | target 270.40 | risk 2725.60 | "
        "order 123 | NIFTY26N1725350CE"
    )
    if encoding == "utf-16":
        # PowerShell both re-encodes and wraps the stream at console width.
        text = "\n".join(line[i:i + 70] for i in range(0, len(line), 70))
    else:
        text = line
    log.write_text(text + "\n", encoding=encoding)
    stamps = load_entry_times_from_logs([log])
    entries, _, unresolved = load_state_entries(path, entry_stamps=stamps)
    assert unresolved == []
    assert len(entries) == 1
    assert entries[0].stamp.strftime("%H:%M") == "10:14"


def test_state_loader_uses_a_matching_intent_time(tmp_path):
    path = tmp_path / f"live_{DAY}.json"
    _write_state(path, _state(
        [_closed_leg(closed_at=f"{DAY}T05:43:52+00:00")],
        intents={"NIFTY|17-Nov-2026|25350.0|CE": {
            "position_id": "NIFTY26N1725350CE",
            "created_at": f"{DAY}T05:12:12+00:00",
            "closed_at": f"{DAY}T05:43:52+00:00",
        }},
    ))
    entries, _, unresolved = load_state_entries(path)
    assert unresolved == []
    assert entries[0].stamp.strftime("%H:%M") == "10:42"


def test_state_loader_reports_a_leg_without_an_entry_time(tmp_path):
    """Never substitute the exit time: an un-timeable leg is not audited."""
    path = tmp_path / f"live_{DAY}.json"
    _write_state(path, _state([_closed_leg()]))
    entries, pnl, unresolved = load_state_entries(path)
    assert entries == []
    assert pnl == {}
    assert len(unresolved) == 1
    assert unresolved[0]["position_id"] == "NIFTY26N1725350CE"
    assert unresolved[0]["entry_premium"] == pytest.approx(208.0)


def test_state_loader_filters_other_underlyings(tmp_path):
    path = tmp_path / f"live_{DAY}.json"
    _write_state(path, _state([
        _closed_leg(entry_ts=f"{DAY}T04:44:00+00:00"),
        _closed_leg(position_id="BANKNIFTY26N1755700CE",
                    option_underlying="BANKNIFTY",
                    entry_ts=f"{DAY}T04:46:00+00:00"),
    ]))
    entries, _, _ = load_state_entries(path, underlying="NIFTY")
    assert [entry.position_id for entry in entries] == ["NIFTY26N1725350CE"]


# ---------------------------------------------------------------------------
# Ledger and summary
# ---------------------------------------------------------------------------


def test_ledger_is_idempotent(tmp_path):
    bars = _session(_breakdown)
    rows = audit_session([_entry("10:05")], bars)
    ledger = tmp_path / "audit.jsonl"
    assert append_record(rows[0], ledger) is True
    assert append_record(rows[0], ledger) is False
    assert len(read_records(ledger)) == 1


def test_summary_counts_every_class():
    bars = _session(_breakdown)
    rows = audit_session(
        [_entry("10:05"), _entry("10:10"), _entry("09:55")], bars
    )
    stats = summarise(rows)
    assert stats["entries"] == 3
    assert stats["days"] == 1
    assert stats["counts"]["survives"] == 1
    assert stats["counts"]["duplicate_entry"] == 1
    assert stats["counts"]["outside_window"] == 1
