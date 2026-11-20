"""Offline tests for the wide NFO options backtest engine."""

import math

import numpy as np
import pandas as pd

from scripts.backtest_nfo_options_wide import (
    _delta,
    build_legs,
    entry_iv,
    leg_iv,
    month_table,
    realized_vol,
    simulate_symbol,
    strike_for_delta,
    summarize_trades,
)


def _bars(n=420, start="2020-01-01", spot=1000.0, vol=0.25, seed=7):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=n)
    steps = rng.normal(0.0, vol / math.sqrt(252.0), n)
    close = spot * np.exp(np.cumsum(steps))
    open_ = close * (1.0 + rng.normal(0.0, 0.002, n))
    frame = pd.DataFrame({
        "Open": open_,
        "High": np.maximum(open_, close) * 1.01,
        "Low": np.minimum(open_, close) * 0.99,
        "Close": close,
        "Adj Close": close,
        "Volume": 1_000_000.0,
    }, index=dates)
    frame.index.name = "Date"
    return frame


def _meta(lot=100, step=5.0, kind="stock"):
    return {"name": "TEST", "lot": lot, "step": step, "vol": 0.18,
            "ticker": "TEST.NS", "kind": kind}


def _run(bars, variant="short_put", **kwargs):
    params = dict(variant=variant, notional=1_000_000.0, dte=30.0,
                  vrp=1.15, skew=0.006, profit_target=0.5, stop=2.0,
                  spread_pct=0.0025, since=None, until=None)
    params.update(kwargs)
    return simulate_symbol("TEST", _meta(), bars, **params)


def test_realized_vol_is_zero_for_a_flat_series():
    close = pd.Series([100.0] * 60)
    assert realized_vol(close, 20).dropna().abs().max() < 1e-12


def test_entry_iv_floors_low_vol_and_scales_by_vrp():
    assert abs(entry_iv(0.05, 1.0) - 0.12) < 1e-12
    assert abs(entry_iv(0.30, 1.15) - 0.345) < 1e-9
    # NaN realised vol falls back to a usable level, never NaN.
    assert np.isfinite(entry_iv(float("nan"), 1.15))


def test_put_skew_only_applies_below_spot():
    assert abs(leg_iv(0.18, 1000, 900, "PE", 0.006) - 0.24) < 1e-9
    assert abs(leg_iv(0.18, 1000, 1100, "CE", 0.006) - 0.18) < 1e-9
    assert abs(leg_iv(0.18, 1000, 1050, "PE", 0.006) - 0.18) < 1e-9


def test_strike_solver_hits_the_target_delta_after_rounding():
    for target in (0.15, 0.30):
        strike = strike_for_delta(1000, 30 / 365, 0.18, "PE", target, 5)
        delta = abs(_delta(1000, strike, 30 / 365, 0.18, "PE"))
        assert abs(delta - target) < 0.05
    strike = strike_for_delta(1000, 30 / 365, 0.18, "CE", 0.25, 5)
    assert abs(_delta(1000, strike, 30 / 365, 0.18, "CE") - 0.25) < 0.05


def test_build_legs_shapes_and_directions():
    short_put = build_legs("short_put", 1000, 30 / 365, 0.18, 5, 0.006)
    assert len(short_put) == 1 and short_put[0]["direction"] == 1
    spread = build_legs("bull_put_spread", 1000, 30 / 365, 0.18, 5, 0.006)
    assert [leg["direction"] for leg in spread] == [1, -1]
    assert spread[0]["strike"] > spread[1]["strike"]
    strangle = build_legs("short_strangle", 1000, 30 / 365, 0.18, 5, 0.006)
    assert {leg["kind"] for leg in strangle} == {"PE", "CE"}
    long_call = build_legs("long_call", 1000, 30 / 365, 0.18, 5, 0.006)
    assert long_call[0]["direction"] == -1


def test_entries_fill_at_the_next_open_and_daily_sums_to_net():
    bars = _bars()
    trades = _run(bars)
    assert trades
    trade = trades[0]
    entry = pd.Timestamp(trade["entry_date"])
    assert abs(trade["entry_spot"] - bars.loc[entry, "Open"]) < 0.01
    total = sum(pnl for _, pnl in trade["daily"])
    assert abs(total - trade["pnl"]) < 0.05


def test_one_position_at_a_time():
    trades = _run(_bars(n=900))
    assert len(trades) > 2
    for previous, current in zip(trades, trades[1:]):
        assert current["entry_date"] > previous["exit_date"]


def test_higher_vrp_makes_the_short_put_book_more_profitable():
    bars = _bars()
    fair = summarize_trades(_run(bars, vrp=1.0))
    rich = summarize_trades(_run(bars, vrp=1.30))
    assert rich["net_pnl"] > fair["net_pnl"]


def test_fair_pricing_short_put_is_not_a_free_lunch():
    stats = summarize_trades(_run(_bars(), vrp=1.0))
    # At fair vol the book is a coin flip: no large edge on margin.
    assert abs(stats["net_pnl"]) / stats["margin"] < 0.10


def test_pre_split_price_with_a_coarse_strike_grid_is_skipped():
    bars = _bars(spot=0.04)
    assert _run(bars) == []


def test_bull_put_spread_risk_is_capped():
    stats = summarize_trades(_run(_bars(), variant="bull_put_spread"))
    trades = _run(_bars(), variant="bull_put_spread")
    assert stats["trades"] > 0
    for trade in trades:
        width = abs(trade["legs"][0]["strike"] - trade["legs"][1]["strike"])
        assert trade["margin"] <= width * trade["qty"] + 1e-6


def test_month_table_groups_by_exit_month():
    trades = _run(_bars(n=900))
    months = month_table(trades)
    assert not months.empty
    assert months["trades"].sum() == len(trades)
    assert abs(months["net_pnl"].sum() - sum(t["pnl"] for t in trades)) < 1.0


def test_zero_profit_target_holds_to_stop_or_expiry():
    trades = _run(_bars(), profit_target=0.0)
    assert trades
    assert all(t["exit_reason"] in {"stop", "expiry"} for t in trades)


def test_trend_filter_skips_a_downtrend():
    dates = pd.bdate_range("2020-01-01", periods=420)
    close = np.linspace(2000.0, 400.0, len(dates))
    frame = pd.DataFrame({
        "Open": close, "High": close * 1.01, "Low": close * 0.99,
        "Close": close, "Adj Close": close, "Volume": 1_000_000.0,
    }, index=dates)
    frame.index.name = "Date"
    assert _run(frame, trend_filter=True) == []
    assert _run(frame, trend_filter=False)


def test_summarize_reports_profit_factor_and_drawdown():
    trades = [
        {"pnl": 100.0, "margin": 1000.0, "exit_date": "2026-01-05",
         "bars": 2, "exit_reason": "target"},
        {"pnl": -50.0, "margin": 1000.0, "exit_date": "2026-01-06",
         "bars": 2, "exit_reason": "stop"},
    ]
    stats = summarize_trades(trades)
    assert stats["trades"] == 2
    assert stats["net_pnl"] == 50.0
    assert abs(stats["profit_factor"] - 2.0) < 1e-9
    assert stats["max_drawdown"] == -50.0
    assert stats["win_rate"] == 50.0
