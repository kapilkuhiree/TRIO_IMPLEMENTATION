"""
TRIO — Dual-bot routing (old=intraday, new=8877508167 options)
Author: Kapil Kuhire <kapilkuhire89@gmail.com>
All offline: patched requests + patched load_config.
"""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import alerts

SENT = []


def _mock_resp(ok=True):
    m = MagicMock()
    m.json.return_value = {"ok": True, "result": []}
    m.raise_for_status = (lambda: None) if ok else (lambda: (_ for _ in ()).throw(Exception("http")))
    return m


def setup_function():
    SENT.clear()


def _cfg_with_options(bot="equity"):
    # Equity and options each have their own token/chat pair
    base = {
        "alerts": {
            "telegram": {
                "enabled": True,
                "bot_token_env": "TELEGRAM_BOT_TOKEN",
                "chat_id_env": "TELEGRAM_CHAT_ID",
                "subscriber_file": "output/subscribers.json",
                "options": {
                    "enabled": True,
                    "bot_token_env": "TELEGRAM_OPTIONS_BOT_TOKEN",
                    "chat_id_env": "TELEGRAM_OPTIONS_CHAT_ID",
                    "subscriber_file": "output/subscribers_options.json",
                },
            }
        }
    }
    return base


def _env_for(kind):
    if kind == "equity":
        return {"TELEGRAM_BOT_TOKEN": "EQ_TOK", "TELEGRAM_CHAT_ID": "EQ_CHAT"}
    return {"TELEGRAM_OPTIONS_BOT_TOKEN": "OPT_TOK", "TELEGRAM_OPTIONS_CHAT_ID": "8877508167"}


def test_send_telegram_routes_to_equity_bot(monkeypatch, tmp_path):
    monkeypatch.setenv("TRIO_TEST_MODE", "")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "EQ_TOK")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "EQ_CHAT")
    monkeypatch.setenv("TELEGRAM_OPTIONS_BOT_TOKEN", "OPT_TOK")
    monkeypatch.setenv("TELEGRAM_OPTIONS_CHAT_ID", "8877508167")
    # Seed subscribers so _subscriber_ids returns one chat
    (tmp_path / "subscribers.json").write_text('{"chats": ["EQ_CHAT"], "failures": {}}')
    (tmp_path / "subscribers_options.json").write_text('{"chats": ["8877508167"], "failures": {}}')
    cfg = _cfg_with_options()
    # Point subscriber files at tmp_path
    cfg["alerts"]["telegram"]["subscriber_file"] = str(tmp_path / "subscribers.json")
    cfg["alerts"]["telegram"]["options"]["subscriber_file"] = str(tmp_path / "subscribers_options.json")
    with patch("src.alerts.load_config", return_value=cfg), \
         patch("requests.post", side_effect=lambda url, json=None, **k: (SENT.append(url), _mock_resp())[1]):
        alerts.send_telegram("hello equity")
    assert any("EQ_TOK" in u for u in SENT)
    assert not any("OPT_TOK" in u for u in SENT)


def test_send_options_telegram_routes_to_options_bot(monkeypatch, tmp_path):
    monkeypatch.setenv("TRIO_TEST_MODE", "")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "EQ_TOK")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "EQ_CHAT")
    monkeypatch.setenv("TELEGRAM_OPTIONS_BOT_TOKEN", "OPT_TOK")
    monkeypatch.setenv("TELEGRAM_OPTIONS_CHAT_ID", "8877508167")
    (tmp_path / "subscribers.json").write_text('{"chats": ["EQ_CHAT"], "failures": {}}')
    (tmp_path / "subscribers_options.json").write_text('{"chats": ["8877508167"], "failures": {}}')
    cfg = _cfg_with_options()
    cfg["alerts"]["telegram"]["subscriber_file"] = str(tmp_path / "subscribers.json")
    cfg["alerts"]["telegram"]["options"]["subscriber_file"] = str(tmp_path / "subscribers_options.json")
    with patch("src.alerts.load_config", return_value=cfg), \
         patch("requests.post", side_effect=lambda url, json=None, **k: (SENT.append(url), _mock_resp())[1]):
        alerts.send_options_telegram("hello options 8877508167")
    assert any("OPT_TOK" in u for u in SENT)
    assert not any("EQ_TOK" in u for u in SENT)


def test_digest_split_filters_by_asset():
    from scripts.digest import collect, format_equity_digest, format_options_digest
    import scripts.digest as dg
    import json
    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    today = datetime.now(IST).strftime("%Y-%m-%d")
    fake_log = dg.LOG_PATH
    # Use tmp file by patching LOG_PATH
    import tempfile
    tf = Path(tempfile.mktemp(suffix=".jsonl"))
    dg.LOG_PATH = tf
    try:
        ts = datetime.now(timezone.utc).isoformat()
        tf.write_text(
            json.dumps({"ts": ts, "event": "order", "mode": "paper", "asset": "options",
                        "symbol": "OPT_X", "strategy": "bull-call-spread"}) + "\n" +
            json.dumps({"ts": ts, "event": "order", "mode": "paper",
                        "symbol": "RELIANCE.NS", "action": "BUY"}) + "\n",
            encoding="utf-8")
        d = collect(today)
        eq_text = format_equity_digest(d)
        opt_text = format_options_digest(d)
        assert "RELIANCE" in eq_text
        assert "OPT_X" not in eq_text
        assert "OPT_X" in opt_text
        assert "RELIANCE" not in opt_text
    finally:
        dg.LOG_PATH = fake_log
        try:
            tf.unlink()
        except Exception:
            pass
