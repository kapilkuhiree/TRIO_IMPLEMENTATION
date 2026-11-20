"""Offline tests for the model-vs-real premium replay helpers and report."""

from scripts.premium_replay import (
    _carried_label,
    _miss_reason,
    _stats,
    build_signal,
    format_report,
)


def test_stats_reports_win_rate_profit_factor_and_drawdown():
    stats = _stats(
        [100.0, -50.0, 200.0, -300.0],
        ["2026-10-21T10:00", "2026-10-21T10:30",
         "2026-10-21T11:00", "2026-10-21T11:30"],
    )
    assert stats["trades"] == 4
    assert stats["wins"] == 2
    assert stats["losses"] == 2
    assert stats["win_rate"] == 50.0
    assert stats["net"] == -50.0
    assert abs(stats["profit_factor"] - 300.0 / 350.0) < 1e-9
    assert stats["max_dd"] == -300.0


def test_stats_handles_an_empty_sample():
    stats = _stats([])
    assert stats == {
        "trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
        "net": 0.0, "profit_factor": 0.0, "avg": 0.0, "max_dd": 0.0,
    }


def test_stats_orders_by_entry_timestamp_before_drawdown():
    stats = _stats(
        [-100.0, 200.0],
        ["2026-10-21T11:00", "2026-10-21T10:00"],
    )
    # Chronologically: +200 then -100, so the peak-to-trough drawdown is -100.
    assert stats["net"] == 100.0
    assert stats["max_dd"] == -100.0


def test_miss_reason_prefers_the_documented_priority():
    result = {"diagnostics": {"counts": {
        "risk_budget_below_one_lot": 1,
        "no_contract": 1,
    }}}
    assert _miss_reason(result) == "no_contract"


def test_miss_reason_falls_back_to_a_nonzero_count():
    result = {"diagnostics": {"counts": {"brand_new_reason": 2}}}
    assert _miss_reason(result) == "brand_new_reason"
    assert _miss_reason({"diagnostics": {"counts": {}}}) == "no_trade"


def test_build_signal_keeps_the_model_identity_and_timestamp():
    trade = {
        "signal_ts": "2026-10-21T10:00:00+05:30",
        "strategy": "orb",
        "symbol": "NIFTY",
        "entry_ts": "2026-10-21T10:05:00+05:30",
    }
    contract = {
        "underlying": "NIFTY",
        "trading_symbol": "NIFTY26OCT23000PE",
        "expiry_date": "2026-10-27",
        "strike": 23_000.0,
        "option_type": "PE",
        "lot_size": 65,
    }
    signal = build_signal(trade, contract)
    assert signal["timestamp"] == trade["signal_ts"]
    assert signal["trading_symbol"] == "NIFTY26OCT23000PE"
    assert signal["option_type"] == "PE"
    assert signal["action"] == "BUY"
    assert signal["completed"] is True
    assert signal["signal_id"].startswith("orb:NIFTY:")


def test_carried_label_names_each_side():
    assert _carried_label({}) == "-"
    assert _carried_label({"entry_carried": True}) == "entry"
    assert _carried_label({"exit_carried": True}) == "exit"
    assert _carried_label({"entry_carried": True, "exit_carried": True}) == "entry+exit"


def _report():
    def stats(net, pf):
        return {"trades": 1, "wins": 1, "losses": 0, "win_rate": 100.0,
                "net": net, "profit_factor": pf, "avg": net, "max_dd": 0.0}

    return {
        "window": {"start": "2026-10-21", "end": "2026-10-21", "sessions": 1},
        "archive": "archive.sqlite3",
        "analytics": "analytics.sqlite3",
        "model_trades": 1,
        "matched_trades": 1,
        "match_rate": 100.0,
        "sleeves": {
            "orb": {
                "model": stats(4_945.0, 9.0),
                "model_gross": stats(5_096.0, 9.0),
                "model_matched": stats(4_945.0, 9.0),
                "model_matched_gross": stats(5_096.0, 9.0),
                "real_gross": stats(4_282.0, 7.0),
                "real_net": stats(4_215.0, 6.5),
                "match_rate": 100.0,
                "miss_reasons": {},
            },
        },
        "pairs": [{
            "status": "matched",
            "trade": {
                "strategy": "orb",
                "entry_ts": "2026-10-21T10:00:00+05:30",
                "strike": 55_600.0,
                "lots": 1,
                "gross_pnl": 5_096.0,
                "pnl": 4_945.0,
            },
            "contract": {"strike": 55_600.0, "strike_drift_pct": 0.0},
            "real": {
                "lots": 1,
                "gross_pnl": 4_282.0,
                "net_pnl": 4_215.0,
                "exit_reason": "target",
                "entry_carried": True,
                "exit_carried": False,
            },
        }],
        "iv_snapshot": {},
        "archive_quality": None,
    }


def test_format_report_shows_gross_net_lots_and_carried_flags():
    text = format_report(_report())
    assert "ModelGross" in text
    assert "RealGross" in text
    assert "MdlLot" in text
    assert "RealLot" in text
    assert "Carried" in text
    assert "entry" in text
    assert "5,096" in text
    assert "4,282" in text
    assert "4,215" in text
    assert "100.0%" in text
    assert "-814" in text  # matched gross delta: 4,282 - 5,096
    assert "-730" in text  # matched net delta: 4,215 - 4,945
    assert "Model backtest assumption" not in text  # no IV snapshot in this stub
