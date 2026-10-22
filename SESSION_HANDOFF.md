# TRIO — Session Handoff (paste into a new chat)

## Current options-only and Zerodha research work — 2026-10-22

- Local integration complete, **not committed, pushed, or deployed** in this
  step. No real broker orders were run. Zerodha read-only authentication was
  verified; no authenticated market-data canary or live-paper entry/exit was run.
- Explicit `options_intraday` sessions now ignore saved plans. First eligible
  tick scans fresh completed 5m candles, rescanning every 300s; management stays
  at 60s, including during read-only discovery. Production/combined retain
  their prior plan behavior.
- Default discovery: `scan_basket: nifty50` plus four configured indices (54
  unique requested underlyings); explicit `scan_underlyings` can replace the
  basket. Actual current option listing,
  underlying identity, exact expiry/strike/CE-PE/symbol/lot, liquidity, strategy,
  demand/supply, freshness and risk checks still gate every entry.
- Orders remain BUY CE or BUY PE only. Each submission freshly reconfirms its
  signal and chain; per-underlying exits use fresh bids without old-snapshot
  fallback. Tomorrow's approved remote-paper limits are strict: 0.50%
  initial-stop cash risk per trade, 1.50% realized-plus-unrealized daily loss,
  2 open positions, 5.00% aggregate full-premium exposure, no averaging down,
  and no new entries after the daily-loss boundary. `PAPER_TRADING=true` and
  `remote_paper` are mandatory; configured correlation controls still apply.
- Main implementation: `src/options_live_scan.py`,
  `src/options_intraday_plan.py`, `src/options_intraday_runtime.py`,
  `src/options_intraday.py`, broker/chain adapters, `src/paper_trader.py`,
  `scripts/session.py`, `main.py`. Settings: `config/options_intraday.yaml`.
  Protected `config/config.yaml` was not changed.
- Reproducibility: `.python-version` pins 3.11.17; hashed Linux-amd64
  `requirements-server.lock`, digest-pinned `Dockerfile`, and `make test`
  (`python scripts/test_offline.py` without make). Scheduled paper workflows
  use the pin/lock. Dependencies require network at install time; tests do not.
- Actual checks: full offline suite **671 passed, 2 pre-existing warnings**
  on local Python 3.14.7; compilation, both CLI help commands, workflow YAML
  parsing/pin checks and `git diff --check` passed. Linux-target dependency
  resolution/hash dry-run succeeded; Python 3.11/Docker build/run were **not**
  verified because those tools are absent locally.
- Zerodha smoke succeeded with 10,363 NSE and 38,563 NFO instruments and
  `orders_placed: 0`. A bounded capture added genuine option candles and quote
  snapshots, but the sample had zero executable bid/ask rows and zero exact
  quote/candle timestamp matches. It remains `NOT_READY_FOR_OPTIONS_PNL`.
- New research files: `src/zerodha_live_capture.py` and
   `scripts/zerodha_live_capture.py` capture exact CE/PE contracts, candles and
   provider-timestamped quotes; `src/zerodha_signal_capture.py` and
   `scripts/zerodha_signal_capture.py` capture current-day completed pure-strategy
   signals only when an exact current option and executable quote exist;
   `src/options_premium_backtest.py` supports exact or interval-aligned quotes
   and chronological walk-forward comparison. The new
   `src/research_cepe_strategy.py` is a pure, causal EMA20/EMA50
   trend-pullback-breakout candidate generator that emits BUY CE/BUY PE only;
   it is available through explicit read-only signal capture selection and is
   not in the production strategy list. Focused research tests pass.
- The 2026-10-21 real-data run used 30d NIFTY/BANKNIFTY history and the new
  strategy. It evaluated 157 historical candidates, rejected them rather than
  attaching current quotes, and found no capturable current-day signal; no
  orders were placed. Artifact:
  `reports/zerodha-signals-research-2026-10-21.json`.
- Read-only capture can use an external local `.env` with `--env-file`; it never
  prints credential values. Run during NSE hours with a bounded contract sample
  before attempting any premium backtest.
- User-approved first research protocol: NIFTY + BANKNIFTY, 20 trading sessions;
  existing policy unchanged (nearest weekly, ATM CE/PE, -20% stop, +30% target,
  ask entry/bid exit, ₹20 order cost, configured slippage/taxes); existing ORB,
  pullback and momentum plus the new research-only strategy are evaluated
  separately; demand/supply confirmation is required; conservative shortlist
  gate is at least 30 out-of-sample trades, positive net P&L after costs, PF >=
  1.25, and no single strategy/underlying contributing over 50% of profits.
  Computer may remain awake/connected for capture. Real-money execution remains
  unauthorized by this protocol; tomorrow's test is remote-paper only.
 - Remaining gates: a genuine current-data scan, reconciled remote-paper book,
  and a controlled entry/exit canary. Unknown remote holdings block entries;
  never ignore or close unrelated holdings automatically. Genuine-premium
  research remains `NOT_READY_FOR_OPTIONS_PNL`; no strategy winner is claimed.
- Next publication step, only when requested: fetch/review newer remote state
   commits, preserve session artifacts, then commit/push the intended changes.
   See `README.md` for read-only discovery, setup/test and paper-session commands.
 - 2026-10-21: the repository overlay now defaults to `scan_basket: nifty50`
  (54 requested including configured indices). The fresh Zerodha smoke passed;
  near-ATM NIFTY and BANKNIFTY read-only collectors are running. The first
  market-hours check recorded 40 valid bid/ask rows. At 09:23 IST there was
  only one completed candle per index and no ORB/pullback/momentum candidate.
  No orders were placed.
