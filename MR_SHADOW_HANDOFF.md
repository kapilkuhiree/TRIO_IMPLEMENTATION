# MR Shadow Handoff — next-session checklist

_Compacted from the 2026-11-09 design session. Stage 1 was implemented on 2026-11-10 (see §5a)._

## 1. Current state (do not disturb)

- Options-only stack: `orb`, `orb_retest`, `mean_reversion`.
- Caps: `max_daily_entries: 10`, `max_open_positions: 2`, `max_trades_per_day: 10`, `orb_confirm_closes: 2`.
- Entry floors: ORB `10:00`, ORB-Retest `09:45`, MR none.
- Execution: MegaBull `remote_paper`. **Live NFO is OFF.**
- `PAPER_TRADING: true`, `execution_class: remote_paper`.
- Do not revert `strategies/orb_strategy.py` or `config/options_intraday.yaml`.
- All work is uncommitted. Baseline test suite: **770 passed**.

## 2. Key finding — MR never trades

Evidence from `output/trade_log.jsonl` and the runtime state:

- MR produced a raw signal in **387 scans** across 2026-11-03 → 11-09.
- **Zero MR orders/exits.** All 11 closed trades in state are `"strategy": "orb"`.
- Of 587 per-symbol MR signals:
  - **326 (55%)** dropped by `vwap_counter_trend(...)`.
  - **159** collided with ORB on the same ATM contract (planner selected 2; global dedupe is contract-only).
  - **102** survived to the pool but produced nothing — same-contract intent collision and silent `continue`.
- Root causes, in order:
  1. **VWAP filter is anti-MR by design** — `src/options_live_scan.py:198-214`: CE only above VWAP, PE only below. MR buys pullbacks against the move.
  2. **Contract-only keys** — `_contract_key` (`options_live_scan.py:510-532`) and `_candidate_key` (`options_intraday_runtime.py:1202-1208`) ignore strategy, so MR and ORB collide. Tie-break `candidate_id` sorts `MEAN_REVERSION_*` before `ORB_*`, so MR can steal ORB's contract if both enter the same pool.
  3. **No reserved slot / strategy priority** — the pool is ranked by contract quality, not strategy.
  4. **Observability bug** — `options_scan` is logged at `options_intraday_runtime.py:1516`, before the execution loop, so `entry_rejections` always looks empty and intent skips are silent.

## 3. Stage 0 result — offline MR replay (done)

```powershell
python scripts/premium_replay.py --strategies mean_reversion
```

- Window 2026-08-19 → 2026-11-04 (40 sessions).
- **75 model MR trades, 0 matched (0.0%).**
- Misses: `no_option_candles` 46, `strike_drift` 21, `no_contract` 6, `unfillable_first_entry_candle` 2.
- Model MR: net ₹26,521, PF 1.35.

Archive coverage (sidecar, read-only check):

| Date | Underlying | Executable | Strikes |
|---|---|---|---|
| 2026-10-21 | NIFTY | 1,440 | 9 |
| 2026-10-21 | BANKNIFTY | 1,440 | 10 |
| 2026-10-20 | NIFTY / BANKNIFTY | 0 | 14 / 20 |
| 2026-10-25 | BANKNIFTY | 0 | 5 |

Only **one executable session**, and MR had no signal on it. The 0/75 result is a **coverage failure, not an MR quality result**. Do not tune, promote or reject MR from it.

## 4. Model evidence (context only, not proof)

From `orb_retest_v2_backtest.json` (60 sessions, 2026-08-13 .. 2026-11-09,
ATM BS, flat 18% IV; regenerated after the stateless confirmation fix):

| Sleeve | Trades | Win % | Net | PF | t-stat |
|---|---|---|---|---|---|
| ORB live | 188 | 46.8 | ₹42,861 | 1.19 | 1.02 |
| MR | 69 | 55.1 | ₹37,111 | 1.57 | 1.57 |
| ORB + C + Retest + MR (active book) | 209 | 57.4 | ₹1,58,713 | 1.80 | 3.67 |

