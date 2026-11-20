"""Tests for the shadow LLM judge report join (no network)."""

from __future__ import annotations

import json

from scripts.llm_report import build_report, load_events


def _verdict(bar_ts, *, status="ok", agrees=True, key_id="k1", latency=1000):
    payload = {"bias": "short", "confidence": 0.8, "reason_code": "vwap_reject"} if status == "ok" else None
    if agrees is None:
        payload = {"bias": "neutral", "confidence": 0.4}
    elif status == "ok":
        payload["agrees"] = agrees
    return {
        "event": "llm_verdict", "bar_ts": bar_ts, "underlying": "NIFTY",
        "strategy": "orb", "direction": "bearish", "status": status,
        "key_id": key_id, "latency_ms": latency, "verdict": payload,
    }


def _order(ts, position_id="p1", strategy="orb"):
    return {
        "event": "order", "ts": ts, "option_underlying": "NIFTY",
        "strategy": strategy, "direction": "bearish", "position_id": position_id,
        "entry_premium": 220.0, "risk_amount": 2900.0,
    }


def _exit(position_id="p1", pnl=1400.0):
    return {"event": "exit", "position_id": position_id, "pnl": pnl,
            "reason": "target", "exit_bid": 300.0}


def _report(tmp_path, rows, **kwargs):
    path = tmp_path / "trade_log.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    return build_report(load_events(path), **kwargs)


def test_verdict_is_joined_to_the_next_matching_order(tmp_path):
    report = _report(tmp_path, [
        _verdict("2026-11-11T13:50:00+05:30"),
        _order("2026-11-11T13:55:00+05:30"),
        _exit(pnl=1400.0),
    ], window_minutes=20)

    assert report["matched_orders"] == 1
    assert report["buckets"]["agreed"]["trades"] == 1
    assert report["buckets"]["agreed"]["pnl"] == 1400.0
    assert report["buckets"]["agreed"]["win_rate"] == 100.0


def test_order_outside_the_window_is_not_matched(tmp_path):
    report = _report(tmp_path, [
        _verdict("2026-11-11T13:50:00+05:30"),
        _order("2026-11-11T14:30:00+05:30"),
        _exit(),
    ], window_minutes=20)

    assert report["matched_orders"] == 0
    assert report["unmatched_signals"] == 1


def test_failed_verdict_is_unusable_not_neutral(tmp_path):
    report = _report(tmp_path, [
        _verdict("2026-11-11T13:50:00+05:30", status="error", agrees=None),
        _verdict("2026-11-11T14:10:00+05:30", status="ok", agrees=None),
        _order("2026-11-11T14:15:00+05:30"),
        _exit(),
    ], window_minutes=20)

    assert report["unusable_verdicts"] == 1
    assert report["neutral_verdicts"] == 1
    assert report["verdict_status"] == {"ok": 1, "error": 1}
    # Only the genuine neutral opinion can reach a bucket.
    assert report["matched_orders"] == 1
    assert report["buckets"]["neutral"]["trades"] == 1


def test_date_window_filters_both_verdicts_and_orders(tmp_path):
    report = _report(tmp_path, [
        _verdict("2026-11-09T13:50:00+05:30"),
        _order("2026-11-09T13:55:00+05:30"),
        _exit(),
        _verdict("2026-11-11T13:50:00+05:30"),
        _order("2026-11-11T13:55:00+05:30", position_id="p2"),
        _exit("p2"),
    ], window_minutes=20, start="2026-11-11", end="2026-11-11")

    assert report["verdicts"] == 1
    assert report["matched_orders"] == 1
