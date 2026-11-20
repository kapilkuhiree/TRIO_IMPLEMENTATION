"""Offline tests for the QQQ mean-reversion research backtest."""

from datetime import date, timedelta

import numpy as np
import pandas as pd

from scripts.backtest_qqq_mean_reversion import (
    Trade,
    build_signals,
    ema,
    rolling_vwap,
    rsi_sma,
    rsi_wilder,
    run_backtest,
    summarize,
)


def _bars(closes, opens=None, highs=None, lows=None, volumes=None):
    closes = np.asarray(closes, dtype=float)
    count = len(closes)
    opens = np.asarray(opens if opens is not None else closes, dtype=float)
    highs = np.asarray(highs if highs is not None else np.maximum(opens, closes), dtype=float)
    lows = np.asarray(lows if lows is not None else np.minimum(opens, closes), dtype=float)
    volumes = np.asarray(volumes if volumes is not None else np.full(count, 1_000_000.0),
                         dtype=float)
    index = pd.bdate_range("2020-01-01", periods=count)
    return pd.DataFrame({
        "Open": opens, "High": highs, "Low": lows,
        "Close": closes, "Volume": volumes,
    }, index=index)


def _signals(count, entry_index=None, exit_index=None):
    frame = pd.DataFrame({
        "rsi": 50.0, "ema": 100.0, "ema_rising": True,
        "vwap": 100.0, "value_discount": False,
        "entry": False, "exit": False,
    }, index=pd.bdate_range("2020-01-01", periods=count))
    if entry_index is not None:
        frame.loc[frame.index[entry_index], "entry"] = True
    if exit_index is not None:
        frame.loc[frame.index[exit_index], "exit"] = True
    return frame


def test_rsi_wilder_matches_hand_calculation():
    close = pd.Series([10.0, 11.0, 10.0, 12.0, 11.0, 13.0])
    rsi = rsi_wilder(close, 3)
    assert np.isnan(rsi.iloc[2])
    assert rsi.iloc[3] == 75.0
    assert abs(rsi.iloc[4] - 54.545454545) < 1e-6
    assert rsi.iloc[5] == 75.0


def test_rsi_sma_matches_hand_calculation():
    close = pd.Series([10.0, 11.0, 10.0, 12.0, 11.0, 13.0])
    rsi = rsi_sma(close, 3)
    assert rsi.iloc[3] == 75.0
    assert rsi.iloc[4] == 50.0
    assert rsi.iloc[5] == 80.0


def test_ema_and_rolling_vwap_are_causal():
    bars = _bars([100, 101, 102, 103, 104])
    trend = ema(bars["Close"], 3)
    vwap = rolling_vwap(bars, 3)
    # Changing the future must not change a past value.
    changed = bars.copy()
    changed.loc[changed.index[4], "Close"] = 999.0
    changed.loc[changed.index[4], "Volume"] = 9_999_999.0
    assert ema(changed["Close"], 3).iloc[2] == trend.iloc[2]
    assert rolling_vwap(changed, 3).iloc[2] == vwap.iloc[2]


def test_build_signals_flags_the_full_combo_on_a_pullback():
    # Slow uptrend, then one sharp down day: RSI(3) collapses, price is more
    # than 1% below the 10-day VWAP, and the 100-day EMA is still rising.
    closes = [100.0 + 0.1 * index for index in range(120)]
    closes[-1] = closes[-2] - 5.0
    bars = _bars(closes)
    signals = build_signals(bars)
    row = signals.iloc[-1]
    assert row["rsi"] < 22.0
    assert bool(row["value_discount"]) is True
    assert bool(row["ema_rising"]) is True
    assert bool(row["entry"]) is True


def test_entry_is_next_open_not_the_signal_close():
    bars = _bars([100, 100, 100, 100, 100],
                 opens=[100, 100, 90, 100, 100])
    signals = _signals(5, entry_index=1, exit_index=3)
    trades = run_backtest(bars, signals, exit_timing="next_open")
    assert len(trades) == 1
    trade = trades[0]
    assert trade.signal_date == bars.index[1].date().isoformat()
    assert trade.entry_date == bars.index[2].date().isoformat()
    assert trade.entry_price == 90.0
    assert trade.exit_date == bars.index[4].date().isoformat()
    assert trade.exit_price == 100.0


