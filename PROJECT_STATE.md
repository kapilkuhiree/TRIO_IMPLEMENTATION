# TRIO — Project State
Last updated: 2026-10-07 (MegaBull default + ladder T1 live, 138/138, +4569.87 today)

## Today scored — first winning session on LIVE + MegaBull (verified)
- LIVE `TRIO_IMPLEMENTATION` WON: **+4569.87 (+0.91%)**, equity 499819.83 -> 504389.70
  (provider=megabull, 92 scans today, 0.0 used_margin — no leverage).
- 13 entries (all SELL, composite-short, 15m): 2 booked intraday via
  `partial@+1R`, 11 held to the 15:15 close (*see Ladder gap below*).
- 13 exits: 8 PASS / 5 FAIL = **61.5% win** (matches the 60.5% walk-forward).
- Entry/Exit Telegram alerts live: exit wiring was on PAPER but never on
  MegaBull (MKT exits 7713xx) — now on BOTH providers with P&L+reason.
- Key in `.env` only as `MEGABULL_API_KEY` (real key wired after chat;
  shared account Shreyash Laddha — rotate monthly).

## MegaBull is now the DEFAULT paper broker (verified key + smoke)
- `src/broker/megabull.py`: MIS fills on the free simulator; P&L authoritative
  on their ledger (`/api/user/my` + `/api/position/my`). 11 mocked-HTTP tests.
- Symbol map: strip `.NS` -> tradingSymbol, 6076-row CSV cached. MIS allows
  short-first. No bracket/OCO: our guard loop owns stop/target exits.
- `risk_management.capital: 500000` so sizing scales (1.5% = Rs7,500/trade,
  20% exposure cap, 5% daily halt = Rs25,000). `TRIO_BROKER_PROVIDER`
  env flips paper|megabull without editing config; test suite pins paper.
- LIVE running on `TRIO_IMPLEMENTATION:5000` will take tomorrow's trades;
  DEV (`TRIO_IMPLEMENTATION_NEW:5001`) stays the lab. Both on megabull
  provider.

## Ladder Scaled Exits — Phase 1 built (T1 @ 0.8R: 50% + breakeven)
**Gap closed:** 11/13 trades sat 355 mins to EOD=flat because 1.5R was too
far. Today a full `T1/T2/T3` ladder (Entry->T1 50% -> breakeven -> T2/T3)
was discussed; for tomorrow Phase 1 (T1 only) is armed:
- `risk_manager.py`: `ladder_targets(entry, stop, action)` -> {T1: 0.8R}.
- `config`: `breakeven_at_r: 0.8`, `partial_at_r: 0.8`, `partial_fraction: 0.5`.
- `paper_trader.py`: `_guard_open_positions()` closes 50% at T1 and moves
  the remaining stop to breakeven (risk-free). Hard stops mastered in
  `_manage_open_positions()`. Guard skips megabull mirrors (remote safety).
- Alerts: `format_exit_message()` marks `partial@T1-0.8R` as `🔹 PARTIAL 50%`;
  Telegram entry alert shows `T1: ... (50% + breakeven)`.
- Tested: `test_ladder_t1_partial_and_breakeven` + `test_ladder_targets_derived`.
  **Future:** when 21-day replay proves it, arm T2 (1.5R -> T1) and T3 (2.5R).

## Six live-session defects fixed today (root-caused from trade_log.jsonl)
1. Broker SELL-on-SHORT silently deleted the position (`paper.py`): now
   side-aware fills; every close records to `closed_trades` + alerts.
2. Balance double-count (equity 857,470 from 10k): equity = cash + LONG
   value − SHORT owed; SHORT margin capped at initial capital.
3. Hold-skip: same-side re-entry skipped+logged (was ULTRACEMCO 8x/5min).
4. EOD square-off inside `scan_once` (was Ctrl+C only): 2026-10-06 held
   2 positions 25 min past close with no exit/alert/record.
5. Setup classifier from readings, not the `MACD` vs `macd` string match
   (83% of live orders were `other`).
6. Log `mode`+`broker` tags; test events -> `trade_log_test.jsonl`. Today's
   polluted log archived as `trade_log_20261006_contaminated.jsonl`.
   Real today 2026-10-06: Rs0 booked (0 real closes), −47 floating, −700 was test junk.

## Telegram alerts LIVE + session-hours guard (verified 21:28 IST)
- Entry/exit alerts wired into paper loop; test message DELIVERED to phone.
- Fixed live 400 Bad Request: `_md_escape()` on all dynamic text
  (tech_score= lines broke Markdown parsing). Regression test pinned.
- Fixed live 21:23 after-hours SELL: `_session_open()` blocks new entries
  outside 09:20–15:15 IST; stops/targets still managed. Replay tool exempt.
  4 boundary tests. Full suite: 93/93 pass.

## Current verdict: Ladder Phase 1 live — run forward paper
- Baseline verified: compile clean, 138/138 tests pass.
- Config frozen at the measured values:
  - shorts ON (all 21-day replays ran with shorts; NIFTY +2963, BANKNIFTY +993)
  - 1.5R final target (all session replays ran at 1.5R; 2.0R untested here)
  - **Active Manager T1 0.8R: 50% + breakeven** (ladder Phase 1)
  - ADX regime filter OFF (helped NIFTY +83 on n=15, bled BANKNIFTY 993→277)
  - time stop OFF (monotonically worse at every tighter setting)
  - no open-window ban (would have deleted the best bucket: +1662 at bars 0-1)
- Short-side evidence is thinner than long-side: 21 days of session replay
  vs 5y daily OOS validation. Stated honestly in config comments.

## Tomorrow morning (restart required — dashboard != trader)
- **Before 9 AM:** `cd "C:\Users\Kapil Kuhire\Downloads\TRIO_IMPLEMENTATION"; python scripts\dashboard.py --port 5000`
  (leave window open; browser http://127.0.0.1:5000).
- At 09:20 entry alerts now show `T1: ... (50% + breakeven)`; a T1 hit
  sends a `🔹 PARTIAL 50% — ... breakeven` Telegram alert mid-session.
- Verify first close has P&L+reason+exit time (wired for both brokers;
  proved live 2026-10-06).

## Full verification (17:29 IST)
- Compile: src + scripts clean.
- Tests: 138/138 pass (ladder: partial+breakeven, targets, megabull id-row).
- Live 15m scan (15 names) -> board; 92 scans today.
- Dashboard: provider megabull, equity 504389.70, scans 92, no error.
