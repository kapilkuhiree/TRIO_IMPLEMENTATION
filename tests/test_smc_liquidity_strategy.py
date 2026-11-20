"""Tests for the SMC liquidity-sweep strategy.

The important property is *causality*: the planner calls ``evaluate_signal``
with a fresh instance on every completed bar, so a signal must depend only on
the candle prefix up to ``index``.  The fixture below builds one synthetic
session that actually produces a bullish setup, so the causality assertion is
not vacuous.
"""

from datetime import datetime, timedelta, timezone

from strategies.smc_liquidity_strategy import SmcLiquidityStrategy

IST = timezone(timedelta(hours=5, minutes=30))
SPECS = {"lot_size": 65, "strike_step": 50, "expiry_preference": "weekly"}


def _bar(ts, open_, high, low, close, symbol="NIFTY"):
    return {
        "timestamp": ts.isoformat(),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": 0.0,
        "underlying_symbol": symbol,
        "exchange": "NSE",
        "instrument_type": "index",
    }


def _bullish_setup():
    """Day 1 flat 99-101, day 2: rise, sweep 99, displace up, retrace into FVG.

    Bar map for day 2 (09:15 + 5m steps):

        6  local swing high 102
        8  quiet bar, high 100.2 (becomes the FVG lower edge)
        9  wick to 98.8 below the previous-day low of 99, close back at 99.9
       10  displacement close 103.5, above the 102 swing high -> MSS + FVG
       11  retrace to 100.3, inside the 100.2-100.5 gap -> entry trigger
    """
    day1 = datetime(2026, 11, 9, 9, 15, tzinfo=IST)
    candles = []
    for k in range(75):
        ts = day1 + timedelta(minutes=5 * k)
        candles.append(_bar(ts, 100, 101, 99, 100))

    day2 = datetime(2026, 11, 10, 9, 15, tzinfo=IST)

    def add(open_, high, low, close):
        candles.append(_bar(day2 + timedelta(minutes=5 * (len(candles) - 75)),
                            open_, high, low, close))

    add(100.0, 100.3, 99.8, 100.1)
    add(100.1, 100.5, 100.0, 100.3)
    add(100.3, 100.7, 100.2, 100.5)
    add(100.5, 100.9, 100.4, 100.7)
    add(100.7, 101.1, 100.6, 100.9)
    add(100.9, 101.3, 100.8, 101.1)
    add(101.0, 102.0, 100.9, 101.6)   # bar 6: swing high 102
    add(101.6, 101.7, 100.6, 100.9)   # bar 7
    add(100.1, 100.2, 99.6, 100.0)    # bar 8: FVG lower edge 100.2
    add(100.0, 100.1, 98.8, 99.9)     # bar 9: sweep of 99 + reclaim
    add(100.6, 103.8, 100.5, 103.5)   # bar 10: displacement + MSS + FVG
    add(103.4, 103.6, 100.3, 101.0)   # bar 11: retrace into the gap
    return candles


def test_bullish_setup_triggers_and_geometry_is_one_to_two():
    candles = _bullish_setup()
    signal = SmcLiquidityStrategy().evaluate_signal(
        candles, len(candles) - 1, SPECS, "NIFTY")

    assert signal.allowed is True
    assert signal.variant == "bullish"
    assert signal.option_type == "CE"
    assert signal.fvg_low < signal.entry_price < signal.fvg_high
    assert signal.stop_loss < signal.entry_price < signal.target
    assert abs((signal.target - signal.entry_price)
               / (signal.entry_price - signal.stop_loss) - 2.0) < 1e-6
    assert signal.bar_index == len(candles) - 1
    assert signal.sweep_bar_index < signal.displacement_bar_index < signal.bar_index


def test_signal_is_causal_and_ignores_appended_future_bars():
    candles = _bullish_setup()
    index = len(candles) - 1
    strategy = SmcLiquidityStrategy()
    expected = strategy.evaluate_signal(candles, index, SPECS, "NIFTY")
    assert expected.allowed is True

    # A violent future move must not change the signal that was already
    # available at ``index``.
    future = [
        _bar(datetime(2026, 11, 10, 10, 15, tzinfo=IST), 101.0, 130.0, 100.9, 129.0),
        _bar(datetime(2026, 11, 10, 10, 20, tzinfo=IST), 129.0, 131.0, 128.0, 130.0),
    ]
    assert strategy.evaluate_signal(candles + future, index, SPECS, "NIFTY") == expected
    assert strategy.scan_signals(candles + future)[0].get(index) == \
        strategy.scan_signals(candles)[0].get(index)


def test_no_signal_before_the_retrace_bar():
    candles = _bullish_setup()
    strategy = SmcLiquidityStrategy()
    # The setup is armed on the displacement bar but not filled until the
    # retrace, so bar 10 must not be an entry.
    for index in range(len(candles) - 2):
        assert strategy.evaluate_signal(candles, index, SPECS, "NIFTY").allowed is False
    assert strategy.evaluate_signal(candles, len(candles) - 1, SPECS, "NIFTY").allowed is True


def test_malformed_and_empty_history_is_rejected():
    strategy = SmcLiquidityStrategy()
    assert strategy.evaluate_signal([], 0).allowed is False
    assert strategy.evaluate_signal([{"high": 1}], 0).allowed is False
    assert strategy.evaluate_signal(_bullish_setup(), 10_000).allowed is False
    assert strategy.evaluate_signal(_bullish_setup(), -1).allowed is False


def test_candidate_pipeline_builds_an_smc_candidate():
    """The shadow wiring must reach SMC through the production entry point."""
    from candidate_pipeline import CandidatePipeline

    candles = _bullish_setup()
    signal = SmcLiquidityStrategy().evaluate_signal(
        candles, len(candles) - 1, SPECS, "NIFTY")
    assert signal.allowed is True

    candidates = CandidatePipeline.evaluate_strategy_signals_pure(
        candles,
        "smc_liquidity",
        underlying_symbol="NIFTY",
        contract_specs=SPECS,
        index=len(candles) - 1,
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.strategy_name == "SMC-Liquidity"
    assert candidate.direction == "bullish"
    assert candidate.option_type == "CE"
    assert candidate.entry_price == signal.entry_price
    assert candidate.stop_loss == signal.stop_loss
    assert candidate.target == signal.target