def test_same_close_exit_uses_the_signal_close():
    bars = _bars([100, 100, 100, 120, 100],
                 opens=[100, 100, 100, 100, 100])
    signals = _signals(5, entry_index=0, exit_index=3)
    trade = run_backtest(bars, signals, exit_timing="same_close")[0]
    assert trade.exit_date == bars.index[3].date().isoformat()
    assert trade.exit_price == 120.0


def test_signal_on_the_last_bar_never_enters():
    bars = _bars([100, 100, 100, 100, 100])
    signals = _signals(5, entry_index=4)
    assert run_backtest(bars, signals) == []


def test_fixed_notional_pnl_is_one_hundred_thousand_times_return():
    bars = _bars([100, 100, 100, 100, 100],
                 opens=[100, 100, 100, 100, 110])
    signals = _signals(5, entry_index=0, exit_index=2)
    trade = run_backtest(bars, signals, exit_timing="next_open")[0]
    # Entry at open[1]=100, exit at open[3]=100 -> flat.
    assert trade.pnl == 0.0

    bars = _bars([100, 100, 100, 100, 100],
                 opens=[100, 100, 100, 110, 100])
    signals = _signals(5, entry_index=0, exit_index=2)
    trade = run_backtest(bars, signals, exit_timing="next_open")[0]
    # Entry at open[1]=100, exit at open[3]=110 -> +10% on $100k.
    assert trade.pnl == 10_000.0
    assert abs(trade.return_pct - 10.0) < 1e-9


def test_costs_are_applied_per_side():
    bars = _bars([100, 100, 100, 100, 100],
                 opens=[100, 100, 100, 110, 100])
    signals = _signals(5, entry_index=0, exit_index=2)
    trade = run_backtest(bars, signals, exit_timing="next_open", cost_bps=10.0)[0]
    # 10 bps per side: buy at 100.10, sell at 109.89 -> ~9.78%.
    assert trade.pnl < 10_000.0
    assert trade.pnl > 9_700.0


def test_max_drawdown_uses_the_running_equity_peak():
    base = dict(signal_date="2020-01-01", entry_date="2020-01-02",
                exit_date="2020-01-03", entry_price=100.0, exit_price=100.0,
                bars_held=1, exit_reason="rsi_exit", entry_rsi=10.0,
                entry_vwap_discount_pct=-2.0)
    trades = [
        Trade(**{**base, "return_pct": 10.0, "pnl": 10_000.0}),
        Trade(**{**base, "return_pct": -20.0, "pnl": -20_000.0}),
        Trade(**{**base, "return_pct": 5.0, "pnl": 5_000.0}),
    ]
    stats = summarize(trades, capital=100_000.0)
    assert stats["net_pnl"] == -5_000.0
    assert stats["max_drawdown"] == -20_000.0
    assert stats["max_drawdown_pct_of_capital"] == 20.0
    assert abs(stats["max_drawdown_pct_of_peak"] - 18.1818) < 1e-3


def test_first_trade_loss_is_measured_from_the_starting_capital():
    base = dict(signal_date="2020-01-01", entry_date="2020-01-02",
                exit_date="2020-01-03", entry_price=100.0, exit_price=100.0,
                bars_held=1, exit_reason="rsi_exit", entry_rsi=10.0,
                entry_vwap_discount_pct=-2.0)
    trades = [
        Trade(**{**base, "return_pct": -5.0, "pnl": -5_000.0}),
        Trade(**{**base, "return_pct": 2.0, "pnl": 2_000.0}),
    ]
    stats = summarize(trades, capital=100_000.0)
    assert stats["max_drawdown"] == -5_000.0
    assert stats["max_drawdown_pct_of_capital"] == 5.0
