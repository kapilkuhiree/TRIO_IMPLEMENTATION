# TRIO — API Keys & Setup Guide

**Goal:** run TRIO with minimal (or zero paid) API keys. Default flows use free/public sources.

## Quick summary
- `scan` and `backtest`: work with **yfinance** (free, no key). 
- `analyze` (screenshot): needs a vision model key (**OpenAI** or **Google Gemini**). 
- `sentiment` (news): needs free keys if you want live headlines (**NewsAPI** or **Finnhub**). VADER runs offline (no key).
- Alerts: optional (**Telegram**, **Email**). 
- Broker: **Zerodha** only when `live_trading=True` (not recommended for now).

## Required vs Optional
| Feature | Key | Free? | Notes |
|---|---|---|---|
| Scan (symbol -> signal) | none | — | Uses yfinance |
| Backtest (history) | none | — | Uses yfinance |
| Paper trading | none | — | Simulated |
| Screenshot analyze (vision) | OPENAI_API_KEY or GOOGLE_GEMINI_API_KEY | mostly | OpenAI has credits; Gemini has free tier |
| News sentiment (NewsAPI) | NEWSAPI_KEY | yes (free tier) | newsapi.org — 100 req/day on free plan in many cases |
| News sentiment (Finnhub) | FINNHUB_API_KEY | yes (free tier) | finnhub.io — generous free tier for many use cases |
| Reddit (optional) | REDDIT_CLIENT_ID/SECRET | yes | only if enabled in config |
| Telegram alerts | TELEGRAM_BOT_TOKEN + CHAT_ID | yes | free |
| Email alerts | EMAIL_* | yes | use app password if Gmail |
| Zerodha live | ZERODHA_* | broker specific | leave disabled |

## Where to get free keys
1. **NewsAPI (free)**: https://newsapi.org/register — get key, add to `.env` as `NEWSAPI_KEY`
2. **Finnhub (free)**: https://finnhub.io/register — get key, add `FINNHUB_API_KEY`
3. **Google Gemini (free tier)**: https://aistudio.google.com/apikey — add `GOOGLE_GEMINI_API_KEY`
4. **OpenAI**: https://platform.openai.com/api-keys — may need credits
5. **Telegram**: @BotFather -> create bot, get token; get chat id (e.g. @userinfobot)

## Setup
```bash
cd "C:\Users\Kapil Kuhire\Downloads\TRIO"
copy .env.example .env  # Windows
# edit .env with your keys
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

## Run
```bash
# no keys needed
python main.py scan --symbol RELIANCE.NS --timeframe 15m
python main.py backtest --symbol RELIANCE.NS --timeframe 1d --period 365d
python main.py paper --symbols RELIANCE.NS TCS.NS --interval 300 --max-iterations 2

# needs vision key
python main.py analyze --image path/to/screenshot.png

# sentiment needs news key
# enable in config: sentiment.enabled=true and sources.newsapi.enabled=true or finnhub
```

## Sentiment config snippet
Edit `config/config.yaml`:
```yaml
sentiment:
  enabled: true
  model: "vader"  # works offline; "finbert" needs torch/transformers
  sources:
    newsapi: { enabled: true, max_articles: 10, max_age_hours: 24 }
    finnhub: { enabled: false }
  cache_ttl_minutes: 15
```

**Note:** FinBERT pulls heavy deps. Use VADER unless you explicitly need it.

## MegaBull paper broker (DEFAULT since 2026-10-06)
Free paper-trading REST API (docs: https://megabull.in/paper-trading-api-india.html,
base `https://api.megabull.in`, header `api-key`). All fills are MIS intraday;
config `broker.provider: "megabull"` (flip with `TRIO_BROKER_PROVIDER=paper`).
- Key lives ONLY in `.env` as `MEGABULL_API_KEY`; regenerate monthly from
  trade.megabull.in Profile (free Rs5L virtual; PRO tier expiry 2026-10-13).
- 2026-10-06 note: the integrated account belongs to Shreyash Laddha
  (shared key; key appeared in chat) — regenerate it before relying on it.
- `risk_management.capital` MUST equal the MegaBull virtual balance (500000)
  so sizing math matches the account; `risk_per_trade_pct: 1.5`.
- Order body: instrumentToken (strip `.NS`, cached CSV) + qty + BUY/SELL +
  MIS + LIMIT/MKT/SL. No brackets — stop/target exits are our loop's job.
- Pre smoke checklist (market hours): 1-qty MIS order -> confirm in
  `/api/order/my` + `/api/position/my` -> close -> remote position zero +
  Telegram exit alert -> reconcile `/api/report/monthly/day/<date>` with log. 

## Backtest fully & improve success rate
We have a working backtester (synthetic-tested). To improve realistically:

- Test on multiple symbols/timeframes (not just 1). 
- Tune weights in `signal_engine.weights` (technical/sentiment/chart). 
- Raise `min_confirmations` or require stronger confluence. 
- Use ATR stops consistently; enforce 1:2 R:R (already there). 
- Consider regime filter (trend strength) to avoid choppy markets.
- Walk-forward: test on out-of-sample periods.
- Multi-timeframe: add 1h confirmation for 15m signals.
- Add simple exit rules (opposing signal exit) — can extend backtester.
- Keep commission/slippage realistic in `backtesting` section.

Start with small changes and re-run tests + backtests.

**Disclaimer:** Educational only. Not financial advice.