MR looks better than ORB stand-alone, but its t-stat is still weak and real IV
medians (14–16%) are below the model's 18%. Forward evidence is required.

## 5. Decision — Stage 1 shadow first

Stage 1 is **MR-only and additive**. ORB, ORB-Retest and the VWAP filter must behave exactly as today.

### Design rules (non-negotiable)

1. Config gate: `shadow_strategies: [mean_reversion]`. Empty/absent = current behaviour.
2. MR candidates go into a **separate `shadow_candidates` list**, collected **before** VWAP and dedupe.
3. Shadow candidates must **never** enter `ranked`, `seen`, `snapshots`, or `execute_plan`.
4. Shadow must **not** create intents, increment `daily_entries`, touch risk state, reserve capital, or call the broker.
5. Do **not** edit `_vwap_allows` or ORB's call to it. In the shadow pass, call it separately for MR and record the result.
6. Reuse the candles/chain already fetched. No extra network calls (avoids latency/rate-limit effects on ORB).
7. Log `shadow_signal` with: timestamp, symbol, direction, selected contract, would-be premium, `vwap_would_block`, close vs VWAP, collision flag, hypothetical outcome.
8. Fix the observability gap: log `entry_rejections` after the execution loop; replace silent intent `continue` with an explicit skip reason (e.g. `contract_held_by_other_strategy`).

### Files planned to change (not yet touched)

- `config/options_intraday.yaml` — add `shadow_strategies: [mean_reversion]`
- `src/options_live_scan.py` — additive shadow collection before VWAP/dedupe
- `src/options_intraday_runtime.py` — log-only shadow branch + post-loop rejection logging
- `scripts/forward_report.py` — shadow section (signals, blocked reasons, hypothetical P&L)
- `tests/test_options_live_gate.py`, `tests/test_options_intraday_runtime.py`, plus a new shadow test

### Tests required

1. Shadow off → scan output identical to current behaviour.
2. Shadow on → ORB candidate set and order identical.
3. Shadow candidates never reach `execute_plan`, never create intents, never increment `daily_entries`.
4. ORB's VWAP allowed/blocked decision unchanged with shadow on/off.

### 5a. Stage 1 implementation status (2026-11-10)

Implemented and tested. Full suite: **775 passed**.

- `config/options_intraday.yaml` — `shadow_strategies: [mean_reversion]`.
- `src/options_live_scan.py` — `shadow` list collected before VWAP/dedupe;
  shadow strategies are excluded from `ranked`/`seen`/`snapshots`; VWAP is
  evaluated separately for each shadow record; `_vwap_allows` unchanged.
  Diagnostics: `shadow_signals`, `shadow_vwap_blocked`,
  `shadow_contract_collisions`.
- `src/options_intraday_runtime.py` — writes `shadow_signal` log rows only;
  never creates an intent, order, `daily_entries`, or risk state. The
  `options_scan` event now fires **after** the execution loop and carries
  `submitted` + `entry_rejections`; intent collisions log an explicit
  `contract_held_by_other_strategy` / `contract_intent_exists` reason.
- `scripts/forward_report.py` — log-only shadow section (raw vs unique,
  sleeve, VWAP-block count, collision count, by day). Shadow is never counted
  as a trade or an order.
- Tests: `tests/test_options_live_scan.py`, `tests/test_options_intraday_runtime.py`,
  `tests/test_forward_report.py`.

Known limitation: a shadow record is emitted only when the planner actually
selects an eligible contract. If the planner rejects MR upstream, there is no
candidate to log, so shadow diagnostics will show nothing for that scan. This
keeps Stage 1 strictly additive — it does not add planner work or fetches.

### 5b. Capture + shadow replay (2026-11-10)

- `scripts/zerodha_live_capture.py` — repeatable `--strike-range
  UNDERLYING=MIN:MAX` (e.g. `--strike-range NIFTY=22000:23000
  --strike-range BANKNIFTY=54000:56000`). `--strike-min/--strike-max` remain
  as the global fallback for underlyings without an override.
