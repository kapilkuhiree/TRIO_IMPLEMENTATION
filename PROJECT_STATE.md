# TRIO — Project State
Last updated: 2026-10-06 (evening: 6 live-session bugs fixed + MegaBull default, 124/124)

## MegaBull is now the DEFAULT paper broker (verified key + smoke)
- `src/broker/megabull.py`: MIS fills on the free simulator; P&L authoritative
  on their ledger (`/api/user/my` + `/api/position/my`). 7 mocked-HTTP tests.
- Account: Shreyash Laddha, Rs5,00,000 virtual, PRO till 2026-10-13.
  Key in `.env` only as `MEGABULL_API_KEY` (rotate monthly from Profile;
  shared account + key was in chat: regenerate after this session).
- `risk_management.capital: 500000` so sizing scales (1.5% = Rs7,500/trade,
  20% exposure cap, 5% daily halt = Rs25,000). `TRIO_BROKER_PROVIDER`
  env flips paper|megabull without editing config; test suite pins paper.
- Symbol map: strip `.NS` -> tradingSymbol, 6076-row CSV cached. MIS allows
  short-first. No bracket/OCO: our guard loop owns stop/target exits.
- LIVE (5000) + DEV (5001) both running it: equity Rs500,000 from their end.
- PENDING: market-hours smoke — first real MIS order + exit alert tomorrow
  09:20+ IST (session closed when this shipped; fail-closed on error).

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
   Real today: Rs0 booked (0 real closes), −47 floating, −700 was test junk.

## Telegram alerts LIVE + session-hours guard (verified 21:28 IST)
- Entry/exit alerts wired into paper loop; test message DELIVERED to phone.
- Fixed live 400 Bad Request: `_md_escape()` on all dynamic text
  (tech_score= lines broke Markdown parsing). Regression test pinned.
- Fixed live 21:23 after-hours SELL: `_session_open()` blocks new entries
  outside 09:20–15:15 IST; stops/targets still managed. Replay tool exempt.
  4 boundary tests. Full suite: 93/93 pass.

## Current verdict: FREEZE — no more filters, run forward paper
- Baseline verified: compile clean, 81/81 tests pass.
- Config frozen at the measured values:
  - shorts ON (all 21-day replays ran with shorts; NIFTY +2963, BANKNIFTY +993)
  - 1.5R target (all session replays ran at 1.5R; 2.0R untested here)
  - ADX regime filter OFF (helped NIFTY +83 on n=15, bled BANKNIFTY 993→277)
  - time stop OFF (monotonically worse at every tighter setting)
  - no open-window ban (would have deleted the best bucket: +1662 at bars 0-1)
- Three hypotheses killed by data this session: time stop, open-window ban,
  ADX filter. The backtest well is dry at n=18-21 trades — next evidence
  must come from forward paper logs, not more replay tuning.
- Short-side evidence is thinner than long-side: 21 days of session replay
  vs 5y daily OOS validation. Stated honestly in config comments.

## Full verification (12:08 IST)
- Compile: src + main.py + scripts clean.
- Tests: 79/79 pass (settlement, screener, pullback gate, MACD short
  check, swing stop, trailing-disabled, PASS/FAIL ledger).
- Live daily scan (5 names): 5 HOLD, 0 SELL, 0 BUY. Bearish composites
  (-0.45 to -0.83) correctly HOLD in long-only mode.
- Session replay (RELIANCE 15m x3): 0 trades, every refusal named.
- Paper loop: "2 scanned, 0 actionable, 0 picked", balance 100000,
  session P&L 0.00 (0 PASS / 0 FAIL — nothing traded, nothing scored).
- Engine LONG-ONLY: BUY + HOLD. Shorts need entry_strategy=signals AND
  allow_shorts=true (MIS setup, off by default).

## Screener: the bot now chooses (BUILT + VERIFIED)
- New `src/screener.py`: scans a basket, keeps signals surviving risk sizing,
  ranks them `confidence x edge-in-ATR x setup-bonus` (pullback-long 1.5,
  composite-short 1.0, other 0.5). Entries inside 0.25 ATR of their stop
  score zero. Returns top N above `min_rank`; empty screen is normal.
- NIFTY50 basket in code (50 tickers; unknown ones skipped, scan never dies).
- `PaperTrader.scan_once` now screens, ranks, executes top 3 only.
- Dashboard signals table shows rank + setup name per row.
- Verified live: 3-symbol 15m scan → "3 scanned, 0 failed, 0 actionable,
  0 picked", balance untouched. 6 new tests. 72/72 pass.

## Paper loop runs clean on live NSE data (verified 09:55–09:56 IST)
- `main.py paper --symbols ... --timeframe 15m --interval 60 --max-iterations 2`
  completed 2 full scans: ~1,400 candles/symbol fetched, 6 signals, 6 HOLD,
  balance untouched at 100000. No errors, no exceptions.
- Bug fixed on the way: `history_period: 2y` broke every intraday fetch
  (yfinance rejects sub-daily ranges beyond ~60d). Split config into
  `history_period` (daily+) and `intraday_history_period: 59d`, and
  `fetch_market_data` picks by interval automatically.

## Mistake learned from the tape: shorts fired into bottoms (FIXED)
- Full 15-name daily scan showed SELL on positive composites (HDFCBANK +0.27)
  and silence on bullish names. Root cause: stale MA votes vs turned-up MACD.
- Fix: `_macd_confirms_short()` — SELL needs MACD below its signal line.
  Refusals print "SHORT BLOCKED...". Post-fix: 11 confirmed SELL, 4 HOLD.
- 15m entry variant FAILED validation (test PF 0.04–0.55) and stays disabled.
  Daily pullback setup (200/50, swing stop, 1.5R) is the only live strategy.
