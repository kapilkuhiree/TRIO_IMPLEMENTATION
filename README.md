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

```
C:\Users\Kapil Kuhire\Downloads\TRIO_IMPLEMENTATION   (this checkout — the production repo)
  docs: C:\Users\Kapil Kuhire\Downloads\Claud_TRIO_v2  (research fork — do not merge wholesale)
```

## Setup

```bash
cd "C:\Users\Kapil Kuhire\Downloads\TRIO_IMPLEMENTATION"

python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux/Mac

pip install -r requirements.txt
copy .env.example .env       # then edit .env with your keys
# edit config/config.yaml and config/market.yaml
```

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
python -m pytest -q --basetemp C:\Users\KAPILK~1\AppData\Local\Temp\trio-pytest
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
* **Options:** NIFTY index only, weekly tactical expiry, defined-risk debit spreads.
  Paper fills via `OptionsPaperBroker` (chain-mid / LTP / BS sim + bidask side);
  remote legs via `MegaBullOptionsBroker` (remote spread id per session,
  max 4/session). Backtest and paper broker share `round_trip_cost`.

## Plan & Session

* Premarket writes `output/plans/YYYY-MM-DD.json` (scan stats `requested/failed/scanned`).
* Session reads today's plan; without `--allow-stale-plan` stale fallbacks are rejected
  (explicit `validate_stale`: age, price deviation, stop/qty checks).
* Live re-scans at `09:40/11:00/13:00 IST` augment the plan; good-morning and routing
  alerts go to Telegram; EOD digest via `scripts/digest.py`.
* Stale/rejected/pending orders never consume risk and never count as fills.

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
| `ZERODHA_API_KEY/_SECRET/_ACCESS_TOKEN` | Zerodha adapter (stub — not wired) | Not active |

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

* Zerodha adapter is a stub — not enabled as a `broker.provider`.
* T2/T3 ladder legs are shadow-logged (`ladder.enabled_phase2: false`) — EOD hard-flat
  still settles everything at `15:10`.
* Sector map covers NIFTY 50; unknown symbols are `Unknown`.

## License

MIT
