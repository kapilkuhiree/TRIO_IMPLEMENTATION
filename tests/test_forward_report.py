"""Tests for the forward paper-trading monitor (scripts/forward_report.py)."""

from scripts.forward_report import (
    GATE_REASON,
    build_report,
    filter_window,
    format_report,
    load_records,
)


def _record(event, ts, *, strategy="orb", pnl=None, reason=None,
            underlying="NIFTY", mode="paper", asset="options", **extra):
    record = {
        "ts": ts,
        "event": event,
        "mode": mode,
        "asset": asset,
        "strategy": strategy,
        "option_underlying": underlying,
    }
    if pnl is not None:
        record["pnl"] = pnl
        record["closed_at"] = ts
    if reason is not None:
        record["reason"] = reason
    record.update(extra)
    return record


def test_sleeve_stats_win_rate_and_profit_factor():
    records = [
        _record("exit", "2026-11-10T10:00:00+05:30", strategy="orb", pnl=1000),
        _record("exit", "2026-11-10T11:00:00+05:30", strategy="orb", pnl=500),
        _record("exit", "2026-11-10T12:00:00+05:30", strategy="orb", pnl=-200),
        _record("exit", "2026-11-10T13:00:00+05:30",
                strategy="mean_reversion", pnl=-100),
    ]
    report = build_report(records)
    orb = report["sleeves"]["orb"]
    assert orb["trades"] == 3
    assert orb["wins"] == 2
    assert round(orb["win_rate"], 1) == 66.7
    assert orb["net"] == 1300
    assert round(orb["profit_factor"], 2) == 7.5
    assert orb["max_dd"] == -200
    assert report["overall"]["trades"] == 4
    assert report["overall"]["net"] == 1200


def test_gate_blocks_collapse_repeated_scans_within_a_bar():
    records = [
        # Three scans of the same ORB signal inside the 10:00-10:05 bar.
        _record("skip", "2026-11-10T10:00:05+05:30", reason=GATE_REASON),
        _record("skip", "2026-11-10T10:00:15+05:30", reason=GATE_REASON),
        _record("skip", "2026-11-10T10:04:55+05:30", reason=GATE_REASON),
        # A genuinely different signal in the next bar.
        _record("skip", "2026-11-10T10:05:05+05:30", reason=GATE_REASON),
        # ORB-Retest has a 09:45 floor, so this is a separate sleeve.
        _record("skip", "2026-11-10T09:50:00+05:30", strategy="orb_retest",
                reason=GATE_REASON),
    ]
    report = build_report(records)
    gate = report["gate_blocks"]
    assert gate["raw"] == 5
    assert gate["unique"] == 3
    assert gate["by_sleeve"] == {"orb": 2, "orb_retest": 1}
    assert gate["by_underlying"] == {"NIFTY": 3}
    assert report["skips"]["raw"] == 5
    assert report["skips"]["unique"] == 3


def test_other_skip_reasons_are_not_counted_as_gate_blocks():
    records = [
        _record("skip", "2026-11-10T10:30:00+05:30",
                reason="options daily-entry limit reached"),
        _record("skip", "2026-11-10T10:31:00+05:30",
                reason="options mark is unresolved; new entries are blocked"),
    ]
    report = build_report(records)
    assert report["gate_blocks"]["unique"] == 0
    assert report["skips"]["reasons"] == {
        "options daily-entry limit reached": 1,
        "options mark is unresolved; new entries are blocked": 1,
    }


def test_test_mode_rows_are_excluded_by_default(tmp_path):
    log = tmp_path / "trade_log.jsonl"
    log.write_text(
        "\n".join([
            '{"ts": "2026-11-10T10:00:00+05:30", "event": "exit", '
            '"mode": "paper", "asset": "options", "strategy": "orb", "pnl": 100}',
            '{"ts": "2026-11-10T10:05:00+05:30", "event": "exit", '
            '"mode": "test", "asset": "options", "strategy": "orb", "pnl": 9999}',
            "not json",
        ]) + "\n",
        encoding="utf-8",
    )
    live = load_records(log)
    assert len(live) == 1
    assert build_report(live)["overall"]["net"] == 100

    everything = load_records(log, include_test=True)
    assert len(everything) == 2
    assert build_report(everything)["overall"]["net"] == 10099


def test_window_filter_uses_ist_dates():
    records = [
        _record("exit", "2026-11-10T10:00:00+05:30", pnl=100),
        _record("exit", "2026-11-11T10:00:00+05:30", pnl=200),
        _record("exit", "2026-11-12T10:00:00+05:30", pnl=300),
        # 23:30 UTC on the 9th is 05:00 IST on the 10th.
        _record("exit", "2026-11-09T23:30:00+00:00", pnl=400),
    ]
    only_10th = filter_window(records, date="2026-11-10")
    assert len(only_10th) == 2
    ranged = filter_window(records, since="2026-11-11", until="2026-11-12")
    assert len(ranged) == 2


def test_orders_without_a_matching_exit_are_reported_open():
    records = [
        _record("order", "2026-11-10T09:55:00+05:30", position_id="A"),
        _record("exit", "2026-11-10T10:30:00+05:30", pnl=10, position_id="A"),
        _record("order", "2026-11-10T11:00:00+05:30", position_id="B"),
    ]
    report = build_report(records)
    assert report["orders"]["count"] == 2
    assert report["orders"]["unclosed"] == 1


def test_report_formats_without_crashing():
    report = build_report([
        _record("exit", "2026-11-10T10:00:00+05:30", pnl=100),
        _record("skip", "2026-11-10T10:01:00+05:30", reason=GATE_REASON),
    ])
    text = format_report(report, log_path="output/trade_log.jsonl")
    assert "FORWARD OPTIONS PAPER REPORT" in text
    assert "PER-SLEEVE PERFORMANCE" in text
    assert "ENTRY-TIME GATE" in text
    assert "orb=1" in text


def test_shadow_signals_are_summarised_but_never_counted_as_trades():
    records = [
        # Same shadow signal re-logged inside one 5-minute bar.
        _record("shadow_signal", "2026-11-10T10:00:05+05:30",
                strategy="mean_reversion", strike=100.0, kind="CE",
                vwap_would_block=True, collides_with_tradable=False),
        _record("shadow_signal", "2026-11-10T10:04:55+05:30",
                strategy="mean_reversion", strike=100.0, kind="CE",
                vwap_would_block=True, collides_with_tradable=False),
        _record("shadow_signal", "2026-11-10T10:05:05+05:30",
                strategy="mean_reversion", strike=100.0, kind="CE",
                vwap_would_block=False, collides_with_tradable=True),
    ]
    report = build_report(records)
    shadow = report["shadow"]
    assert shadow["raw"] == 3
    assert shadow["unique"] == 2
    assert shadow["by_strategy"] == {"mean_reversion": 2}
    assert shadow["vwap_blocked"] == 1
    assert shadow["contract_collisions"] == 1
    assert report["overall"]["trades"] == 0
    assert report["orders"]["count"] == 0

    text = format_report(report)
    assert "SHADOW SIGNALS (log-only; never traded)" in text
    assert "mean_reversion=2" in text
