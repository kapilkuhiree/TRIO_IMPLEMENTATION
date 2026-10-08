"""
TRIO — Telegram alert tests (no network: send_telegram is mocked).
Author: Kapil Kuhire <kapilkuhire89@gmail.com>
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.alerts import (
    format_signal_message,
    format_exit_message,
    send_signal_alert,
    send_exit_alert,
)


def _sig():
    return {
        "symbol": "RELIANCE.NS",
        "action": "BUY",
        "confidence": 78,
        "entry_price": 1180.0,
        "stop_loss": 1160.0,
        "target": 1210.0,
        "position_size": 7,
        "risk_amount": 140.0,
        "risk_reward_ratio": "1:1.5",
        "rank": 122.5,
        "setup_name": "pullback-long",
        "reasoning": [
            "NO PULLBACK SETUP: skipped",
            "  sma_9: bullish (value=1.0)",
            "8/15 technical indicators bullish (tech_score=0.40)",
            "Composite score: 0.4000",
        ],
    }


def test_entry_message_has_what_where_stop_target_why():
    msg = format_signal_message(_sig())
    assert "BUY" in msg and "RELIANCE" in msg
    assert "1,180" in msg          # entry (dots escaped, so match loosely)
    assert "1,160" in msg          # stop
    assert "1,210" in msg          # target
    assert "122" in msg            # rank
    assert "pullback" in msg       # setup
    assert "78" in msg             # confidence
    # noisy per-indicator dump lines are stripped
    assert "value=1.0" not in msg
    # verdict lines kept
    assert "NO PULLBACK" in msg


def test_markdown_specials_escaped_no_raw_underscores():
    """Regression: the live 400 Bad Request. tech_score= lines must not
    leave raw underscores that break Telegram legacy-Markdown parsing."""
    d = _sig()
    d["reasoning"] = [
        "8/15 technical indicators bullish (tech_score=0.40)",
        "Composite score: 0.4000",
        "sma_9: bullish (value=1168.16)",
    ]
    msg = format_signal_message(d)
    import re
    # every underscore / paren / dot / colon / equals in dynamic text escaped
    assert "tech\\_score\\=0\\.40" in msg
    # static markers keep their intentional formatting
    assert "*TRIO BUY" in msg and "*Why:*" in msg


def test_exit_message_escapes_reason():
    msg = format_exit_message({
        "symbol": "TCS.NS", "side": "LONG", "quantity": 5,
        "entry": 2000.0, "exit": 2030.0, "pnl": 150.0,
        "pnl_pct": 1.5, "reason": "guard-exit: trend_lost (x=1.0)",
        "result": "PASS",
    })
    assert "trend\\_lost" in msg
    assert "PASS" in msg


def test_hold_message_has_reason_not_levels():
    d = _sig()
    d["action"] = "HOLD"
    msg = format_signal_message(d)
    assert "HOLD" in msg
    assert "guard refused" in msg.lower() or "NO PULLBACK" in msg


def test_exit_message_pass_fail():
    msg = format_exit_message({
        "symbol": "TCS.NS", "side": "LONG", "quantity": 5,
        "entry": 2000.0, "exit": 2030.0, "pnl": 150.0, "pnl_pct": 1.5,
        "reason": "target-hit", "result": "PASS",
    })
    # dots are backslash-escaped for Telegram Markdown — match loosely
    assert "PASS" in msg and "TCS" in msg
    assert "+150" in msg
    assert "target" in msg

    msg2 = format_exit_message({
        "symbol": "X", "side": "LONG", "quantity": 1,
        "entry": 100.0, "exit": 90.0, "pnl": -10.0, "pnl_pct": -10.0,
        "reason": "stop-hit", "result": "FAIL",
    })
    assert "FAIL" in msg2 and "-10" in msg2


def test_send_signal_alert_calls_telegram(monkeypatch):
    # conftest pins TRIO_TEST_MODE=1; the suppression must yield to an
    # explicit live call in these tests, which simulate production.
    monkeypatch.delenv("TRIO_TEST_MODE", raising=False)
    with patch("src.alerts.send_telegram", return_value=True) as tg, \
         patch("src.alerts.send_email", return_value=True):
        send_signal_alert(_sig())
    assert tg.call_count == 1
    text = tg.call_args[0][0]
    assert "RELIANCE" in text and "BUY" in text


def test_send_exit_alert_calls_telegram(monkeypatch):
    monkeypatch.delenv("TRIO_TEST_MODE", raising=False)
    trade = {"symbol": "TCS.NS", "side": "LONG", "quantity": 5,
             "entry": 2000.0, "exit": 2030.0, "pnl": 150.0,
             "pnl_pct": 1.5, "reason": "target-hit", "result": "PASS"}
    with patch("src.alerts.send_telegram", return_value=True) as tg, \
         patch("src.alerts.send_email", return_value=True):
        send_exit_alert(trade)
    assert tg.call_count == 1
    assert "TCS" in tg.call_args[0][0]


def test_alerts_suppressed_in_test_mode(monkeypatch):
    """No test path may ping the real bot (2026-10-08: a pytest run sent
    live entry/exit Telegram messages)."""
    monkeypatch.setenv("TRIO_TEST_MODE", "1")
    from src.alerts import send_telegram, send_email
    with patch("requests.post") as post:
        assert send_telegram("hello") is False
        post.assert_not_called()
    with patch("smtplib.SMTP") as smtp:
        assert send_email("s", "b") is False
        smtp.assert_not_called()


def test_exit_message_has_entry_exit_time_and_held():
    """The full audit question: entry timestamp, exit timestamp,
    holding duration, P&L and close reason — all on one message."""
    msg = format_exit_message({
        "symbol": "HDFCBANK.NS", "side": "SHORT", "quantity": 28,
        "entry": 709.0, "exit": 699.0, "pnl": 280.0, "pnl_pct": 1.41,
        "reason": "target-hit", "result": "PASS",
        "entry_time": "2026-10-07T04:05:00+00:00",
        "closed_at": "2026-10-07T06:35:00+00:00",
        "holding_minutes": 150,
    })
    assert "PASS" in msg and "HDFCBANK" in msg
    assert "+280" in msg
    assert "target" in msg
    # IST rendering: 04:05 UTC = 09:35 IST, 06:35 UTC = 12:05 IST
    assert "09:35 IST" in msg
    assert "12:05 IST" in msg
    assert "2h 30m" in msg


def test_exit_message_graceful_without_times():
    """Old records (no entry_time) must render, not crash — '—' shown."""
    msg = format_exit_message({
        "symbol": "X", "side": "LONG", "quantity": 1,
        "entry": 100.0, "exit": 90.0, "pnl": -10.0, "pnl_pct": -10.0,
        "reason": "stop-hit", "result": "FAIL",
    })
    assert "FAIL" in msg
    assert "Entry:" in msg and "Exit:" in msg and "Held:" in msg


def test_entry_message_has_timestamp():
    d = _sig()
    d["generated_at"] = "2026-10-07T04:05:00+00:00"
    msg = format_signal_message(d)
    assert "Time:" in msg
    assert "09:35 IST" in msg


def test_ist_display_and_minutes_between():
    from src.utils import ist_display, minutes_between
    assert ist_display("2026-10-07T04:05:00+00:00") == "07 Oct, 09:35 IST (04:05 UTC)"
    assert ist_display(None) == "—"
    assert ist_display("garbage") == "—"
    assert minutes_between("2026-10-07T04:05:00+00:00",
                           "2026-10-07T06:35:00+00:00") == 150
    assert minutes_between(None, "2026-10-07T06:35:00+00:00") is None


def test_alerts_disabled_when_no_config():
    with patch("src.alerts.load_config",
               return_value={"alerts": {"telegram": {"enabled": False}}}):
        from src.alerts import send_telegram
        assert send_telegram("hi") is False


def test_startup_message_clean_boot():
    from src.alerts import format_startup_message
    msg = format_startup_message({
        "booted_at": "2026-10-07T03:40:00+00:00",
        "provider": "megabull", "equity": 499819.83, "positions": 0,
        "symbols": 15, "interval": 300, "session": "09:20–15:15 IST",
        "capital_start": 500000.0, "issues": [],
    })
    assert "STARTED AND WORKING" in msg
    assert "Mr Kapil Kuhire Sir" in msg
    assert "09:10 IST" in msg  # 03:40 UTC -> IST
    assert "megabull" in msg
    assert "499" in msg and "15" in msg


def test_startup_message_with_issues_names_them():
    from src.alerts import format_startup_message
    msg = format_startup_message({
        "booted_at": "2026-10-07T03:40:00+00:00",
        "provider": "megabull", "equity": 0, "positions": 0,
        "symbols": 15, "interval": 300, "session": "09:20–15:15 IST",
        "capital_start": 500000.0,
        "issues": ["broker balance fetch failed: 401",
                   "scan loop thread not running"],
    })
    assert "STARTED WITH ISSUES" in msg
    assert "Mr Kapil Kuhire Sir" in msg
    assert "401" in msg
    assert "scan loop" in msg
