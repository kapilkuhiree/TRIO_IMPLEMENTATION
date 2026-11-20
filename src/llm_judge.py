"""Shadow-only LLM judge for options candidates.

Design contract
---------------
This module is a *logger*, not a gate:

* it never blocks the caller (``submit`` only enqueues);
* it never raises into the trading path;
* it never changes sizing, stops, targets or order placement;
* a dead key, an HTTP error or a timeout is recorded and dropped.

A background daemon thread sends one compact snapshot per unique
``(underlying, strategy, bar_ts, direction)`` to Gemini and stores the parsed
verdict.  The intraday runtime drains the answers with :meth:`ShadowJudge.poll`
and writes them to ``output/trade_log.jsonl`` as ``llm_verdict`` events.

That gives a paired dataset (system decision vs LLM opinion vs outcome) which
can be scored later by ``scripts/llm_report.py``.  Only after that evidence
exists should anyone consider promoting the judge to a real filter.

Configuration lives under ``options_intraday.llm_judge`` in
``config/options_intraday.yaml``.  ``TRIO_LLM_JUDGE_ENABLED=0|1`` overrides the
``enabled`` flag for a single session.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_MODEL = "gemini-3.5-flash"
DEFAULT_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
DEFAULT_KEYS_ENV: Tuple[str, ...] = (
    "GEMINI_KEY_1",
    "GEMINI_KEY_2",
    "GEMINI_KEY_3",
    "GEMINI_KEY_4",
    "GOOGLE_GEMINI_API_KEY",
)

VALID_BIAS = ("long", "short", "neutral")
VALID_TREND = ("up", "down", "range")
VALID_REASON = (
    "vwap_reject",
    "level_retest",
    "momentum_decay",
    "range_bound",
    "counter_trend",
    "unclear",
)

# HTTP statuses that mean "this key is unusable right now" rather than
# "the request was malformed".
_KEY_COOLDOWN_STATUS = {401, 403, 429}

# Gemini returns 400 for a bad key, not 401.  These reasons are key-scoped.
_KEY_COOLDOWN_REASONS = {
    "API_KEY_INVALID",
    "API_KEY_EXPIRED",
    "PERMISSION_DENIED",
    "RESOURCE_EXHAUSTED",
    "QUOTA_EXCEEDED",
}
_PERMANENT_KEY_REASONS = {"API_KEY_INVALID", "API_KEY_EXPIRED"}

PROMPT_TEMPLATE = """You are a risk reviewer for an intraday NIFTY index-options system.
You do NOT place orders. You answer one question: does the prevailing trend
support the signal direction over roughly the next 30-60 minutes?

Signal
  strategy    : {strategy}
  direction   : {direction} ({option_type})
  underlying  : {underlying}
  bar time    : {bar_ts}
  spot        : {spot}
  session VWAP: {vwap}
  OR high/low : {orb_high} / {orb_low}
  ATR(5m)     : {atr}
  extension   : {extension_atr} ATR beyond the opening-range trigger
  premium ask : {premium}

Recent 5-minute bars, oldest first (HH:MM open high low close, volume):
{bars}

Rules for your answer
  - Judge only trend agreement, not profit targets.
  - "bias" must be long, short or neutral (neutral = genuinely no edge).
  - "trend_15m" describes the 15-minute structure: up, down or range.
  - confidence is 0.0-1.0.
  - reason_code must be one of: vwap_reject, level_retest, momentum_decay,
    range_bound, counter_trend, unclear.
  - disagree must be true when your bias conflicts with the signal direction.

