# TRIO — Trading Intelligence and Optimization
**Author:** Kapil Kuhire <kapilkuhire89@gmail.com>
![Author](https://img.shields.io/badge/author-Kapil%20Kuhire-2ea44f)

> **DISCLAIMER:** This software is for **educational purposes only**. It does
> not constitute financial advice. Trading involves substantial risk of loss.
> The authors are not responsible for any financial losses incurred from using
> this software. Always consult a qualified financial advisor before making
> investment decisions.

## What is TRIO?

A screenshot-driven trading assistant that:

1. Accepts a market screenshot (chart, terminal, etc.)
2. Extracts context using a vision model (symbol, price, trend, patterns)
3. Fetches live market data and computes technical indicators
4. Cross-checks screenshot values against real data
5. Scores news and social sentiment
6. Generates a **BUY / SELL / HOLD** signal with entry, stop-loss, target,
   position size, confidence, and reasoning
7. Manages risk with ladder targets T1/T2/T3, daily loss + mark-to-market,
   max positions, sector exposure, consecutive losses and drawdown halts,
   and a trailing stop after T2
8. Supports **local paper (default)**, **MegaBull remote paper** (MIS), and
   **NIFTY index options** paper spreads (lot 65, bidask fill + dated costs)

## Project Root

Run commands from this repository's root. Research results are not executable
session instructions; do not merge a separate research fork wholesale.

## Setup

The server/paper-session build pins **Python 3.11.17** in `.python-version` and
82 exact, hashed dependencies in `requirements-server.lock` (Linux amd64).
The broader `requirements.txt` remains an optional desktop/research stack.

```bash
# Recommended on Linux or Windows with Docker; installs system packages too.
docker build --platform linux/amd64 -t trio .
docker run --rm --network none trio                 # CLI help only; no orders
docker run --rm --network none --entrypoint make trio test

# Native Linux alternative, with Python 3.11.17 and build prerequisites present:
python -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements-server.lock
cp .env.example .env                                # private; never commit
make test                                          # offline after installation
```

Without `make`, including on Windows, the same test entry point is
`python scripts/test_offline.py`. It blocks external requests before collection,
disables local dotenv credentials, forces test ledgers, and isolates default
plan/chain diagnostic files. Scoped pytest paths may follow the command.
Docker/CPython 3.11 execution still needs verification on a machine providing
those tools; the local integration suite was run on Python 3.14.7.

## CLI Commands

```bash
# Screenshot analysis
python main.py analyze --image path/to/screenshot.png
python main.py analyze --image path/to/screenshot.png --symbol RELIANCE.NS --timeframe 15m

# Scan one or more symbols (no screenshot)
python main.py scan --symbol RELIANCE.NS --timeframe 15m

# Backtest (history-realistic: next-bar Open fills, gap-aware stops, notional costs)
python main.py backtest --symbol RELIANCE.NS --period 365d --timeframe 1d

# Paper trading loop (dual-clock: fast guard + slow entry pass)
python main.py paper --symbols RELIANCE.NS TCS.NS --interval 300
python main.py paper --timeframe 1d --max-iterations 2   # smoke

# Scheduled jobs (GitHub Actions; see .github/workflows/)
python scripts/premarket.py --universe nifty100 --timeframe 1d --top-n 3
python scripts/session.py   --date 2026-10-10 --interval 60 --max-seconds 21600
python scripts/session.py   --date 2026-10-10 --allow-stale-plan   # stale guard
python scripts/digest.py    --date 2026-10-10 --no-alert

# Tests & compile
python scripts/test_offline.py
python -m pytest tests/test_guard.py tests/test_session_hours.py tests/test_eod_squareoff.py tests/test_risk_manager.py tests/test_paper_broker.py tests/test_megabull.py tests/test_options.py tests/test_backtester.py tests/test_trade_log.py -q
python -m compileall -q src scripts main.py
```

## Broker Defaults

* **Local paper** is the default in tests (`TRIO_BROKER_PROVIDER=paper` via `tests/conftest.py`).
* The installed `config/config.yaml` ships `broker.provider: megabull` — the free remote
  paper API (MIS, Rs 5 lakh virtual, `MEGABULL_API_KEY` in `.env`).
* Any typo provider name now raises at startup (no silent fallback to paper).
* MegaBull without a key raises; `mode: live` requires `TRIO_LIVE=1` or
  `forward_test.live_confirmed: true`.

## Paper vs Remote Paper vs Options

* **Equity:** local `PaperBroker` or `MegaBullBroker` (refresh_positions, PENDING≠FILLED, idempotent).
* **Legacy options path (production/combined):** NIFTY index, weekly tactical
  expiry, defined-risk debit spreads.
  Paper fills via `OptionsPaperBroker` (chain-mid / LTP / BS sim + bidask side);
  remote legs via `MegaBullOptionsBroker` (remote spread id per session,
  max 4/session). Backtest and paper broker share `round_trip_cost`.

## Fresh options-only scanning

Explicit `options_intraday` sessions do **not** load, save, mark executed, or
fall back to a premarket/previous-day plan. The first entry-eligible tick scans
fresh 5m underlying candles; scans repeat every 300 seconds. Exits/reconciliation
run every 60 seconds, including while read-only discovery is slow or fails.
All order submission and management remain serialized on the session thread.

`config/options_intraday.yaml` controls discovery without changing production
`config/config.yaml`:

```yaml
options_intraday:
  scan_basket: nifty50        # nifty100 also supported
  scan_interval_seconds: 300
  # Optional explicit replacement, instead of the basket/index union:
  # scan_underlyings: [NIFTY, BANKNIFTY, RELIANCE, TCS]
  stop_premium_fraction: 0.20
  profit_lock_step_fraction: 0.10
  target_premium_fraction: 0.30
  risk_per_trade_pct: 0.50
  max_daily_loss_pct: 1.50
  max_open_positions: 2
  max_premium_exposure_pct: 5.00
  allow_averaging_down: false
  PAPER_TRADING: true
```

- The repository overlay currently resolves **NIFTY50 stocks + 4 configured
  indices = 54 unique underlyings**. Set `scan_basket: nifty100` to widen it;
  this is a requested basket count, not a count of eligible contracts.
- Only current, unambiguous NSE option listings proceed. Non-F&O stocks,
  missing/expired contracts, stale/incomplete/gapped bars and provider failures
  are rejected with diagnostic counts. A stock signal is never mapped to NIFTY.
- ORB/pullback/momentum and configured demand/supply confirmation remain in use.
  Chain requests bypass caches and occur only for listed underlyings with a
  completed-bar signal. Candidates are ranked globally, without an equity top-3 cap.
- Each entry gets a fresh same-underlying signal/chain confirmation and exact
  broker expiry/strike/type/symbol/lot validation. Bullish signals submit **BUY CE**;
  bearish signals submit **BUY PE**, never equity, futures or short-option orders.
- Tomorrow's approved remote-paper limits are strict: 0.50% cash risk per trade
  at the initial premium stop, 1.50% realized-plus-unrealized daily loss, 2
  open positions, 5.00% aggregate full-premium exposure, no averaging down,
  and no new entries after the daily-loss boundary. `PAPER_TRADING=true` and
  `remote_paper` are mandatory. Five daily entries, configured correlation
  limits, 14:30 entry cutoff and 15:10 hard-flat (IST) remain active. Wider
  discovery does not guarantee or force any entries.
- Option-premium exits use the staged bid ladder: initial stop at **−20%**;
  an executable bid at **+10%** moves the stop to +10% and the next target to
  +20%; +20% moves the stop to +20% and the next target to +30%; +30% closes
  the full remaining position. A price jump skips directly to the highest
  reached rung. The +10% and +20% levels do not partially close the position.
  Missing/stale bids do not trigger a level, and exits use the fresh executable
  bid rather than option candle high/low.
- Management fetches each owned underlying's own fresh bid. There is no saved
  discovery-snapshot fallback in options-only mode. Private/persistent intents
  prevent duplicate or uncertain submissions across scans/restarts.

Read-only discovery (network/data credentials needed, **no orders**):

```bash
python -c "from src.utils import load_env, load_config; from src.options_live_scan import build_live_options_scan; load_env(); d=build_live_options_scan(load_config())['diagnostics']; print({k:d[k] for k in ('requested','data_ok','no_signal','not_listed','contract_rejection','fetch_errors','selected')})"
```

Only after verifying remote-paper credentials and reconciling existing holdings,
start a paper session. This command **can place remote-paper option orders**:

```bash
docker run --rm --env-file .env \
  -e TRIO_TRADING_MODE=options_intraday -e TRIO_BROKER_PROVIDER=megabull \
  -e PAPER_TRADING=true \
  -e TRIO_REMOTE_REQUIRED=1 -e TRIO_ALLOW_LOCAL_OPTION_SHADOW=0 \
  -e TRIO_DEMAND_SUPPLY=1 \
  --mount type=volume,src=trio-options-state,dst=/app/runtime \
  --entrypoint python trio scripts/session.py \
  --trading-mode options_intraday --interval 60 --max-seconds 21300
```

Add `--no-plan` for **management only** (the name is legacy; the new mode never
reads a plan). A historical/future `--date` is also management-only; stale-plan
flags cannot enable stale options. The scheduled workflow already selects this
mode; premarket previews are optional research artifacts, not prerequisites.
No live canary or genuine-premium profitability validation is claimed.

## Zerodha research capture and genuine premium backtest

The research path is read-only and separate from the remote-paper order path.
It records exact NFO CE/PE contracts, real option candles, provider quote
timestamps, local observation times and depth. It never imports an order
adapter and every report includes `orders_placed: 0`.

If the credentials are in a different local checkout, pass that `.env` path;
the value is loaded locally and never printed:

```powershell
python scripts/zerodha_live_capture.py `
  --env-file "C:\Users\YOUR_USERNAME\Downloads\TRIO_IMPLEMENTATION\.env" `
  --underlyings NIFTY BANKNIFTY `
  --period 1d `
  --max-contracts 20 `
  --quote-batch-size 5 `
  --rate-limit-seconds 1 `
  --cycles 72 `
  --interval-seconds 300
```

Run this during NSE hours if you want approximately one quote snapshot per
5-minute bar. Use `--once` for a bounded smoke capture. It is intentionally a
sample unless the contract selection is expanded; `--max-contracts` is reported
as partial coverage. Do not run it with `--network none`; it requires the
read-only Zerodha API.

Capture current-day completed strategy signals separately:

```powershell
python scripts/zerodha_signal_capture.py `
  --env-file "C:\Users\YOUR_USERNAME\Downloads\TRIO_IMPLEMENTATION\.env" `
  --underlyings NIFTY BANKNIFTY `
  --strategies orb pullback momentum `
  --period 5d `
  --max-provider-clock-skew-seconds 120 `
  --require-demand-supply `
  --output reports\zerodha-signals.json `
  --dry-run
```

The new `trend_pullback_breakout_research` strategy is available only when
explicitly selected through this read-only capture command; it is not part of
the production `options_intraday` strategy list and never places orders:

```powershell
python scripts/zerodha_signal_capture.py `
  --env-file "C:\Users\YOUR_USERNAME\Downloads\TRIO_IMPLEMENTATION\.env" `
  --underlyings NIFTY BANKNIFTY `
  --strategies trend_pullback_breakout_research `
  --period 30d `
  --max-provider-clock-skew-seconds 120 `
  --require-demand-supply `
  --output reports\zerodha-signals-research.json `
  --dry-run
```

It is a causal EMA20/EMA50 trend, EMA20 pullback and completed-candle
breakout candidate definition. Bullish candidates map to `BUY CE`; bearish
candidates map to `BUY PE`. It selects no option contract and estimates no
premium. It is a research candidate, not a profitability claim or a live
strategy; the 20-session out-of-sample gate must pass before any promotion.

Older signals are rejected rather than paired with current quotes. A
`READY` signal artifact still needs matching archived option candles and
historical bid/ask rows before P&L is calculated.

When those inputs exist, run the genuine premium backtest with interval-aware
provider timestamps:

```powershell
python scripts/options_premium_backtest.py `
  --db "C:\Users\YOUR_USERNAME\AppData\Local\TRIO\options\options_archive.sqlite3" `
  --signals reports\zerodha-signals.json `
  --underlyings NIFTY BANKNIFTY `
  --start 2026-10-01 `
  --end 2026-10-31 `
  --quote-policy interval_aligned
```

`interval_aligned` uses the actual provider quote timestamp only when it falls
inside the option candle's interval and the quote was observed before that
candle completed. Exact/ambiguous/missing quotes are rejected; no current
quote, midpoint, Black-Scholes value or underlying substitute is used. The
backtest remains `NOT_READY_FOR_OPTIONS_PNL` until real aligned data and exact
signal identities are present.

The signal-capture clock-skew tolerance is research-only and bounded at 120
seconds for the observed Zerodha timestamp offset. It does not relax the
historical backtest timestamp rules or live order validation. Demand/supply is
fail-closed when requested: a missing fresh causal zone rejects the signal.

Walk-forward comparison is chronological: candidate selection uses validation
data, with training as a tie-breaker; the test period is never used for tuning.
No parameter is promoted because it merely produces a high in-sample score.

## Plan & Session

The following describes the unchanged production/combined path, **not** the
plan-free options-only session above.

* Premarket writes the next valid NSE session plan to
  `output/plans/YYYY-MM-DD.json` (scan stats `requested/failed/scanned`).
  Equity execution stays capped at `max_positions_to_open`; options inspect
  the wider `options_max_candidates` ranked pool independently.
* Session reads today's plan; without `--allow-stale-plan` stale fallbacks are rejected
  (explicit `validate_stale`: age, price deviation, stop/qty checks).
* Live re-scans at `09:40/11:00/13:00 IST` augment the plan; good-morning and routing
  alerts go to Telegram; EOD digest via `scripts/digest.py`.
* Stale/rejected/pending orders never consume risk and never count as fills.
  Remote refresh failures keep the broker uncertain and block new entries.
  Confirmed risk snapshots and option idempotency keys persist under
  `output/equity_snapshot.json` and `output/runtime/`.

## Risk Limits

* Risk per trade `1.5%`, capital `500000`, `max_daily_loss_pct 5.0` (realized +
  marked unrealized), `max_open_positions 5`, per-symbol exposure `20%`,
  per-sector `40%` (`config/sectors.yaml`), consecutive losses + drawdown halts.
* Ladder `T1 0.8R (50%→breakeven)`, `T2 1.5R (30%→trail)`, `T3` runner; `enabled_phase2: false`
  (shadow-logged until the sweep validates).
* Restart: `risk_manager.rebuild_from_ledger(positions, closed_trades)` restores
  exposure from broker state + the JSONL ledger.

## Market Hours

`config/market.yaml` (authoritative, merged into `load_config()["market"]`):

```
open 09:15 | entry_start 09:20 | entry_cutoff 15:00 (exclusive)
hard_flat 15:10 | close 15:30 | tz Asia/Kolkata | holidays: []
```

* `<09:20` no new entries. `09:20–15:00` entries allowed. `15:00–15:10` exits only.
* `>=15:10` force hard-flat. Invalid config or wrong timezone → management-only.

## Environment Variables

| Key | Purpose | Required? |
|-----|---------|-----------|
| `OPENAI_API_KEY` / `GOOGLE_GEMINI_API_KEY` | Screenshot analysis | Yes (one) |
| `NEWSAPI_KEY` | News headlines for sentiment | Recommended |
| `FINNHUB_API_KEY` | Financial news (alternative) | Optional |
| `REDDIT_CLIENT_ID/_SECRET/_USER_AGENT` | Reddit sentiment | Optional |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` / `output/subscribers.json` | Alerts | Optional |
| `MEGABULL_API_KEY` | Remote paper broker (`broker.provider: megabull`) | For remote |
| `TRIO_BROKER_PROVIDER` | `paper` (tests) or `megabull` | Overrides config |
| `TRIO_TEST_MODE` / `TRIO_LIVE` | Test sidecar / live confirm | As needed |
| `ZERODHA_API_KEY/_SECRET/_ACCESS_TOKEN` | Read-only Zerodha market data/archive | Required for archive |
| `TRIO_OPTIONS_ARCHIVE_PATH` | Optional SQLite options archive path | Optional |

## Project Structure

```
TRIO_IMPLEMENTATION/
├── config/
│   ├── config.yaml            # all tunables (screener, risk, options, alerts)
│   ├── market.yaml            # authoritative market clock (open/entry/hard-flat/close)
│   └── sectors.yaml           # symbol→sector (NIFTY 50)
├── src/
│   ├── screenshot_analyzer.py
│   ├── data_fetcher.py
│   ├── indicators.py
│   ├── cross_checker.py
│   ├── sentiment.py
│   ├── signal_engine.py
│   ├── risk_manager.py
│   ├── sector_map.py
│   ├── market_clock.py
│   ├── screener.py            # batch yfinance + IV-rank gate
│   ├── backtester.py          # next-bar Open, gap-aware, notional costs, Sharpe by bars/yr
│   ├── paper_trader.py        # scan_once + guard/trail + EOD + risk wiring
│   ├── plan.py                # today + find_latest_plan + validate_stale
│   ├── broker/
│   │   ├── base.py
│   │   ├── paper.py
│   │   ├── megabull.py
│   │   ├── megabull_options.py
│   │   └── options_paper.py
│   ├── options_pricing.py     # BS + round_trip_cost
│   ├── options_chain.py
│   ├── options_selector.py
│   ├── alerts.py
│   └── utils.py
├── scripts/
│   ├── premarket.py
│   ├── session.py
│   ├── day_session.py
│   ├── digest.py
│   ├── options_backtest.py
│   └── (walkforward, sweeps, validation)
├── .github/workflows/         # session.yml (09:10) + premarket.yml (15:30)
├── tests/                     # see Phase 11 verification
├── main.py
├── requirements.txt
└── .env.example
```

## Known Limitations

* Zerodha is read-only for market-data research. It is not enabled as a
  `broker.provider` and the archive cannot discover expired option tokens that
  were not captured while active. Run `python scripts/zerodha_options_archive.py`
  with the daily access token to build the local incremental archive.
* T2/T3 ladder legs are shadow-logged (`ladder.enabled_phase2: false`) — EOD hard-flat
  still settles everything at `15:10`.
* Sector map covers NIFTY 50; unknown symbols are `Unknown`.

## License

MIT
