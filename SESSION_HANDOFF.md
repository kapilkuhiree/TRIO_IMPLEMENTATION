# TRIO — Session Handoff (paste into a new chat)

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
