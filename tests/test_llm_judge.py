"""Tests for the shadow-only LLM judge (no network, no sleeps beyond polling)."""

from __future__ import annotations

import json
import sys
import time
import types
from datetime import datetime, timedelta, timezone

import pytest

from src import llm_judge
from src.llm_judge import (
    GeminiTransport,
    JudgeTransportError,
    ShadowJudge,
    build_shadow_judge,
    parse_verdict,
)


IST = timezone(timedelta(hours=5, minutes=30))


class FakeTransport:
    """Deterministic stand-in for GeminiTransport."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def generate(self, prompt, *, key, model, timeout=2.0):
        self.calls.append({"key": key, "model": model, "timeout": timeout,
                           "prompt": prompt})
        reply = self.replies.pop(0) if self.replies else '{"bias":"neutral"}'
        if isinstance(reply, Exception):
            raise reply
        return reply


def _snapshot(**overrides):
    payload = {
        "underlying": "NIFTY",
        "strategy": "orb",
        "direction": "bearish",
        "option_type": "PE",
        "bar_ts": "2026-11-11T13:50:00+05:30",
        "trigger": 25263.80,
        "spot": 25167.00,
        "vwap": 25225.4,
        "atr": 29.4,
        "extension_atr": 3.29,
        "orb_high": 25399.10,
        "orb_low": 25263.80,
        "premium": 220.55,
        "bars": [{"t": "13:50", "o": 25190.3, "h": 25190.3, "l": 25162.7,
                  "c": 25167.0, "v": 0}],
    }
    payload.update(overrides)
    return payload


def _drain(judge, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        records = judge.poll()
        if records:
            return records
        time.sleep(0.02)
    return []


def _judge(replies, **kwargs):
    options = {
        "keys": [("GEMINI_KEY_1", "secret-key-1")],
        "transport": FakeTransport(replies),
        "model": "gemini-2.5-flash",
        "timeout_seconds": 0.5,
        "max_queue": 4,
        "max_calls_per_session": 10,
    }
    options.update(kwargs)
    return ShadowJudge(**options)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_verdict_accepts_plain_json():
    verdict = parse_verdict(
        '{"bias":"short","trend_15m":"down","confidence":0.62,'
        '"reason_code":"vwap_reject","disagree":false}',
        "bearish")
    assert verdict["ok"] is True
    assert verdict["bias"] == "short"
    assert verdict["agrees"] is True
    assert verdict["disagree"] is False


def test_parse_verdict_strips_markdown_fences():
    verdict = parse_verdict(
        '```json\n{"bias":"long","trend_15m":"up","confidence":0.8,'
        '"reason_code":"level_retest"}\n```', "bearish")
    assert verdict["ok"] is True
    assert verdict["agrees"] is False


def test_parse_verdict_neutral_is_neither_agree_nor_disagree():
    verdict = parse_verdict('{"bias":"neutral","confidence":0.4}', "bearish")
    assert verdict["ok"] is True
    assert verdict["agrees"] is None
    assert verdict["disagree"] is False


@pytest.mark.parametrize("raw,error", [
    ("not json at all", "invalid_json"),
    ("[1,2,3]", "not_an_object"),
    ('{"bias":"sideways"}', "invalid_bias"),
])
def test_parse_verdict_rejects_malformed(raw, error):
    verdict = parse_verdict(raw, "bearish")
    assert verdict["ok"] is False
    assert verdict["error"] == error


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_disabled_config_returns_none():
    assert build_shadow_judge({"llm_judge": {"enabled": False}}) is None
    assert build_shadow_judge({}) is None


def test_enabled_without_keys_returns_none(monkeypatch):
    monkeypatch.setattr(llm_judge, "_env_file_values", lambda names: {})
    for name in llm_judge.DEFAULT_KEYS_ENV:
        monkeypatch.delenv(name, raising=False)
    assert build_shadow_judge({"llm_judge": {"enabled": True}}) is None


def test_env_override_disables(monkeypatch):
    monkeypatch.setattr(llm_judge, "_env_file_values",
                        lambda names: {"GEMINI_KEY_1": "abc"})
    monkeypatch.setenv("TRIO_LLM_JUDGE_ENABLED", "0")
    assert build_shadow_judge({"llm_judge": {"enabled": True}}) is None


def test_duplicate_secrets_are_collapsed(monkeypatch):
    monkeypatch.setattr(
        llm_judge, "_env_file_values",
        lambda names: {"GEMINI_KEY_1": "same", "GEMINI_KEY_2": "same"})
    judge = build_shadow_judge({"llm_judge": {"enabled": True}})
    assert judge is not None
    assert judge.key_count == 1
    judge.close()


# ---------------------------------------------------------------------------
# Worker behaviour
# ---------------------------------------------------------------------------


def test_ok_verdict_is_recorded_and_never_contains_secret():
    judge = _judge(['{"bias":"short","trend_15m":"down","confidence":0.7,'
                    '"reason_code":"level_retest"}'])
    assert judge.submit(_snapshot()) is True
    records = _drain(judge)
    judge.close()

    assert len(records) == 1
    record = records[0]
    assert record["status"] == "ok"
    assert record["verdict"]["agrees"] is True
    assert record["verdict"]["reason_code"] == "level_retest"
    assert record["key_id"] == "GEMINI_KEY_1"
    assert "secret-key-1" not in json.dumps(record)


def test_duplicate_snapshot_is_not_queued_twice():
    judge = _judge(['{"bias":"short","confidence":0.6}',
                    '{"bias":"short","confidence":0.6}'])
    assert judge.submit(_snapshot()) is True
    assert judge.submit(_snapshot()) is False
    _drain(judge)
    judge.close()
    assert len(judge._transport.calls) == 1


def test_different_bar_is_a_new_call():
    judge = _judge(['{"bias":"short","confidence":0.6}',
                    '{"bias":"short","confidence":0.6}'])
    assert judge.submit(_snapshot(bar_ts="2026-11-11T13:50:00+05:30")) is True
    assert judge.submit(_snapshot(bar_ts="2026-11-11T13:55:00+05:30")) is True
    _drain(judge)
    time.sleep(0.3)
    judge.close()
    assert len(judge._transport.calls) == 2


def test_invalid_json_is_logged_not_raised():
    judge = _judge(["this is not json"])
    judge.submit(_snapshot())
    records = _drain(judge)
    judge.close()
    assert records[0]["status"] == "invalid_verdict"
    assert records[0]["error"] == "invalid_json"


def test_rate_limited_key_falls_back_to_the_next_key():
    judge = _judge(
        [JudgeTransportError("http_429", status=429),
         '{"bias":"short","confidence":0.55}'],
        keys=[("GEMINI_KEY_1", "a"), ("GEMINI_KEY_2", "b")],
    )
    judge.submit(_snapshot())
    records = _drain(judge)
    judge.close()

    assert records[0]["status"] == "ok"
    assert records[0]["key_id"] == "GEMINI_KEY_2"
    assert judge._keys[0]["cooldown_until"] > 0


def test_timeout_is_recorded_as_error_without_retry():
    judge = _judge([JudgeTransportError("ReadTimeout")],
                   keys=[("GEMINI_KEY_1", "a"), ("GEMINI_KEY_2", "b")])
    judge.submit(_snapshot())
    records = _drain(judge)
    judge.close()
    assert records[0]["status"] == "error"
    assert "ReadTimeout" in records[0]["error"]
    assert len(judge._transport.calls) == 1


def test_invalid_api_key_disables_the_key_permanently():
    judge = _judge(
        [JudgeTransportError("http_400:API_KEY_INVALID", status=400,
                             reason="API_KEY_INVALID")],
        keys=[("GEMINI_KEY_1", "dead")])
    judge.submit(_snapshot(bar_ts="2026-11-11T13:50:00+05:30"))
    first = _drain(judge)
    judge.submit(_snapshot(bar_ts="2026-11-11T13:55:00+05:30"))
    second = _drain(judge)
    judge.close()

    assert first[0]["status"] == "skipped_no_key"
    assert "API_KEY_INVALID" in (first[0]["error"] or "")
    # The dead key is never tried again in this session.
    assert second[0]["status"] == "skipped_no_key"
    assert len(judge._transport.calls) == 1


def test_error_body_reason_is_preserved_in_message():
    error = JudgeTransportError("http_400:API_KEY_INVALID", status=400,
                                reason="API_KEY_INVALID")
    assert error.status == 400
    assert error.reason == "API_KEY_INVALID"
    assert "API_KEY_INVALID" in str(error)


def test_queue_full_drops_without_raising():
    judge = _judge(['{"bias":"short"}'], max_queue=1)
    # Occupy the queue and the worker by never letting it run: close the
    # worker thread start by submitting one item, then flood.
    assert judge.submit(_snapshot()) is True
    assert judge.submit(_snapshot(bar_ts="2026-11-11T13:55:00+05:30")) in (True, False)
    # No exception is the contract.
    judge.close()


def test_max_calls_per_session_is_honoured():
    judge = _judge(['{"bias":"short"}', '{"bias":"short"}'],
                   max_calls_per_session=1)
    judge.submit(_snapshot(bar_ts="2026-11-11T13:50:00+05:30"))
    _drain(judge)
    judge.submit(_snapshot(bar_ts="2026-11-11T13:55:00+05:30"))
    records = _drain(judge)
    judge.close()
    assert records[0]["status"] == "skipped_quota"
    assert len(judge._transport.calls) == 1


def test_prompt_includes_signal_context():
    judge = _judge(['{"bias":"short","confidence":0.5}'])
    judge.submit(_snapshot())
    _drain(judge)
    judge.close()
    prompt = judge._transport.calls[0]["prompt"]
    assert "orb" in prompt
    assert "25263.8" in prompt
    assert "13:50" in prompt
    assert "5-minute bars" in prompt


# ---------------------------------------------------------------------------
# Transport (stubbed requests; no network)
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeRequests(types.ModuleType):
    def __init__(self, responses):
        super().__init__("requests")
        self.responses = list(responses)
        self.posts = []

    def post(self, url, *, headers=None, json=None, timeout=None):
        self.posts.append({"url": url, "headers": headers, "json": json,
                           "timeout": timeout})
        reply = self.responses.pop(0) if self.responses else FakeResponse(
            200, {"candidates": [{"content": {"parts": [{"text": "{}"}]}}]})
        if isinstance(reply, Exception):
            raise reply
        return reply


def _install_requests(monkeypatch, responses):
    module = FakeRequests(responses)
    monkeypatch.setitem(sys.modules, "requests", module)
    return module


def test_transport_sends_thinking_off_and_returns_joined_parts(monkeypatch):
    requests_module = _install_requests(monkeypatch, [FakeResponse(200, {
        "modelVersion": "gemini-3.5-flash-001",
        "candidates": [{
            "content": {"parts": [
                {"text": '{"bias":', "thoughtSignature": "sig"},
                {"text": '"short"}'},
            ]},
            "finishReason": "STOP",
        }],
    })])
    transport = GeminiTransport(max_output_tokens=512, thinking_budget=0)

    text = transport.generate("prompt", key="secret", model="gemini-3.5-flash",
                              timeout=5.0)

    assert text == '{"bias":"short"}'
    assert transport.last_model_version == "gemini-3.5-flash-001"
    sent = requests_module.posts[0]["json"]["generationConfig"]
    assert sent["thinkingConfig"] == {"thinkingBudget": 0}
    assert sent["maxOutputTokens"] == 512


def test_transport_strips_models_prefix_from_configured_name(monkeypatch):
    requests_module = _install_requests(monkeypatch, [FakeResponse(200, {
        "candidates": [{"content": {"parts": [{"text": "{}"}]}}],
    })])
    transport = GeminiTransport()

    transport.generate("prompt", key="k", model="models/gemini-3.5-flash",
                       timeout=5.0)

    url = requests_module.posts[0]["url"]
    assert "/models/gemini-3.5-flash:generateContent" in url
    assert "models/models/" not in url


def test_transport_surfaces_dead_model_as_not_found(monkeypatch):
    _install_requests(monkeypatch, [FakeResponse(404, {
        "error": {
            "code": 404,
            "status": "NOT_FOUND",
            "message": "This model models/gemini-2.5-flash is no longer available",
        },
    })])
    transport = GeminiTransport()

    with pytest.raises(JudgeTransportError) as excinfo:
        transport.generate("prompt", key="k", model="gemini-2.5-flash", timeout=5.0)

    assert excinfo.value.status == 404
    assert excinfo.value.reason == "NOT_FOUND"


def test_transport_reports_truncated_output_instead_of_empty_success(monkeypatch):
    _install_requests(monkeypatch, [FakeResponse(200, {
        "candidates": [{"content": {"parts": [{"thoughtSignature": "sig"}]},
                        "finishReason": "MAX_TOKENS"}],
    })])
    transport = GeminiTransport()

    with pytest.raises(JudgeTransportError) as excinfo:
        transport.generate("prompt", key="k", model="gemini-3.5-flash", timeout=5.0)

    assert "empty_text:MAX_TOKENS" in str(excinfo.value)


def test_transport_reports_blocked_prompt(monkeypatch):
    _install_requests(monkeypatch, [FakeResponse(200, {
        "promptFeedback": {"blockReason": "SAFETY"},
        "candidates": [],
    })])
    transport = GeminiTransport()

    with pytest.raises(JudgeTransportError) as excinfo:
        transport.generate("prompt", key="k", model="gemini-3.5-flash", timeout=5.0)

    assert "no_candidate:SAFETY" in str(excinfo.value)


def test_transient_503_moves_to_next_key_without_cooling_it():
    judge = _judge(
        [JudgeTransportError("http_503:UNAVAILABLE", status=503,
                             reason="UNAVAILABLE"),
         '{"bias":"short","confidence":0.6}'],
        keys=[("GEMINI_KEY_1", "a"), ("GEMINI_KEY_2", "b")],
    )
    judge.submit(_snapshot())
    records = _drain(judge)
    judge.close()

    assert records[0]["status"] == "ok"
    assert records[0]["key_id"] == "GEMINI_KEY_2"
    # Capacity errors are not the key's fault, so key 1 stays usable.
    assert judge._keys[0]["cooldown_until"] == 0.0
