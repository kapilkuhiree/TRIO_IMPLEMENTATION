"""
TRIO — Backtester Tests (synthetic data)
"""

import sys
from pathlib import Path
from unittest.mock import patch
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtester import run_backtest, BacktestResult


def _make_df(n=250):
    np.random.seed(42)
    base = 100.0
    prices = base + np.cumsum(np.random.randn(n) * 0.3)
    df = pd.DataFrame({
        "Open": prices - 0.1,
        "High": prices + 0.2,
        "Low": prices - 0.2,
        "Close": prices,
        "Volume": np.linspace(100000, 200000, n),
    })
    df.index = pd.date_range("2024-01-01", periods=n, freq="1D")
    return df


def test_backtest_runs_on_synthetic():
    with patch("src.backtester.fetch_market_data") as mock_fetch:
        df = _make_df(220)
        mock_fetch.return_value.ohlcv = df
        mock_fetch.return_value.latest_price = float(df["Close"].iloc[-1])
        res = run_backtest(symbol="TEST", timeframe="1d", period="1y")
        assert isinstance(res, BacktestResult)
        assert hasattr(res, "total_trades")
        assert hasattr(res, "win_rate")
        assert hasattr(res, "profit_factor")
        assert hasattr(res, "max_drawdown_pct")
        assert hasattr(res, "sharpe_ratio")