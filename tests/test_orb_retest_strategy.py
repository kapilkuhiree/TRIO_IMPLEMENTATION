"""Tests for the ORB-Retest v2 state machine."""

from datetime import datetime, timedelta, timezone

import pytest

from strategies.orb_retest_strategy import OrbRetestStrategy

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


def _session(*, fail_retest=False):
    """Build one day: OR 88-100, breakout, retest, resumption."""
    base = datetime(2026, 11, 9, 9, 15, tzinfo=IST)
    candles = []
    # Previous-day bars give ATR(14) a stable ~2-point range.
    for k in range(20):
        ts = base - timedelta(days=1) + timedelta(minutes=5 * k)
        candles.append(_bar(ts, 100, 101, 99, 100))
    # Opening range 09:15-09:30: high 100, low 88 => 12 points wide.
    candles.append(_bar(base, 95, 100, 88, 95))
    candles.append(_bar(base + timedelta(minutes=5), 92, 96, 89, 94))
    candles.append(_bar(base + timedelta(minutes=10), 94, 99, 91, 98))
    # 09:30 stays inside the range.
    candles.append(_bar(base + timedelta(minutes=15), 98, 99.5, 97, 98.5))
    # 09:35 breakout close (body 0.5, not an exhaustion thrust).
    candles.append(_bar(base + timedelta(minutes=20), 101, 103, 101, 102))
    # 09:40 retest of OR high.
    candles.append(_bar(base + timedelta(minutes=25), 102, 102.2, 99.8, 100.5))
    if fail_retest:
        # 09:45 rejects back through the range instead of resuming.
        candles.append(_bar(base + timedelta(minutes=30), 100.5, 100.8, 87.0, 88.0))
        candles.append(_bar(base + timedelta(minutes=35), 88.0, 88.5, 86.0, 86.5))
    else:
        # 09:45 resumption close beyond OR high.
        candles.append(_bar(base + timedelta(minutes=30), 100.8, 102.8, 100.6, 102.5))
        candles.append(_bar(base + timedelta(minutes=35), 102.5, 103, 101.8, 102.2))
    return candles


def _evaluate(candles, index, **params):
    # The synthetic series is deliberately compressed in price, so its ATR is
    # a much larger percentage of spot than a real index.  Widen the vol band
    # to isolate the retest state machine from that scale artifact.
    params.setdefault("atr_pct_max", 10.0)
    strategy = OrbRetestStrategy(lot_size=65, **params)
    return strategy.evaluate_signal(candles, index, SPECS, "NIFTY")


def test_breakout_bar_does_not_enter_and_resumption_does():
    candles = _session()
    breakout_index = 24  # 09:35
    assert not _evaluate(candles, breakout_index).allowed

    resumption_index = 26  # 09:45
    signal = _evaluate(candles, resumption_index)
    assert signal.allowed
    assert signal.option_type == "CE"
    assert signal.variant == "bullish"
    assert signal.stop_loss < signal.entry_price < signal.target
    assert signal.breakout_bar_index == breakout_index
    assert signal.retest_bar_index is not None


def test_failed_retest_never_enters():
    candles = _session(fail_retest=True)
    # No bar after the failed retest should produce a bullish resumption.
    for index in range(23, 28):
        signal = _evaluate(candles, index)
        assert not signal.allowed


def test_narrow_opening_range_is_rejected():
    candles = _session()
    # A 3.5xATR floor should reject this ~3.6x range only if we raise the bar.
    assert not _evaluate(candles, 26, or_min_width_atr=6.0).allowed
    assert _evaluate(candles, 26, or_min_width_atr=3.0).allowed


def test_entry_window_is_enforced():
    candles = _session()
    # 09:45 is minute 30 since 09:15.
    assert _evaluate(candles, 26, earliest_minute=30).allowed
    assert not _evaluate(candles, 26, earliest_minute=45).allowed


def test_wrong_underlying_is_rejected():
    candles = _session()
    strategy = OrbRetestStrategy(lot_size=65)
    signal = strategy.evaluate_signal(candles, 26, SPECS, "BANKNIFTY")
    assert not signal.allowed
