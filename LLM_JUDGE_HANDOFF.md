# Shadow LLM judge (Gemini) — handoff

A **log-only** second opinion on every tradable options signal. It never
blocks, sizes, prices or alters an order. Its only effect is one extra line in
`output/trade_log.jsonl`:

```json
{"event": "llm_verdict", "bar_ts": "...", "underlying": "NIFTY",
 "strategy": "orb", "direction": "bearish", "status": "ok",
 "model": "gemini-3.5-flash", "model_version": "gemini-3.6-flash",
 "key_id": "GOOGLE_GEMINI_API_KEY[1]", "latency_ms": 1349,
 "verdict": {"bias": "short", "trend_15m": "down", "confidence": 0.85,
             "reason_code": "vwap_reject", "agrees": true, "disagree": false}}
```

## 1. Why it exists

Today's ORB book took the same short signal four times (13:40, 13:55, 14:09,
14:20). Trade 2 was already 3.28 ATR beyond the OR low when it was entered and
was the day's only loser. A trend judge is cheap to run in the background and
produces a paired dataset — system decision vs model opinion vs outcome —
before anyone considers promoting it to a filter.

**It is not a filter today.** Nothing in the entry path reads the verdict.

## 2. Guarantees

| Concern | Behaviour |
| --- | --- |
| Latency on the order path | `submit()` only enqueues; all HTTP happens on a daemon thread |
| Exceptions | Swallowed in `submit`, `poll` and the worker; the worker records `status: "error"` |
| Order flow | No code reads `verdict`; `log_only: true` is recorded with each row |
| Queue pressure | Bounded (`max_queue: 8`); a full queue drops the snapshot and counts `dropped` |
| Cost | One call per `(underlying, strategy, bar_ts, direction)`, hard-capped at `max_calls_per_session` |
| Keys | Rotated round-robin; a key-scoped error cools that key for 60 s; `API_KEY_INVALID` disables it for the session |
| Kill switch | `TRIO_LLM_JUDGE_ENABLED=0` |

Sleeves observed: `orb` and `orb_retest` only. Shadow sleeves are excluded —
they never place an order, so an opinion on them has no decision to attach to.

## 3. Configuration

`config/options_intraday.yaml`:

```yaml
llm_judge:
  enabled: true
  provider: gemini
  model: "gemini-3.5-flash"
  thinking_budget: 0
  max_output_tokens: 512
  timeout_seconds: 6.0
  max_queue: 8
  max_calls_per_session: 120
  log_only: true
  keys_env: [GEMINI_KEY_1, GEMINI_KEY_2, GEMINI_KEY_3, GEMINI_KEY_4,
             GOOGLE_GEMINI_API_KEY]
```

Keys are read from the environment first, then directly from `.env`. One
variable may hold several comma-separated keys; they are split and
de-duplicated, so `.env` can keep the existing single `GOOGLE_GEMINI_API_KEY`
line with four keys in it.

### Two traps that cost a session each

1. **Retired model.** `gemini-2.5-flash` still appears in `GET /v1beta/models`
   but `generateContent` returns `404 NOT_FOUND` ("no longer available to new
   users"). A valid key is not proof of a valid model. `gemini-3.5-flash` and
   `gemini-flash-latest` both answer.
2. **Thinking tokens.** Reasoning tokens are billed against
   `maxOutputTokens`. At 256 the model spent the whole budget thinking and the
   JSON arrived truncated (`invalid_json`). `thinking_budget: 0` makes the call
   a ~1.4–2.6 s lookup with a complete object. The lite models reject
   `thinkingConfig` with `INVALID_ARGUMENT`, so do not copy this setting to
   them without testing.

`scripts/llm_judge_check.py` validates both layers — list *and* probe — and
prints the concrete `modelVersion` that answered:

```
python scripts/llm_judge_check.py
python scripts/llm_judge_check.py --no-probe
```

Status as of 2026-11-11: 3 of the 4 keys in `.env` answer; key 4 returns
`API_KEY_INVALID` and is skipped silently at runtime.

## 4. Files

| File | Role |
| --- | --- |
| `src/llm_judge.py` | Transport, key resolution/rotation, prompt, verdict parsing, `ShadowJudge` |
| `src/options_live_scan.py` | `_judge_snapshot()` — pure read-only snapshot attached to each ranked candidate |
| `src/options_intraday_runtime.py` | `_submit_judge` / `_drain_judge`, `llm_verdict` logging, drain in `tick()` and `finalize()` |
| `scripts/llm_judge_check.py` | Key + model validation |
| `scripts/llm_report.py` | Joins verdict → order → exit and scores the buckets |
| `tests/test_llm_judge.py` | 32 tests: parsing, config, worker, transport, key lifecycle |
| `tests/test_judge_snapshot.py` | 5 tests: snapshot shape, shadow exclusion, incomplete OR |
| `tests/test_llm_report.py` | 4 tests: join, window, unusable-vs-neutral |

## 5. Reading the result

```
python scripts/llm_report.py --start 2026-11-12 --end 2026-11-12
```

Buckets are `agreed`, `disagreed`, `neutral`. `unusable_verdicts` counts failed
or skipped calls and is deliberately **not** folded into `neutral`: neutral
means the model looked and saw no edge.

Only after roughly 20 matched trades in each of `agreed` and `disagreed` is the
P&L gap between them meaningful. One session is noise.

## 6. First evidence on today's data (13:50 snapshot)

Replayed offline against the real 13:50 bar (extension 3.28 ATR):

| Bar | Extension | Model verdict |
| --- | --- | --- |
| 13:30 | 1.70 ATR | `short`, 0.85, `vwap_reject` — agrees |
| 13:50 | 3.28 ATR | `short`, 0.85, `vwap_reject` — **agrees with the losing trade** |
| 14:05 | 0.27 ATR | `neutral`, 0.75, `counter_trend` |

So the first sample does **not** support using the judge as an over-extension
filter: it endorsed the 3.28 ATR entry. Two of the two directional calls agreed
with the system. Treat the judge as an independent label until the paired count
justifies more, and keep the candidate "distance from trigger" check as a
separate, rule-based idea.
