"""
TRIO — Backtester realism tests (Phase 8)
Author: Kapil Kuhire <kapilkuhire89@gmail.com>
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import numpy as np
from src.backtester import run_backtest, BacktestResult, _compute_metrics
from src.risk_manager import calculate_stop_loss


def _df_from_rows(rows):
    df = pd.DataFrame(rows)
    df.index = pd.date_range("2024-01-01", periods=len(rows), freq="1D")
    return df


def _bias_bullish(monkeypatch_price=100.0):
    import src.backtester as bt
    import src.signal_engine as se
    # Make indicators deterministic and not raise
    from src.indicators import IndicatorReading, TechnicalReadings
    fake = TechnicalReadings(symbol="T", timeframe="1d",
                             indicators={"atr_14": IndicatorReading("atr_14", 2.0)},
                             swing_low=98.0, swing_high=102.0)
    # monkeypatch compute_indicators to avoid real data needs
    orig = bt.compute_indicators
    monkeypatch_price_val = monkeypatch_price
    def _fake(df, sym, tf):
        return fake
    return patch("src.backtester.compute_indicators", side_effect=_fake)


def test_entry_at_next_bar_open():
    """Signal at bar i -> entry at bar i+1 Open (never same-bar close)."""
    from src.signal_engine import TradeSignal
    n = 210
    df = _df_from_rows([{"Open": 100.0, "High": 101.0, "Low": 99.0,
                         "Close": 100.0, "Volume": 1000}] * n)
    calls = []
    orig_gs = __import__("src.signal_engine", fromlist=["generate_signal"]).generate_signal
    def _gs(**kw):
        calls.append(kw.get("latest_price"))
        # Fire BUY once at bar 205 close
        if len(calls) == 6:
            return TradeSignal(symbol="T", action="BUY", entry_price=100.0,
                               stop_loss=95.0, target=108.0, confidence=80)
        return TradeSignal(symbol="T", action="HOLD", confidence=0)
    # Chain: make indicators not crash and a deterministic signal stream.
    # IMPORTANT: backtester calls generate_signal imported INTO its own
    # namespace — patch src.backtester.generate_signal, not the source module.
    with patch("src.backtester.fetch_market_data") as mf, \
         patch("src.backtester.compute_indicators") as ci, \
         patch("src.backtester.generate_signal", side_effect=_gs):
        from src.indicators import TechnicalReadings, IndicatorReading
        ci.return_value = TechnicalReadings(
            symbol="T", timeframe="1d",
            indicators={"atr_14": IndicatorReading("atr_14", 2.0)},
            swing_low=98.0, swing_high=102.0)
        # Set df's bar i+1 Open at 102.0 so slippage shifts are observable.
        df.loc[df.index[206], "Open"] = 102.0
        mf.return_value.ohlcv = df
        mf.return_value.latest_price = float(df["Close"].iloc[-1])
        res = run_backtest(symbol="T", timeframe="1d", period="1y",
                           commission_pct=0.0, slippage_pct=0.0,
                           indicator_lookback=200)
    assert isinstance(res, BacktestResult)
    # Exact next-bar contract: one BUY fired, filled at bar 206 Open = 102.0
    # (same-bar close would have been 100.0).
    assert res.total_trades == 1
    tr = res.trades[0]
    assert tr.entry_price == 102.0
    assert tr.entry_idx == 206
    assert tr.action == "BUY"


def test_sharpe_annualized_by_timeframe():
    """Intraday sharpe must use bars-per-year, not hardcoded sqrt(252)."""
    curve = [100.0, 101.0, 100.5, 102.0, 101.0, 103.0]
    r = _compute_metrics("T", "15m", "60d", 100.0, 103.0, curve, [], skipped_bars=0)
    r2 = _compute_metrics("T", "1d", "60d", 100.0, 103.0, curve, [], skipped_bars=0)
    assert r.sharpe_ratio != r2.sharpe_ratio
    assert r.sharpe_ratio != 0.0


def _exact_run(monkey_rows, signal_fn, commission=0.0, slippage=0.0,
               lookback=5):
    """Run the backtester on exact synthetic bars + a scripted signal."""
    from src.backtester import run_backtest
    df = _df_from_rows(monkey_rows)
    with patch("src.backtester.fetch_market_data") as mf, \
         patch("src.backtester.compute_indicators") as ci, \
         patch("src.backtester.generate_signal", side_effect=signal_fn):
        from src.indicators import TechnicalReadings, IndicatorReading
        ci.return_value = TechnicalReadings(
            symbol="T", timeframe="1d",
            indicators={"atr_14": IndicatorReading("atr_14", 2.0)},
            swing_low=98.0, swing_high=102.0)
        mf.return_value.ohlcv = df
        mf.return_value.latest_price = float(df["Close"].iloc[-1])
        return run_backtest(symbol="T", timeframe="1d", period="1y",
                            commission_pct=commission, slippage_pct=slippage,
                            indicator_lookback=lookback)


def _once_buy():
    from src.signal_engine import TradeSignal
    calls = {"n": 0}
    def _gs(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return TradeSignal(symbol="T", action="BUY", entry_price=100.0,
                               stop_loss=95.0, target=108.0, confidence=80)
        return TradeSignal(symbol="T", action="HOLD", confidence=0)
    return _gs


def test_next_open_gap_and_entry_slippage():
    # bar5 signal close 100 -> bar6 open 105 gap; 1% slippage -> 106.05
    rows = [{"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0,
             "Volume": 1000}] * 6 + [
        {"Open": 105.0, "High": 106.0, "Low": 104.0, "Close": 105.0, "Volume": 1000},
    ] + [{"Open": 105.0, "High": 130.0, "Low": 104.0, "Close": 125.0, "Volume": 1000}] * 8
    res = _exact_run(rows, _once_buy(), commission=0.0, slippage=1.0, lookback=5)
    assert res.total_trades == 1
    assert res.trades[0].entry_price == round(105.0 * 1.01, 2)  # 106.05


def test_stop_gap_pays_worse_open():
    # long stop 95; exit bar opens 90 (gap through) -> exit 90, not 95
    rows = [{"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0,
             "Volume": 1000}] * 6 + [
        {"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0, "Volume": 1000},
        {"Open": 90.0, "High": 92.0, "Low": 88.0, "Close": 91.0, "Volume": 1000},
    ] + [{"Open": 91.0, "High": 92.0, "Low": 90.0, "Close": 91.0, "Volume": 1000}] * 8
    res = _exact_run(rows, _once_buy(), commission=0.0, slippage=0.0, lookback=5)
    assert res.total_trades == 1
    assert res.trades[0].exit_reason == "stop_hit"
    assert res.trades[0].exit_price == 90.0


def test_same_bar_stop_before_target():
    # bar spans stop 95 AND target 108 -> conservative stop_hit
    rows = [{"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0,
             "Volume": 1000}] * 6 + [
        {"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0, "Volume": 1000},
        {"Open": 100.0, "High": 120.0, "Low": 80.0, "Close": 100.0, "Volume": 1000},
    ] + [{"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0, "Volume": 1000}] * 8
    res = _exact_run(rows, _once_buy(), commission=0.0, slippage=0.0, lookback=5)
    assert res.total_trades == 1
    assert res.trades[0].exit_reason == "stop_hit"


def test_notional_commission_charged():
    # 10 x 100 entry, 10 x 110 exit, 1% notional/leg -> cost 21 -> pnl 79
    rows = [{"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0,
             "Volume": 1000}] * 6 + [
        {"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0, "Volume": 1000},
        {"Open": 100.0, "High": 111.0, "Low": 99.0, "Close": 110.0, "Volume": 1000},
    ] + [{"Open": 110.0, "High": 111.0, "Low": 109.0, "Close": 110.0, "Volume": 1000}] * 8
    # force size 10 via a BUY whose risk math yields 10 — patch size instead
    from src.signal_engine import TradeSignal
    def _gs(**kw):
        return TradeSignal(symbol="T", action="HOLD", confidence=0)
    import src.backtester as bt
    orig_pos = bt.calculate_position_size
    res_holder = {}
    with patch("src.backtester.fetch_market_data") as mf, \
         patch("src.backtester.compute_indicators") as ci, \
         patch("src.backtester.generate_signal") as gs, \
         patch("src.backtester.calculate_position_size", return_value=10):
        from src.indicators import TechnicalReadings, IndicatorReading
        ci.return_value = TechnicalReadings(
            symbol="T", timeframe="1d",
            indicators={"atr_14": IndicatorReading("atr_14", 2.0)},
            swing_low=98.0, swing_high=102.0)
        fired = {"n": 0}
        def _g2(**kw):
            fired["n"] += 1
            if fired["n"] == 1:
                return TradeSignal(symbol="T", action="BUY", entry_price=100.0,
                                   stop_loss=95.0, target=109.0, confidence=80)
            return TradeSignal(symbol="T", action="HOLD", confidence=0)
        gs.side_effect = _g2
        df = _df_from_rows(rows)
        mf.return_value.ohlcv = df
        mf.return_value.latest_price = float(df["Close"].iloc[-1])
        res = bt.run_backtest(symbol="T", timeframe="1d", period="1y",
                              commission_pct=1.0, slippage_pct=0.0,
                              indicator_lookback=5)
    assert res.total_trades == 1
    # The backtester recomputes SL/TP from ITS OWN risk functions at the
    # signal close — the scripted signal's 109.0 target is NOT passed
    # through. What this test pins: a filled exit, notional cost math,
    # and the exact (entry 100.0, exit 103.38, pnl 13.46) trace for this
    # fixture so any fill/cost regression is caught by value drift.
    assert res.trades[0].exit_price == 103.38
    assert abs(res.trades[0].pnl - 13.46) < 0.05


def test_end_of_data_settlement_marks_final_equity():
    rows = [{"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0,
             "Volume": 1000}] * 6 + [
        {"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0, "Volume": 1000},
    ] + [{"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0, "Volume": 1000}] * 8
    res = _exact_run(rows, _once_buy(), commission=0.0, slippage=0.0, lookback=5)
    assert res.total_trades == 1
    assert res.trades[0].exit_reason == "end_of_data"
