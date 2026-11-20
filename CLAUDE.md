# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Testing
- Run all offline/unit tests: `python scripts/test_offline.py`
- Run a specific test suite:
  ```bash
  python -m pytest tests/test_guard.py tests/test_session_hours.py tests/test_risk_manager.py -q
  ```

### Analysis & Trading
- Analyze a chart screenshot:
  ```bash
  python main.py analyze --image path/to/screenshot.png --symbol <SYMBOL> --timeframe <TIME>
  ```
- Backtest a strategy:
  ```bash
  python main.py backtest --symbol <SYMBOL> --period <PERIOD> --timeframe <TIME>
  ```
- Run paper trading (local default):
  ```bash
  python main.py paper --symbols <SYM1> <SYM2> --interval <INTERVAL_SEC>
  ```

### Options-Only Trading (Options Intraday Mode)
This mode does NOT read plans and scans fresh 5m candles every 300s.
- Diagnostic check (read-only):
  ```bash
  python -c "from src.utils import load_env, load_config; from src.options_live_scan import build_live_options_scan; load_env(); d=build_live_options_scan(load_config())['diagnostics']; print({k:d[k] for k in ('requested','data_ok','no_signal','not_listed','contract_rejection','fetch_errors','selected')})"
  ```
- Run options session (places remote-paper orders):
  ```bash
  python scripts/session.py --trading-mode options_intraday --interval 60 --max-seconds 21300
  ```

## Architecture

- **Core Logic (`src/`)**: Contains the signal engine, risk management, and broker adapters.
- **Strategies (`strategies/` and `src/strategies/`)**: Defines trading algorithms (ORB, SMC, etc.).
- **Broker Interface (`src/broker/`)**: Abstracts execution (Paper, MegaBull, Zerodha). Note that `zerodha.py` is for read-only research capture.
- **Config (`config/`)**:
  - `config.yaml`: Main settings, risk parameters, and strategy selection.
  - `market.yaml`: Defines market hours (Open, Entry Cutoff, Hard Flat).
  - `options_intraday.yaml`: Specific to the options-only scan mode.
- **Research (`output/` and `scripts/`)**: Holds backtest results, captured market data, and simulation scripts. Research results are NOT executable instructions; do not merge these into core logic.

## Guidelines
- **Root execution**: Always run commands from the repository root.
- **Environment**: Use Python 3.11.17 where possible; verify Python 3.14.7 compatibility for local integration testing.
- **Options Mode**: When working on options-only features, note that `options_intraday` bypasses the standard plan/chain cache and relies on fresh signals.
- **Disclaimer**: Code is for educational purposes. Trading involves risk.