- Later on 2026-10-21, the live read-only archive reached 160 executable quote
  rows. A 120-second research-only Zerodha clock-skew tolerance produced 6
  exact current-day CE signals. With the approved demand/supply filter enabled,
  all 8 candidates were rejected fail-closed (`no fresh demand zone`/no supply
  reaction), so the approved signal artifact remains `NOT_READY`. An exploratory
  non-demand/supply one-day premium run produced 2 closed trades, net -6105,
  but is explicitly not a strategy conclusion. Full offline suite: 657 passed,
  2 existing warnings. Collectors remain read-only/background.
- The requested option-premium management ladder is now implemented in both
  the research backtest and options runtime: initial -20% stop; executable-bid
  +10% locks +10% and targets +20%; +20% locks +20% and targets +30%; +30%
  closes. Full-position ratcheting is used (no partial exits), skipped rungs
  advance directly to the highest reached level, and candle OHLC cannot trigger
  an exit. Focused backtest/runtime tests pass. This changes exit mechanics,
  not the research signal, and does not prove profitability.
- 2026-10-22 read-only Zerodha analysis at 10:32 IST used the fresh token with
  NIFTY/BANKNIFTY and all four candidate strategies. The raw capture had 12
  current-day exact CE/PE signals with executable bid/ask quotes, but it
  contained conflicting/duplicate directions: early NIFTY PE momentum signals,
  a BANKNIFTY research PE, and later BANKNIFTY/NIFTY CE ORB/momentum signals.
  The approved demand/supply run rejected 7 current-day candidates and
  accepted none, so the safe conclusion was **no trade**, not a forced market
  direction. Artifacts:
  `reports/zerodha-market-analysis-raw-2026-10-22.json` and
  `reports/zerodha-market-analysis-2026-10-22.json`.
- Strict paper-risk wiring is now tested: missing/false risk keys fail closed,
  `PAPER_TRADING=true` is required, same-underlying averaging is blocked,
  pending intents count toward exposure, and the daily-loss boundary is
  inclusive. No broker orders were placed.

---

The older notes below describe the legacy equity/research context, not the new
options-only execution mode.

## Where things stand (2026-10-05, build mode active, 93/93 tests green)

Working folder: `C:\Users\Kapil Kuhire\Downloads\TRIO_IMPLEMENTATION`
Run tests: `cd` there, then `python -m pytest tests -q` (expect 93 passed).

## Strategy (frozen, measured — do not retune without new evidence)
- Daily pullback setup (200/50 SMA + MACD turn), swing-low stop, 1.5R target.
- Shorts ON (`trading.allow_shorts: true`); settlement layer clips/skips SELL
  without holdings (CNC-safe). MIS-style shorting only with real margin.
- ADX regime filter OFF, time stop OFF, no open-window ban — all three were
  measured and rejected (see scripts/sweep_adx.py, sweep_timestop.py,
  entry_timing.py). Evidence tables live in PROJECT_STATE.md + code comments.
- 21-day NIFTY 15m replay: 18 trades, 66.7%, +2,963.61 (17 SELL + 1 BUY).
- 21-day BANKNIFTY 15m replay: 18 trades, 72.2%, +993.27.
- 10-symbol whole-month replay was 90% done when the user stopped it with
  9/10 symbols complete (RELIANCE +1079, TCS −272, INFY −272, HDFCBANK +55,
  ICICIBANK +876, SBIN +1389, ITC −8, LT +1140; AXISBANK was mid-run).
  Rerun any time: `python scripts/day_session.py --symbols <list> --tf 15m --days 21`
  (multi-symbol mode is now ~4x faster: daily-windows only, no debug
  double-pass, 250-bar window cap, per-symbol progress lines).

## Live wiring (working, verified on phone)
- Telegram entry alerts (BUY/SELL + entry/stop/target/qty/rank/setup/why) and
  exit alerts (PASS/FAIL + ₹ P&L + close reason). Markdown escaped
  (`_md_escape`) after a live 400 Bad Request. Test message DELIVERED.
- Session-hours guard: no new entries outside 09:20–15:15 IST
  (`_session_open()`); stops/targets still managed. 4 boundary tests.
- Dashboard: `python scripts/dashboard.py` → http://127.0.0.1:5000
  (+ `/ticket` phone view, `/api/alert` JSON feed).
- Bot token + chat ID are in `.env` (local only, never commit, never paste
  in chat). Bot: @Kapilkuhire_bot.

## Watchlist (20 names, in config `symbols:`)
RELIANCE.NS TCS.NS INFY.NS HDFCBANK.NS ICICIBANK.NS SBIN.NS ITC.NS LT.NS
AXISBANK.NS MARUTI.NS HINDUNILVR.NS SUNPHARMA.NS KOTAKBANK.NS TITAN.NS
ULTRACEMCO.NS ONGC.NS M&M.NS TATASTEEL.NS BAJFINANCE.NS HCLTECH.NS
`python main.py paper` with no `--symbols` now defaults to this list.

## Tomorrow's run (copy-paste)
```
cd "C:\Users\Kapil Kuhire\Downloads\TRIO_IMPLEMENTATION"
python scripts/dashboard.py --timeframe 15m --capital 10000 --interval 300
```
Browser: http://127.0.0.1:5000. Leave till 15:15, then paste the
closed-trades table back here for audit.

## Rules for the next session
- Build mode is ON: edit freely, verify with pytest, keep diffs small.
- Do not retune strategy numbers without a sweep script + evidence table.
- Short side has 21 days of replay evidence, not 5 years — say so honestly.
- Never ask for or paste API keys/tokens in chat. `.env` only.
- After each change: pytest green, update PROJECT_STATE.md + todo.txt.
