# TRIO — Project State
**Commit:** `f9778a6` (MegaBull state machine + reconciliation + idempotent close + weekly loss + equity snapshots).
**Tests:** 221 passed (`python -m pytest -q --basetemp ... -p no:randomly`), compile clean.
**Last update:** 2026-10-11 (Phase 1 Integrity Layer complete).

## Modes right now
- **Broker:** `config broker.provider: paper` (local paper default). MegaBull via
  `TRIO_BROKER_PROVIDER=megabull` + `MEGABULL_API_KEY`. Unknown provider raises.
  **Order state machine**: SUBMITTED → PENDING → FILLED/REJECTED/UNKNOWN. 
  PENDING/UNKNOWN never book risk or fire alerts; only FILLED triggers `record_confirmed_fill()`.
- **Options:** `config options.enabled: false` (Experiment E). Shadow fallback active.
- **Shorts:** `config trading.allow_shorts: false` (Experiment A long-only).
- **Market clock:** `config/market.yaml` authoritative — IST only.
- **Risk:** T1 live; T2 shadow (`enabled_phase2: false`). 
  Halts: daily_loss (5%), weekly_loss (configurable), max_positions (dynamic),
  consecutive_loss, drawdown (from high watermark), broker_uncertain.
  **Weekly rollover implemented** (ISO week, restart-safe via equity_snapshot.json).
  **High watermark persistence**: restored from `output/equity_snapshot.json` on restart.

## Execution (single lifecycle — no more path forks)
`src/execution_lifecycle.py`: `reconcile_broker` (2 clean refreshes for remote,
1 for paper; unresolved orders → uncertain) → `preflight` → `submit_candidate`
→ `record_confirmed_close` (idempotent via close_id/order_id dedup).
Ladder guard broker-agnostic; each broker only executes `close_position`.

## Backtest (execution-realistic)
Next-bar `Open` fills, stop gaps, stop-before-target, notional costs per leg,
unrealized equity, Sharpe by bars/year, `skipped_bars`. Exact-value tests.

## Known defects / open work (honest)
- Exit policy module (`src/exit_policy.py`) exists but **not yet wired into
  `PaperTrader` guard or `Backtester`** (migration planned incremental).
- Weak test assertions remain (`or True`, `>= 0`) — need exact behavioral checks.
- Failure-injection suite started (`test_broker_failures.py`: 3 tests); needs
  expansion (partial fill, restart mid-confirmation, corrupt ledger, alert outage).
- Sector map covers NIFTY 50; unknown symbols → `Unknown` sector.
- Strategy evidence: Experiment A (long-only local paper) not yet run 60 sessions.

## Next action (P0 → P3)
P0: Wire `exit_policy` incrementally (T1 first) into `PaperTrader` & `Backtester`.
P0: Expand `test_broker_failures.py` (partial fill, restart, corrupt ledger, alert outage).
P1: Replace all weak assertions with exact behavioral checks.
P2: Run Experiment A forward-paper (60 sessions) with costs.
P3: CI quality gates, exchange calendar, operator runbook.
