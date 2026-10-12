# TRIO — Project State
**Commit:** See `git log -1` (current development commit).
**Tests:** 255 passed (`python -m pytest -q --basetemp ...`), compile clean.
**Last update:** 2026-10-12 (remote refresh safety, durable execution state, wider options pool).

## Modes right now
- **Broker:** `config broker.provider: megabull` (remote MegaBull paper). Local
  paper remains available with `TRIO_BROKER_PROVIDER=paper`; tests force it.
  Unknown provider raises and live mode requires explicit confirmation.
  **Order state machine**: SUBMITTED → PENDING → FILLED/REJECTED/UNKNOWN. 
  PENDING/UNKNOWN never book risk or fire alerts; only FILLED triggers `record_confirmed_fill()`.
- **Options:** enabled for the MegaBull paper experiment. Equity uses the top
  configured candidates; options inspect a wider ranked pool
  (`screener.options_max_candidates`) and retain local shadow fallback when a
  remote leg is rejected.
- **Shorts:** `config trading.allow_shorts: false` (Experiment A long-only).
- **Market clock:** `config/market.yaml` authoritative — IST only.
- **Risk:** T1 live; T2 shadow (`enabled_phase2: false`). 
  Halts: daily_loss (5%), weekly_loss (configurable), max_positions (dynamic),
  consecutive_loss, drawdown (from high watermark), broker_uncertain.
  **Weekly rollover implemented** (ISO week, restart-safe via equity_snapshot.json).
  **High watermark persistence**: restored from `output/equity_snapshot.json` on restart.

## Execution (single lifecycle — no more path forks)
`src/execution_lifecycle.py`: `reconcile_broker` (2 healthy refreshes for
remote, 1 for paper; unresolved orders or failed remote refresh → uncertain)
→ `preflight` → `submit_candidate` → `record_confirmed_close` (idempotent via
close_id/order_id dedup). Remote option spreads remain open until every leg
close is confirmed.
Ladder guard broker-agnostic; each broker only executes `close_position`.

## Backtest (execution-realistic)
Next-bar `Open` fills, stop gaps, stop-before-target, notional costs per leg,
unrealized equity, Sharpe by bars/year, `skipped_bars`. Exact-value tests.

## Known defects / open work (honest)
- `exit_policy` is wired into the live guard, but the historical
  `Backtester`/`day_session` replay still needs the same ladder state machine
  for exact live/backtest parity.
- Failure-injection coverage now includes refresh failure, uncertain remote
  reconciliation, failed option exits, durable streak ordering, and wider
  option selection; a live remote soak test with delayed/partial fills remains.
- Risk preflight now fails closed if sector or margin calculations raise.
- Sector map covers NIFTY 50; unknown symbols → `Unknown` sector.
- Strategy evidence: Experiment A (long-only local paper) not yet run 60 sessions.
- NSE holiday data is still configured manually in `config/market.yaml`.

## Next action (P0 → P3)
P0: Run a MegaBull remote-paper soak with delayed/partial fills and verify the
     persisted `output/equity_snapshot.json` and `output/runtime` registry.
P1: Wire `exit_policy` into `Backtester` and `day_session` for parity.
P1: Replace remaining weak assertions with exact behavioral checks.
P2: Run the wider options opportunity pool for 60 forward-paper sessions with
     costs and compare option candidates against executed equity picks.
P3: Replace manual holidays with an NSE exchange calendar and publish the
     operator runbook.
