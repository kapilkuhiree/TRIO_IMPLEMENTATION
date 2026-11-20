"""Execution-model tests for the SMC backtest harness.

The retest entry is a resting limit at the FVG level, not a market order on
the next bar.  Getting that wrong silently destroys the 1:2 geometry, so it
is pinned down here.
"""

from datetime import datetime, timedelta, timezone

from scripts.backtest_smc import simulate

IST = timezone(timedelta(hours=5, minutes=30))
META = {"name": "TEST", "lot": 65, "step": 50.0, "vol": 0.18}


def _bar(minute, open_, high, low, close):
    ts = datetime(2026, 11, 10, 9, 15, tzinfo=IST) + timedelta(minutes=minute)
    return {"timestamp": ts.isoformat(), "open": open_, "high": high,
            "low": low, "close": close}


def _candles():
    return [
        _bar(0, 101.0, 102.0, 99.5, 101.5),   # trigger bar touches the 100 level
        _bar(5, 103.0, 104.0, 102.5, 103.5),  # next bar opens away from the gap
    ]


def _payload(**over):
    payload = {"variant": "bullish", "option_type": "CE", "entry_price": 100.0,
               "stop_loss": 98.0, "risk_reward": 2.0}
    payload.update(over)
    return {0: payload}


def test_limit_fill_uses_the_fvg_level_not_the_next_open():
    trades = simulate(_candles(), _payload(), META, fill="limit")
    assert len(trades) == 1
    assert trades[0]["entry_spot"] == 100.0
    assert trades[0]["stop"] == 98.0
    assert trades[0]["target"] == 104.0


def test_next_open_fill_recomputes_the_target_from_the_actual_fill():
    trades = simulate(_candles(), _payload(), META, fill="next_open")
    assert len(trades) == 1
    assert trades[0]["entry_spot"] == 103.0
    # 1:2 from the actual fill, not the stale 104 target.
    assert trades[0]["target"] == 113.0


def test_premium_floor_skips_penny_contracts():
    # The synthetic premium is well under Rs 5, so the tradeability floor
    # must drop the trade while a zero floor keeps it.
    assert simulate(_candles(), _payload(), META, fill="limit",
                    min_premium=5.0) == []
    assert len(simulate(_candles(), _payload(), META, fill="limit",
                        min_premium=0.0)) == 1


def test_itm_strike_is_below_spot_for_a_call():
    atm = simulate(_candles(), _payload(), META, fill="limit", moneyness_steps=0)
    itm = simulate(_candles(), _payload(), META, fill="limit", moneyness_steps=1)
    assert atm[0]["strike"] == 100.0
    assert itm[0]["strike"] == 50.0
    assert itm[0]["entry_premium"] > atm[0]["entry_premium"]
