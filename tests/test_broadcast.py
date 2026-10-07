"""
TRIO — Open-broadcast tests (no network: requests + store mocked).
Author: Kapil Kuhire <kapilkuhire89@gmail.com>
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.alerts as alerts


def _store(tmp_path, chats=None, offset=0):
    p = Path(tmp_path) / "subs.json"
    p.write_text(json.dumps({"chats": chats or [], "offset": offset}),
                 encoding="utf-8")
    return str(p)


def _cfg(sub_file):
    return {"alerts": {"telegram": {"enabled": True,
                                    "bot_token_env": "TELEGRAM_BOT_TOKEN",
                                    "chat_id_env": "TELEGRAM_CHAT_ID",
                                    "subscriber_file": sub_file}}}


def test_unknown_start_subscribed_and_welcomed(tmp_path):
    sub = _store(tmp_path, chats=["111"], offset=10)
    updates = {"result": [
        {"update_id": 10,
         "message": {"text": "/start", "chat": {"id": 222}}}]}
    with patch("src.alerts.load_config", return_value=_cfg(sub)), \
         patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "T"}), \
         patch("src.alerts._send_one") as send_one, \
         patch("requests.get") as get:
        get.return_value.json.return_value = updates
        get.return_value.raise_for_status.return_value = None
        new_ids = alerts.handle_joins()
    assert new_ids == ["222"]
    data = json.loads(Path(sub).read_text(encoding="utf-8"))
    assert "222" in data["chats"] and "111" in data["chats"]
    assert data["offset"] == 11
    # welcome text sent once, contains entry + exit samples
    assert send_one.call_count == 1
    assert "TRIO BUY" in send_one.call_args[0][2]


def test_repeat_start_no_duplicate_no_rewelcome(tmp_path):
    sub = _store(tmp_path, chats=["111", "222"], offset=10)
    updates = {"result": [
        {"update_id": 10,
         "message": {"text": "/start", "chat": {"id": 222}}}]}
    with patch("src.alerts.load_config", return_value=_cfg(sub)), \
         patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "T"}), \
         patch("src.alerts._send_one") as send_one, \
         patch("requests.get") as get:
        get.return_value.json.return_value = updates
        get.return_value.raise_for_status.return_value = None
        new_ids = alerts.handle_joins()
    assert new_ids == []
    assert send_one.call_count == 0


def test_broadcast_reaches_all_ids(tmp_path):
    sub = _store(tmp_path, chats=["111", "222"])
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(json["chat_id"])
        m = MagicMock()
        m.raise_for_status.return_value = None
        return m

    with patch("src.alerts.load_config", return_value=_cfg(sub)), \
         patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "T",
                                   "TELEGRAM_CHAT_ID": "111"}), \
         patch("requests.post", side_effect=fake_post):
        assert alerts.send_telegram("hello") is True
    assert sorted(calls) == ["111", "222"]


def test_blocked_recipient_does_not_break_others(tmp_path):
    import requests as rq
    sub = _store(tmp_path, chats=["111", "222"])

    def fake_post(url, json=None, timeout=None):
        if json["chat_id"] == "222":
            resp = MagicMock()
            resp.status_code = 403
            err = rq.exceptions.HTTPError("403")
            err.response = resp
            raise err
        m = MagicMock()
        m.raise_for_status.return_value = None
        return m

    with patch("src.alerts.load_config", return_value=_cfg(sub)), \
         patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "T",
                                   "TELEGRAM_CHAT_ID": "111"}), \
         patch("requests.post", side_effect=fake_post):
        assert alerts.send_telegram("hello") is True
    # 222 blocked (403): removed immediately, 111 unaffected.
    data = json.loads(Path(sub).read_text(encoding="utf-8"))
    assert "222" not in data["chats"]
    assert "111" in data["chats"]


def test_three_strikes_removes_id(tmp_path):
    # Non-403 network errors accumulate: removed after 3 consecutive.
    sub = _store(tmp_path, chats=["111", "222"])
    p = Path(sub)
    data = json.loads(p.read_text(encoding="utf-8"))
    data["failures"] = {"222": 2}
    p.write_text(json.dumps(data), encoding="utf-8")

    def fake_post(url, json=None, timeout=None):
        if json["chat_id"] == "222":
            raise ConnectionError("timeout")
        m = MagicMock()
        m.raise_for_status.return_value = None
        return m

    with patch("src.alerts.load_config", return_value=_cfg(sub)), \
         patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "T",
                                   "TELEGRAM_CHAT_ID": "111"}), \
         patch("requests.post", side_effect=fake_post):
        alerts.send_telegram("hello")
    assert "222" not in json.loads(Path(sub).read_text(encoding="utf-8"))["chats"]


def test_missing_store_falls_back_to_env_single_id(tmp_path):
    missing = str(Path(tmp_path) / "nope.json")
    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append(json["chat_id"])
        m = MagicMock()
        m.raise_for_status.return_value = None
        return m

    with patch("src.alerts.load_config", return_value=_cfg(missing)), \
         patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "T",
                                   "TELEGRAM_CHAT_ID": "999"}), \
         patch("requests.post", side_effect=fake_post):
        assert alerts.send_telegram("hello") is True
    assert sent == ["999"]


def test_disabled_config_sends_nothing(tmp_path):
    sub = _store(tmp_path, chats=["111"])
    cfg = _cfg(sub)
    cfg["alerts"]["telegram"]["enabled"] = False
    with patch("src.alerts.load_config", return_value=cfg), \
         patch("requests.post") as post:
        assert alerts.send_telegram("hello") is False
        assert alerts.handle_joins() == []
    post.assert_not_called()
