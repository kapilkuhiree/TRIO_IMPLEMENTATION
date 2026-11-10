"""Offline tests for the shadow-signal replay report."""

from scripts.shadow_replay import (
    build_report,
    build_shadow_trades,
    format_report,
    main,
)


def _shadow(ts, *, strategy="mean_reversion", underlying="NIFTY",
            strike=25000.0, kind="CE", vwap_block=False, collision=False):
    return {
        "ts": ts,
        "event": "shadow_signal",
        "mode": "paper",
        "asset": "options",
        "strategy": strategy,
        "underlying": underlying,
        "observed_at": ts,
        "expiry": "29-Oct-2026",
        "strike": strike,
        "kind": kind,
        "option_type": kind,
        "ask": 100.0,
        "vwap_would_block": vwap_block,
        "collides_with_tradable": collision,
    }


def test_build_shadow_trades_deduplicates_scans_and_filters_window():
    records = [
        # Same signal re-logged three times inside the 10:00-10:05 bar.
        _shadow("2026-11-11T10:00:05+05:30"),
        _shadow("2026-11-11T10:02:05+05:30"),
        _shadow("2026-11-11T10:04:55+05:30"),
        # New bar, new signal.
        _shadow("2026-11-11T10:05:05+05:30"),
        # Different sleeve and contract are separate signals.
        _shadow("2026-11-11T10:05:05+05:30", strategy="orb", strike=25100.0),
        _shadow("2026-11-11T10:05:05+05:30", kind="PE"),
        # Outside the requested window.
        _shadow("2026-11-12T10:05:05+05:30"),
        # Not a shadow signal.
        {"ts": "2026-11-11T10:06:00+05:30", "event": "order"},
    ]
    trades, raw = build_shadow_trades(
        records, start="2026-11-11", end="2026-11-11")

    assert raw == 7
    assert len(trades) == 4
    assert {trade["strategy"] for trade in trades} == {"mean_reversion", "orb"}
    assert all(trade["day"] == "2026-11-11" for trade in trades)
    assert all(trade["lots"] == 1 for trade in trades)


def test_build_shadow_trades_strategy_filter():
    records = [
        _shadow("2026-11-11T10:00:05+05:30"),
        _shadow("2026-11-11T10:05:05+05:30", strategy="orb"),
    ]
    trades, _ = build_shadow_trades(records, strategies=["mean_reversion"])
    assert len(trades) == 1
    assert trades[0]["strategy"] == "mean_reversion"


def _pair(strategy, day, net, *, status="matched", reason=None):
    trade = {"strategy": strategy, "day": day, "entry_ts": f"{day}T10:00:00+05:30"}
    if status == "matched":
        return {
            "trade": trade,
            "status": "matched",
            "real": {
                "gross_pnl": net + 20.0,
                "net_pnl": net,
                "entry_timestamp": f"{day}T10:05:00+05:30",
            },
        }
    return {"trade": trade, "status": "miss", "reason": reason}


def test_build_report_counts_hypothetical_pnl_and_misses():
    pairs = [
        _pair("mean_reversion", "2026-11-11", 500.0),
        _pair("mean_reversion", "2026-11-11", -200.0),
        _pair("mean_reversion", "2026-11-11", 0.0, status="miss",
              reason="no_option_candles"),
        _pair("mean_reversion", "2026-11-12", 100.0),
    ]
    report = build_report(
        pairs,
        trades=[pair["trade"] for pair in pairs],
        raw=9,
        start=None,
        end=None,
        archive_path="archive.sqlite3",
        log_path="trade_log.jsonl",
    )

    shadow = report["shadow"]
    assert shadow["raw_events"] == 9
    assert shadow["signals"] == 4
    assert shadow["matched"] == 3
    assert shadow["match_rate"] == 75.0
    assert shadow["hypothetical_net"]["trades"] == 3
    assert shadow["hypothetical_net"]["net"] == 400.0
    assert shadow["hypothetical_gross"]["net"] == 460.0
    assert shadow["miss_reasons"] == {"no_option_candles": 1}
    assert report["by_strategy"]["mean_reversion"]["matched"] == 3
    assert report["by_day"]["2026-11-11"]["net"] == 300.0
    assert report["by_day"]["2026-11-12"]["net"] == 100.0


def test_format_report_is_explicit_that_the_result_is_hypothetical():
    report = build_report(
        [_pair("mean_reversion", "2026-11-11", 250.0)],
        trades=[{"strategy": "mean_reversion", "day": "2026-11-11"}],
        raw=1,
        start=None,
        end=None,
        archive_path="archive.sqlite3",
        log_path="trade_log.jsonl",
    )
    text = format_report(report)
    assert "SHADOW SIGNAL REPLAY" in text
    assert "HYPOTHETICAL PERFORMANCE" in text
    assert "BY SLEEVE" in text
    assert "mean_reversion" in text
    assert "250" in text


def test_main_handles_an_empty_ledger_without_an_archive(tmp_path):
    log = tmp_path / "trade_log.jsonl"
    log.write_text("", encoding="utf-8")
    archive = tmp_path / "missing.sqlite3"
    assert main([
        "--log", str(log), "--archive", str(archive),
    ]) == 0