Return ONLY the JSON object, no markdown fences, no prose."""


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class JudgeTransportError(Exception):
    """A single Gemini request failed."""

    def __init__(self, message: str, *, status: Optional[int] = None,
                 reason: str = ""):
        super().__init__(message)
        self.status = status
        self.reason = reason


class GeminiTransport:
    """Minimal REST client.  No SDK, so tests can inject a fake transport."""

    def __init__(self, endpoint: str = DEFAULT_ENDPOINT,
                 max_output_tokens: int = 512,
                 thinking_budget: Optional[int] = 0):
        self.endpoint = endpoint
        self.max_output_tokens = max(64, int(max_output_tokens))
        self.thinking_budget = thinking_budget
        # Populated from the last successful response so the logged record can
        # name the concrete model behind an alias like ``gemini-flash-latest``.
        self.last_model_version: Optional[str] = None

    def generate(self, prompt: str, *, key: str, model: str,
                 timeout: float = 2.0) -> str:
        import requests  # imported lazily so offline tests never need it

        # Accept both ``gemini-3.5-flash`` and ``models/gemini-3.5-flash``: the
        # endpoint template already supplies the ``models/`` segment.
        name = str(model or "").strip()
        if name.startswith("models/"):
            name = name[len("models/"):]
        url = self.endpoint.format(model=name)
        generation: Dict[str, Any] = {
            "temperature": 0,
            "responseMimeType": "application/json",
            "maxOutputTokens": self.max_output_tokens,
        }
        if self.thinking_budget is not None:
            # Reasoning tokens are billed against maxOutputTokens, and a
            # truncated JSON verdict is worse than no verdict at all.  This
            # prompt is a lookup, not a puzzle, so thinking adds latency
            # without adding signal.
            generation["thinkingConfig"] = {
                "thinkingBudget": max(0, int(self.thinking_budget))}
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation,
        }
        try:
            response = requests.post(
                url,
                headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                json=body,
                timeout=(min(1.0, timeout), timeout),
            )
        except Exception as exc:  # requests.Timeout, ConnectionError, ...
            raise JudgeTransportError(
                f"{type(exc).__name__}: {exc}") from exc

        if response.status_code >= 400:
            raise self._http_error(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise JudgeTransportError("malformed_response_body") from exc

        self.last_model_version = payload.get("modelVersion")
        candidates = payload.get("candidates") or []
        if not candidates:
            blocked = (payload.get("promptFeedback") or {}).get("blockReason")
            raise JudgeTransportError(
                f"no_candidate:{blocked}" if blocked else "no_candidate")
        candidate = candidates[0] or {}
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(str(part.get("text") or "") for part in parts
                       if isinstance(part, dict))
        if not text.strip():
            finish = str(candidate.get("finishReason") or "EMPTY")
            raise JudgeTransportError(f"empty_text:{finish}")
        return text

    @staticmethod
    def _http_error(response: Any) -> JudgeTransportError:
        reason = ""
        try:
            payload = response.json()
            error = payload.get("error") or {}
            reason = str(error.get("status") or "")
            for detail in error.get("details") or []:
                if isinstance(detail, dict) and detail.get("reason"):
                    reason = str(detail["reason"])
                    break
        except Exception:
            reason = ""
        message = f"http_{response.status_code}"
        if reason:
            message = f"{message}:{reason}"
        return JudgeTransportError(
            message, status=response.status_code, reason=reason)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env_file_values(names: Iterable[str]) -> Dict[str, str]:
    """Read ``.env`` directly for keys not exported into ``os.environ``.

    The paper session does not always export ``.env`` into the process
    environment, and a missing key must degrade to "judge disabled", never to
    an exception.  This is a deliberately small parser.
    """
    wanted = {name for name in names if name}
    if not wanted:
        return {}
    path = ROOT / ".env"
    if not path.exists():
        return {}
    found: Dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            if key not in wanted:
                continue
            value = value.strip().strip("'\"")
            if value:
                found[key] = value
    except OSError:
        return {}
    return found


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number


def _strip_fences(text: str) -> str:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1] if "\n" in raw else raw
        if raw.endswith("```"):
            raw = raw[:-3]
    return raw.strip()


def _render_bars(bars: Iterable[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for bar in bars or []:
        lines.append(
            "  {t} {o} {h} {l} {c} {v}".format(
                t=str(bar.get("t", "")),
                o=_finite(bar.get("o")),
                h=_finite(bar.get("h")),
                l=_finite(bar.get("l")),
                c=_finite(bar.get("c")),
                v=_finite(bar.get("v")),
            )
        )
    return "\n".join(lines) if lines else "  (no bars supplied)"


def build_prompt(snapshot: Dict[str, Any]) -> str:
    return PROMPT_TEMPLATE.format(
        strategy=snapshot.get("strategy", ""),
        direction=snapshot.get("direction", ""),
        option_type=snapshot.get("option_type", ""),
        underlying=snapshot.get("underlying", ""),
        bar_ts=snapshot.get("bar_ts", ""),
        spot=snapshot.get("spot", ""),
        vwap=snapshot.get("vwap", ""),
        orb_high=snapshot.get("orb_high", ""),
        orb_low=snapshot.get("orb_low", ""),
        atr=snapshot.get("atr", ""),
        extension_atr=snapshot.get("extension_atr", ""),
        premium=snapshot.get("premium", ""),
        bars=_render_bars(snapshot.get("bars") or []),
    )


def _expected_bias(direction: str) -> str:
    text = str(direction or "").strip().lower()
    if text in {"bullish", "long", "ce", "buy"}:
        return "long"
    if text in {"bearish", "short", "pe", "sell"}:
        return "short"
    return ""


def parse_verdict(text: str, direction: str) -> Dict[str, Any]:
    """Parse the model answer into a stable, enumerable shape."""
    raw = _strip_fences(text)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {"ok": False, "error": "invalid_json", "raw_len": len(raw)}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "not_an_object"}

    bias = str(payload.get("bias", "")).strip().lower()
    trend = str(payload.get("trend_15m", "")).strip().lower()
    reason = str(payload.get("reason_code", "")).strip().lower()
    confidence = _finite(payload.get("confidence"), -1.0)

    if bias not in VALID_BIAS:
        return {"ok": False, "error": "invalid_bias", "bias": bias}
    if trend not in VALID_TREND:
        trend = "range"
    if reason not in VALID_REASON:
        reason = "unclear"
    if not 0.0 <= confidence <= 1.0:
        confidence = 0.0

    expected = _expected_bias(direction)
    if bias == "neutral":
        agree: Optional[bool] = None
    elif expected:
        agree = bias == expected
    else:
        agree = None

    return {
        "ok": True,
        "bias": bias,
        "trend_15m": trend,
        "confidence": round(confidence, 3),
        "reason_code": reason,
        "agrees": agree,
        "disagree": (agree is False),
    }


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------


def _split_secrets(value: Any) -> List[str]:
    """Split one env value into individual keys.

    ``.env`` files in the wild sometimes pack several keys into a single line
    (``AIza...,AIza...``).  Splitting here means the operator does not have to
    rename variables just to rotate keys.
    """
    text = str(value or "").strip()
    if not text:
        return []
    text = text.replace(";", ",").replace("\n", ",")
    parts = [part.strip().strip("'\"") for part in text.split(",")]
    return [part for part in parts if part]


def resolve_keys(mode_cfg: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Return ``(id, secret)`` pairs for the configured Gemini keys.

    Sources, in order: the named environment variables, then a direct read of
    ``.env`` for names the session did not export, then the comma-separated
    ``TRIO_GEMINI_KEYS`` override.  A single value may hold several keys.
    Identical secrets are collapsed so a key copied under two names is not
    counted twice.
    """
    section = (mode_cfg or {}).get("llm_judge")
    if not isinstance(section, dict):
        return []
    names = section.get("keys_env") or list(DEFAULT_KEYS_ENV)
    if isinstance(names, str):
        names = [part.strip() for part in names.split(",") if part.strip()]
    names = [str(name).strip() for name in names if str(name).strip()]

    values: Dict[str, str] = {}
    for name in names:
        value = str(os.environ.get(name) or "").strip()
        if value:
            values[name] = value
    if not values:
        values.update(_env_file_values(names))

    found: List[Tuple[str, str]] = []
    for name in names:
        secrets = _split_secrets(values.get(name))
        if not secrets:
            continue
        if len(secrets) == 1:
            found.append((name, secrets[0]))
        else:
            for index, secret in enumerate(secrets, start=1):
                found.append((f"{name}[{index}]", secret))

    for index, secret in enumerate(
            _split_secrets(os.environ.get("TRIO_GEMINI_KEYS")), start=1):
        found.append((f"TRIO_GEMINI_KEYS[{index}]", secret))

    unique: List[Tuple[str, str]] = []
    seen_secrets = set()
    for key_id, secret in found:
        if secret in seen_secrets:
            continue
        seen_secrets.add(secret)
        unique.append((key_id, secret))
    return unique


