"""Check the Gemini keys and model used by the shadow LLM judge.

The trading session treats a bad key or a retired model as a silent no-op,
which is the right behaviour for execution but a terrible experience when you
are wiring keys up.  Run this after editing ``.env`` or the ``llm_judge`` block:

    python scripts/llm_judge_check.py
    python scripts/llm_judge_check.py --no-probe     # skip the generate call

A key can be valid and the model can still be wrong: ``gemini-2.5-flash``
answers 200 on the models list and 404 on ``generateContent``.  So the default
is to send one tiny prompt per key.

Exit code 0 means at least one key produced a real answer with the configured
model.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.llm_judge import (  # noqa: E402
    DEFAULT_ENDPOINT,
    DEFAULT_MODEL,
    GeminiTransport,
    JudgeTransportError,
    resolve_keys,
)
from src.options_intraday import load_options_intraday_config  # noqa: E402

PROBE_PROMPT = 'Return exactly this JSON and nothing else: {"ok": true}'


def _mask(secret: str) -> str:
    if len(secret) <= 10:
        return "*" * len(secret)
    return f"{secret[:6]}...{secret[-4:]}"


def _list_models(key: str, timeout: float) -> int:
    import requests

    response = requests.get(
        "https://generativelanguage.googleapis.com/v1beta/models",
        headers={"x-goog-api-key": key},
        params={"pageSize": 1},
        timeout=timeout,
    )
    return response.status_code


def _probe(key: str, model: str, endpoint: str, timeout: float,
           max_output_tokens: int, thinking_budget) -> str:
    transport = GeminiTransport(
        endpoint,
        max_output_tokens=max_output_tokens,
        thinking_budget=thinking_budget,
    )
    text = transport.generate(PROBE_PROMPT, key=key, model=model, timeout=timeout)
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return f"non-JSON reply: {text[:60]!r}"
    version = transport.last_model_version or "?"
    if payload.get("ok") is True:
        return f"ok (served by {version})"
    return f"unexpected reply: {text[:60]!r}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Validate Gemini keys for the LLM judge.")
    parser.add_argument("--no-probe", dest="probe", action="store_false",
                        help="only list models; do not send a generate request")
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args(argv)

    cfg = load_options_intraday_config() or {}
    mode = cfg.get("options_intraday") or {}
    section = mode.get("llm_judge") or {}
    model = str(section.get("model") or DEFAULT_MODEL)
    endpoint = str(section.get("endpoint") or DEFAULT_ENDPOINT)
    max_output_tokens = int(section.get("max_output_tokens") or 512)
    thinking_budget = section.get("thinking_budget")
    if thinking_budget is None:
        thinking_budget = 0

    keys = resolve_keys(mode)
    print(f"config enabled : {bool(section.get('enabled'))}")
    print(f"model          : {model}")
    print(f"thinking budget: {thinking_budget}")
    print(f"keys found     : {len(keys)}")
    if not keys:
        print("\nNo keys. Add GEMINI_KEY_1 (and 2..4) to .env, or GOOGLE_GEMINI_API_KEY.")
        return 1

    working = 0
    for key_id, secret in keys:
        print(f"\n{key_id}  {_mask(secret)}  ({len(secret)} chars)")
        try:
            status = _list_models(secret, args.timeout)
        except Exception as exc:
            print(f"  list models : ERROR {type(exc).__name__}: {exc}")
            continue
        print(f"  list models : HTTP {status}")
        if status == 400:
            print("  -> key rejected (API_KEY_INVALID). Copy the full key from"
                  " Google AI Studio; keys start with 'AIza' and are 39 chars.")
            continue
        if status in (401, 403):
            print("  -> key not authorised for the Generative Language API.")
            continue
        if status == 429:
            print("  -> key accepted but rate limited / quota exhausted.")
        if not args.probe:
            working += 1 if status == 200 else 0
            continue
        try:
            result = _probe(secret, model, endpoint, args.timeout,
                            max_output_tokens, thinking_budget)
            print(f"  probe       : {result}")
            if result.startswith("ok"):
                working += 1
        except JudgeTransportError as exc:
            print(f"  probe       : FAILED {exc}")
            reason = str(exc)
            if "NOT_FOUND" in reason:
                print(f"  -> the key is fine; '{model}' is not servable. Try"
                      " gemini-3.5-flash or gemini-flash-latest.")
            elif "INVALID_ARGUMENT" in reason:
                print("  -> the model rejected a generation parameter"
                      " (often thinking_budget; try setting it to null).")
            elif "UNAVAILABLE" in reason or "http_503" in reason:
                print("  -> transient capacity error; rerun in a minute.")

    print()
    if working:
        print(f"{working} key(s) produced a verdict-capable answer.")
        return 0
    print("No key/model pair works. The session will keep trading; the judge")
    print("will log `status: skipped_no_key` (or `error`) and nothing else")
    print("will change.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