- `src/zerodha_live_capture.py` — `CaptureSettings.strike_ranges` applies the
  per-underlying window after one union discovery pass; invalid ranges fail
  closed. `--max-contracts` is still optional; leave it off for coverage.
- `scripts/shadow_replay.py` — replays distinct logged `shadow_signal` rows on
  real archived bid/ask using the same pricing engine as
  `scripts/premium_replay.py`; reports matched/missed signals and hypothetical
  gross/net/PF. This is the Stage 1 evidence tool.
- Tests: `tests/test_shadow_replay.py`, plus per-underlying range tests in
  `tests/test_zerodha_live_capture.py`, and a shipped-config guard in
  `tests/test_options_live_gate.py`.

Full suite after 5a + 5b: **784 passed**.

### 5c. ORB confirmation wiring fix (2026-11-10)

`orb_confirm_closes: 2` existed in the config but was dead: the pure scan
boundary constructed `OrbStrategy()` with the default of 1, and the old
instance counters never reached 2 because the live scan creates a fresh
strategy object per scan.

- `strategies/orb_strategy.py` — confirmation now counts trailing same-session
  closes from the candle history (`_consecutive_closes_beyond`) instead of
  instance state. A close back inside the range resets progress. This gives
  the live scan and the sequential backtest identical semantics.
- `candidate_pipeline.py` — `evaluate_strategy_signals_pure(...,
  orb_confirm_closes=...)` passes the count into `OrbStrategy`.
- `src/options_live_scan.py` — reads `orb_confirm_closes` from the effective
  config (default 1) and forwards it. The shipped overlay therefore enforces
  two consecutive closes in paper and live.
- Tests: `tests/test_orb_confirm_closes.py` (fresh-instance history, reset on
  inside close, bearish symmetry, pure-pipeline plumbing) and a config
  forwarding test in `tests/test_options_live_scan.py`. Existing routing
  fixtures pin `orb_confirm_closes: 1` so they keep testing their own policy.

Full suite after 5c: **790 passed**. `orb_retest_v2_backtest.json` was
regenerated on the fixed semantics; section 4 carries the refreshed numbers.

## 6. Stages 2–3 (later, after shadow evidence)

- **Stage 2** — MR-only paper sleeve: `strategies: [mean_reversion]`, `max_open_positions: 1`, MR VWAP-safe path, 2–3 paper sessions.
- **Stage 3** — ORB + MR with reserved slots (`orb: 1`, `mean_reversion: 1`), VWAP scoped via `vwap_filter_strategies: [orb, orb_retest]`, strategy-aware intent keys. Decide ORB-Retest's slot separately.

## 7. Tomorrow / trading calendar

1. **Tue 2026-11-10** — market holiday (Diwali-Balipratipada). Read-only checks only.
2. Implement Stage 1; run the full suite (expect 770 + new tests passing).
3. **Wed 2026-11-11** — normal paper session:
   - `python scripts/session.py --trading-mode options_intraday`
   - optional wiring check: `python scripts/weekday_live.py --weekday-live 2026-11-11 --dry-run`
   - widen read-only capture (no `--max-contracts`, explicit ATM ladder via `--strike-min/--strike-max`)
   - `python scripts/zerodha_live_capture.py --cycles 75 --interval-seconds 300`
4. Evaluate shadow report: MR signals/day, VWAP-block rate, collision rate, hypothetical PF after costs.
5. **Thu 2026-11-12** — live only if Wednesday is clean:
   `python scripts/weekday_live.py --weekday-live 2026-11-12 --confirm WEEKDAY-LIVE`

## 8. Hard guardrails

- Live NFO stays off until the date-locked command is explicitly run.
- Do not recalibrate stops/targets on the thin archive (needs ≥60 executable sessions and ≥80% match).
- Do not let shadow candidates influence ORB ranking, dedupe, slots, or daily counters.
- Keep all execution on MegaBull paper until Stage 1 evidence is reviewed.
