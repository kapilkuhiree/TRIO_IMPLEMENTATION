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
7. Manages risk with daily loss limits, position limits, and trailing stops
8. Supports paper trading (default) and can plug into any broker API

## Setup

```bash
# 1. Clone / copy the project
cd TRIO

# 2. Create a virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux/Mac

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment variables
copy .env.example .env       # then edit .env with your API keys

# 5. Review config
# Edit config/config.yaml to set symbols, timeframes, risk params, etc.
```

## Usage

### Analyze a screenshot
```bash
python main.py analyze --image path/to/screenshot.png
```

### Analyze a symbol directly (no screenshot)
```bash
python main.py scan --symbol RELIANCE.NS --timeframe 15m
```

### Run backtest
```bash
python main.py backtest --symbol RELIANCE.NS --start 2026-01-01 --end 2026-10-01
```

### Paper trading mode
```bash
python main.py paper --symbols RELIANCE.NS TCS.NS --interval 300
```

## Project Structure

```
TRIO/
├── config/config.yaml         # All tunable parameters
├── src/
│   ├── screenshot_analyzer.py # Vision model chart extraction
│   ├── data_fetcher.py        # OHLCV data from yfinance/APIs
│   ├── indicators.py          # Technical indicator calculations
│   ├── cross_checker.py       # Verify screenshot vs live data
│   ├── sentiment.py           # News + social sentiment scoring
│   ├── signal_engine.py       # Composite signal generation
│   ├── risk_manager.py        # Position sizing, stops, limits
│   ├── backtester.py          # Historical strategy testing
│   ├── paper_trader.py        # Paper trading loop
│   ├── broker/
│   │   ├── base.py            # Abstract broker interface
│   │   ├── paper.py           # Paper trading adapter
│   │   └── zerodha.py         # Zerodha Kite adapter (stub)
│   ├── alerts.py              # Telegram / email notifications
│   └── utils.py               # Retry logic, logging, helpers
├── tests/                     # Unit tests
├── main.py                    # CLI entry point
├── requirements.txt
├── .env.example
└── README.md
```

## Configuration

All parameters are in `config/config.yaml`. Key sections:

- **symbols**: List of tickers to track
- **indicators**: Periods and thresholds for every technical indicator
- **signal_engine.weights**: How much weight technical, sentiment, and chart
  analysis get in the composite score
- **risk_management**: Capital, risk per trade, stop-loss method, max daily
  loss, max positions

## API Keys Required

| Key | Purpose | Required? |
|-----|---------|-----------|
| `OPENAI_API_KEY` | Screenshot analysis via GPT-4V | Yes (or Gemini) |
| `NEWSAPI_KEY` | News headlines for sentiment | Recommended |
| `FINNHUB_API_KEY` | Financial news (alternative) | Optional |
| `TELEGRAM_BOT_TOKEN` | Alert notifications | Optional |

## License

MIT
