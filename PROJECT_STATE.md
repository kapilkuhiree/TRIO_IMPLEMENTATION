# TRIO — Project State
**Commit:** `fix/production-hardening` (see `git log`; based on `origin/main 6851d82`).
**Tests:** 214 passed (`python -m pytest -q --basetemp ... -p no:randomly`), compile clean.
**Last update:** 2026-10-10 (unified execution/risk lifecycle + restart-safe rebuild).

## Modes right now
- **Broker:** `config broker.provider: megabull` (remote paper), tests pin `paper`.
  Unknown provider raises at startup; missing key raises; `mode: live` needs
  `TRIO_LIVE=1` / `live_confirmed`. PENDING/PLACED/ACCEPTED never booked as fills.
- **Options:** `config options.enabled: false` (separate experiment E). Chain code,
  bidask fill, `round_trip_cost` and remote/megabull legs remain, but the session
  attaches no legs and `execute_option_plan` only fires on explicit config.
  Shadow fallback records are tagged `venue: local_shadow` and never enter risk/equity.
- **Shorts:** `config trading.allow_shorts: false` (long-only validation A first).
  CNC settlement rule clips/skips SELL without holdings; shorts need their own
  multi-regime proof before `allow_shorts: true` returns.
- **Market clock:** `config/market.yaml` authoritative —
  `open 09:15 | entry_start 09:20 | entry_cutoff 15:00 (excl) | hard_flat 15:10 | close 15:30`,
  IST only. Invalid/wrong-TZ → management-only. Legacy `forward_test` fallback kept.
- **Risk:** T1 `0.8R 50%→breakeven` live; T2 `1.5R 30%→trail` shadow
  (`ladder.enabled_phase2: false`). Realized + marked-unrealized daily halt
  (5%), max positions (5, dynamic), per-symbol 20%, per-sector 40%, plus
  consecutive-loss and drawdown halts. Halts are per-reason:
  `daily_loss`/`consecutive_loss`/`drawdown` = session-persistent;
  `max_positions` = dynamic; `broker_uncertain` = until reconciliation.
  Restart rebuilds everything from broker + JSONL with order_id dedup + IST day
  mapping (consec losses/wins, watermark, partials, sector, risk amounts).

## Execution (single lifecycle — no more path forks)
`src/execution_lifecycle.py` owns: `reconcile_broker` (refresh + PENDING/UNKNOWN
scan + `broker_uncertain` halt + risk rebuild) → `preflight` (halt/max/dup/
sector/margin) → `submit_candidate` (CNC clip, place, pending≠filled, confirm,
register, audit, alert) → `record_confirmed_close` (pnl/consec/position/halts/
log/alert-once). `scan_once`, `execute_plan`, live rescans all call it. Sector
is transactional: every confirmed fill updates the proposed portfolio in-pass.
The ladder guard is broker-agnostic: it decides for local AND MegaBull; each
broker only executes `close_position`. `_manage_open_positions` stop/target
paths also route through `record_confirmed_close`.

## Backtest (execution-realistic)
Next-bar `Open` fills (never same-bar), stop gaps pay the worse open,
stop-before-target on shared bars, notional costs per leg, unrealized in
equity, Sharpe annualized by bars-per-year, `skipped_bars: N`. Exact-value
tests pin entry/exit/slippage/commission/gap/EOD traces.

## Known defects / open work (honest)
- Session cadence/jackson `09:40/11:00/13:00` rescans proven only on paper;
  remote-pending storms need the UNKNOWN_REMOTE_STATE poll loop tested live.
- Weekly-loss scheduler keys off `weekly_pnl` but has no weekly rollover job yet.
- Sector map covers NIFTY 50; unknown symbols fall back to `Unknown`.
- Zerodha adapter is a stub — `provider: zerodha` raises today by design.
- Strategy evidence: 5y daily long OOS + 21-day short replay + short single
  live-paper win is NOT a joint proof of shorts+megabull+ladder+options+rescans.
  Follow the A→H experiment ladder in config comments before combining features.

## Next action
1. Merge `fix/production-hardening` → `main` after one green CI + smoke.
2. Run experiment A (long-only equity, local paper) forward-paper with costs;
   ship separate long/short drawdown + regime analysis before enabling B–H.
3. Failure-injection suite: timeout, accepted-but-unconfirmed, delayed refresh,
   duplicate close, partial remote fill, restart, corrupt ledger, alert outage.
