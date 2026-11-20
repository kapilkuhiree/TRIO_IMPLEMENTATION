"""Offline tests for the INR day-wise ORB / mean-reversion / QQQ comparison."""

from types import SimpleNamespace

import pandas as pd

from scripts.backtest_inr_daywise import (
    SpotTrade,
    _vwap_with_fallback,
    build_daywise,
    metrics,
    option_trade_objs,
    simulate_spot,
)

IST = "Asia/Kolkata"


def _candles(rows, day="2026-01-05"):
    """rows: list of (hh:mm, open, high, low, close)."""
    out = []
    for clock, open_, high, low, close in rows:
        stamp = pd.Timestamp(f"{day} {clock}", tz=IST)
        out.append({
            "timestamp": stamp.isoformat(),
            "open": float(open_), "high": float(high),
            "low": float(low), "close": float(close),
            "volume": 0.0, "underlying_symbol": "NIFTY",
            "exchange": "NSE", "instrument_type": "index",
        })
    return out


def _intraday():
    # Signal at 09:30; entry at the 09:35 open (earliest_min=15 allows 09:30).
    return _candles([
        ("09:15", 100, 101, 99, 100),
        ("09:20", 100, 101, 99, 100),
        ("09:25", 100, 101, 99, 100),
        ("09:30", 100, 101, 99, 100),
        ("09:35", 101, 103, 100.5, 102),
        ("09:40", 102, 104, 101, 103),
    ])


def _run(candles, signals, cost_bps=0.0):
    return simulate_spot(candles, signals, strategy="orb", earliest_min=15,
                         notional=1_000_000, cost_bps=cost_bps)


def test_vwap_fallback_ignores_zero_volume():
    candles = [
        {"timestamp": "2026-01-05T09:15:00+05:30",
         "high": 102, "low": 100, "close": 101, "volume": 0},
        {"timestamp": "2026-01-05T09:20:00+05:30",
         "high": 105, "low": 103, "close": 104, "volume": 0},
    ]
    # Equal-weighted typical prices: 101 and 104 -> 102.5
    assert abs(_vwap_with_fallback(candles) - 102.5) < 1e-9


def test_entry_is_next_bar_open_not_the_signal_close():
    trades = _run(_intraday(), {3: ("CE", 99.0, 110.0)})
    assert len(trades) == 1
    assert trades[0].entry_price == 101.0
    assert trades[0].entry_ts.startswith("2026-01-05T09:35")


def test_long_target_fills_at_the_target():
    trade = _run(_intraday(), {3: ("CE", 99.0, 104.0)})[0]
    assert trade.exit_reason == "target"
    assert trade.exit_price == 104.0
    assert abs(trade.pnl - (104.0 / 101.0 - 1.0) * 1_000_000) < 0.01


def test_long_stop_wins_when_a_bar_straddles_both_levels():
    # Stop 100.5 and target 102.5 are both inside the 09:35 bar.
    trade = _run(_intraday(), {3: ("CE", 100.5, 102.5)})[0]
    assert trade.exit_reason == "stop"
    assert trade.exit_price == 100.5


def test_short_stop_and_target_are_mirrored():
    trade = _run(_intraday(), {3: ("PE", 104.0, 98.0)})[0]
    assert trade.side == "short"
    assert trade.exit_reason == "stop"
    assert trade.exit_price == 104.0


def test_entry_is_skipped_when_price_already_ran_through_target():
    # Entry at 101 is already past a 100.5 long target.
    assert _run(_intraday(), {3: ("CE", 99.0, 100.5)}) == []


def test_eod_flat_uses_the_close_of_the_last_bar():
    trade = _run(_intraday(), {3: ("CE", 90.0, 200.0)})[0]
    assert trade.exit_reason == "eod"
    assert trade.exit_price == 103.0


def test_costs_reduce_the_pnl_by_two_sides():
    gross = _run(_intraday(), {3: ("CE", 90.0, 200.0)}, cost_bps=0.0)[0].pnl
    net = _run(_intraday(), {3: ("CE", 90.0, 200.0)}, cost_bps=5.0)[0].pnl
    assert net < gross
    exit_notional = 1_000_000 * (103.0 / 101.0)
    expected = 1_000_000 * 0.0005 + exit_notional * 0.0005
    assert abs((gross - net) - expected) < 0.01


def test_metrics_drawdown_starts_from_the_notional():
    def trade(pnl):
        return SpotTrade(strategy="x", day="2026-01-05",
                         entry_ts="2026-01-05T09:30", exit_ts="2026-01-05T15:10",
                         side="long", entry_price=100.0, exit_price=100.0,
                         qty=1.0, return_pct=pnl / 1_000_000 * 100.0,
                         pnl=pnl, exit_reason="eod")
    stats = metrics([trade(-50_000.0), trade(20_000.0)], 1_000_000.0)
    assert stats["net_pnl"] == -30_000.0
    assert stats["max_drawdown"] == -50_000.0
    assert stats["max_drawdown_pct"] == 5.0


def test_build_daywise_carries_the_running_equity():
    frame = build_daywise(
        days=["2026-01-05", "2026-01-06", "2026-01-07"],
        series={"orb": {"2026-01-05": 100.0, "2026-01-07": -40.0}},
        notional=1_000_000.0,
    )
    assert list(frame["orb"]) == [100.0, 0.0, -40.0]
    assert list(frame["orb_cum"]) == [1_000_100.0, 1_000_100.0, 1_000_060.0]


def test_option_trade_objs_normalise_pnl_and_exit_day():
    raw = [{"pnl": 2_500.0, "exit_ts": "2026-01-05T15:10:00+05:30",
            "day": "2026-01-05"}]
    objs = option_trade_objs(raw, 1_000_000.0)
    assert objs[0].pnl == 2_500.0
    assert abs(objs[0].return_pct - 0.25) < 1e-9
    assert objs[0].exit_date == "2026-01-05"