class ShadowJudge:
    """Queue-backed, non-blocking Gemini shadow judge."""

    def __init__(
        self,
        *,
        keys: List[Tuple[str, str]],
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = 2.0,
        max_queue: int = 8,
        max_calls_per_session: int = 40,
        log_only: bool = True,
        endpoint: str = DEFAULT_ENDPOINT,
        max_output_tokens: int = 512,
        thinking_budget: Optional[int] = 0,
        transport: Optional[Any] = None,
        logger: Optional[Any] = None,
    ) -> None:
        self.model = model
        self.timeout_seconds = max(0.2, float(timeout_seconds))
        self.max_calls_per_session = max(0, int(max_calls_per_session))
        self.log_only = bool(log_only)
        self._keys = [
            {"id": key_id, "secret": secret, "cooldown_until": 0.0, "failures": 0}
            for key_id, secret in keys
        ]
        self._key_cursor = 0
        self._transport = transport or GeminiTransport(
            endpoint,
            max_output_tokens=max_output_tokens,
            thinking_budget=thinking_budget,
        )
        self._logger = logger
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(
            maxsize=max(1, int(max_queue)))
        self._results: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._seen: set = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._calls = 0
        self._dropped = 0
        self._errors = 0
        self._started_at = datetime.now(timezone.utc).isoformat()

    # -- construction ---------------------------------------------------
    @classmethod
    def from_mode(cls, mode_cfg: Dict[str, Any],
                  logger: Optional[Any] = None) -> Optional["ShadowJudge"]:
        """Build from ``options_intraday.llm_judge``; ``None`` when disabled."""
        section = (mode_cfg or {}).get("llm_judge")
        if not isinstance(section, dict):
            return None
        enabled = bool(section.get("enabled", False))
        override = os.environ.get("TRIO_LLM_JUDGE_ENABLED")
        if override is not None and override.strip() != "":
            enabled = override.strip().lower() in {"1", "true", "yes", "on", "y"}
        if not enabled:
            return None

        keys = resolve_keys(mode_cfg)
        if not keys:
            if logger is not None:
                logger.warning(
                    "llm_judge enabled but no key configured; judge disabled")
            return None

        return cls(
            keys=keys,
            model=str(section.get("model") or DEFAULT_MODEL),
            timeout_seconds=_finite(section.get("timeout_seconds"), 2.0),
            max_queue=int(_finite(section.get("max_queue"), 8.0)),
            max_calls_per_session=int(
                _finite(section.get("max_calls_per_session"), 40.0)),
            log_only=bool(section.get("log_only", True)),
            endpoint=str(section.get("endpoint") or DEFAULT_ENDPOINT),
            max_output_tokens=int(
                _finite(section.get("max_output_tokens"), 512.0)),
            thinking_budget=(
                None if section.get("thinking_budget") is None
                else int(_finite(section.get("thinking_budget"), 0.0))),
            logger=logger,
        )

    # -- public API -----------------------------------------------------
    @property
    def key_count(self) -> int:
        return len(self._keys)

    def submit(self, snapshot: Dict[str, Any]) -> bool:
        """Enqueue one snapshot.  Returns False when dropped.  Never raises."""
        try:
            if not isinstance(snapshot, dict) or not snapshot:
                return False
            key = self._dedupe_key(snapshot)
            if key in self._seen:
                return False
            if self._queue.full():
                self._dropped += 1
                return False
            self._seen.add(key)
            self._ensure_thread()
            self._queue.put_nowait(dict(snapshot))
            return True
        except Exception:
            self._dropped += 1
            return False

    def poll(self) -> List[Dict[str, Any]]:
        """Drain completed verdict records (thread-safe)."""
        with self._lock:
            if not self._results:
                return []
            drained = list(self._results)
            self._results.clear()
        return drained

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            completed = len(self._results)
        return {
            "model": self.model,
            "keys": [entry["id"] for entry in self._keys],
            "calls": self._calls,
            "dropped": self._dropped,
            "errors": self._errors,
            "queued": self._queue.qsize(),
            "completed": completed,
            "started_at": self._started_at,
        }

    def close(self, timeout: float = 3.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            try:
                self._queue.put_nowait({"_sentinel": True})
            except Exception:
                pass
            thread.join(timeout=max(0.1, float(timeout)))

    # -- internals ------------------------------------------------------
    @staticmethod
    def _dedupe_key(snapshot: Dict[str, Any]) -> Tuple[str, str, str, str]:
        return (
            str(snapshot.get("underlying") or "").upper(),
            str(snapshot.get("strategy") or ""),
            str(snapshot.get("bar_ts") or ""),
            str(snapshot.get("direction") or "").lower(),
        )

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if self._stop.is_set():
            self._stop.clear()
        self._thread = threading.Thread(
            target=self._worker, name="llm-judge", daemon=True)
        self._thread.start()

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if not isinstance(item, dict) or item.get("_sentinel"):
                continue
            try:
                record = self._judge(item)
            except Exception as exc:  # never let the worker die
                record = self._base_record(item, status="error",
                                           error=f"{type(exc).__name__}: {exc}")
                self._errors += 1
            with self._lock:
                self._results.append(record)

    def _base_record(self, snapshot: Dict[str, Any], *, status: str,
                     **extra: Any) -> Dict[str, Any]:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "underlying": str(snapshot.get("underlying") or "").upper(),
            "strategy": str(snapshot.get("strategy") or ""),
            "direction": str(snapshot.get("direction") or "").lower(),
            "option_type": str(snapshot.get("option_type") or "").upper(),
            "bar_ts": snapshot.get("bar_ts"),
            "trigger": snapshot.get("trigger"),
            "spot": snapshot.get("spot"),
            "vwap": snapshot.get("vwap"),
            "atr": snapshot.get("atr"),
            "extension_atr": snapshot.get("extension_atr"),
            "orb_high": snapshot.get("orb_high"),
            "orb_low": snapshot.get("orb_low"),
            "premium": snapshot.get("premium"),
            "provider": "gemini",
            "model": self.model,
            "log_only": self.log_only,
            "status": status,
        }
        record.update(extra)
        return record

    def _pick_key(self) -> Optional[Dict[str, Any]]:
        now = time.monotonic()
        count = len(self._keys)
        for offset in range(count):
            entry = self._keys[(self._key_cursor + offset) % count]
            if entry["cooldown_until"] <= now:
                self._key_cursor = (self._key_cursor + offset + 1) % count
                return entry
        return None

    def _judge(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        if self.max_calls_per_session and self._calls >= self.max_calls_per_session:
            return self._base_record(snapshot, status="skipped_quota")

        attempts = min(3, len(self._keys))
        last_error: Optional[str] = None
        for _ in range(attempts):
            entry = self._pick_key()
            if entry is None:
                return self._base_record(
                    snapshot, status="skipped_no_key",
                    error=last_error or "all_keys_cooling_down")
            prompt = build_prompt(snapshot)
            started = time.monotonic()
            self._calls += 1
            try:
                text = self._transport.generate(
                    prompt,
                    key=entry["secret"],
                    model=self.model,
                    timeout=self.timeout_seconds,
                )
            except JudgeTransportError as exc:
                latency_ms = int((time.monotonic() - started) * 1000)
                last_error = str(exc)
                if (exc.status in _KEY_COOLDOWN_STATUS
                        or exc.reason in _KEY_COOLDOWN_REASONS):
                    entry["failures"] += 1
                    if exc.reason in _PERMANENT_KEY_REASONS:
                        # A dead key cannot recover inside this session.
                        entry["cooldown_until"] = float("inf")
                    else:
                        entry["cooldown_until"] = time.monotonic() + 60.0
                    continue  # one immediate fallback key, never a sleep
                if exc.status is not None and exc.status >= 500:
                    # 503 is Gemini shedding load, not a bad key.  Try the next
                    # key once; never punish the key for it.
                    continue
                self._errors += 1
                return self._base_record(
                    snapshot, status="error", error=last_error,
                    latency_ms=latency_ms, key_id=entry["id"])
            except Exception as exc:
                self._errors += 1
                return self._base_record(
                    snapshot, status="error",
                    error=f"{type(exc).__name__}: {exc}",
                    latency_ms=int((time.monotonic() - started) * 1000),
                    key_id=entry["id"])

            latency_ms = int((time.monotonic() - started) * 1000)
            verdict = parse_verdict(text, snapshot.get("direction", ""))
            if not verdict.get("ok"):
                self._errors += 1
                return self._base_record(
                    snapshot, status="invalid_verdict",
                    error=verdict.get("error"),
                    latency_ms=latency_ms, key_id=entry["id"])
            return self._base_record(
                snapshot, status="ok", latency_ms=latency_ms,
                key_id=entry["id"],
                model_version=getattr(self._transport, "last_model_version", None),
                verdict={
                    "bias": verdict["bias"],
                    "trend_15m": verdict["trend_15m"],
                    "confidence": verdict["confidence"],
                    "reason_code": verdict["reason_code"],
                    "agrees": verdict["agrees"],
                    "disagree": verdict["disagree"],
                })

        if self._pick_key() is None:
            return self._base_record(
                snapshot, status="skipped_no_key",
                error=last_error or "all_keys_cooling_down")
        self._errors += 1
        return self._base_record(
            snapshot, status="error",
            error=f"all_keys_failed:{last_error}")


def build_shadow_judge(mode_cfg: Dict[str, Any],
                       logger: Optional[Any] = None) -> Optional[ShadowJudge]:
    """Convenience wrapper that never raises."""
    try:
        return ShadowJudge.from_mode(mode_cfg, logger=logger)
    except Exception as exc:
        if logger is not None:
            logger.warning("llm_judge disabled: %s", exc)
        return None


__all__ = [
    "GeminiTransport",
    "JudgeTransportError",
    "ShadowJudge",
    "build_prompt",
    "build_shadow_judge",
    "parse_verdict",
    "resolve_keys",
]
