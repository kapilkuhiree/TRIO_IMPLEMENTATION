"""
TRIO — Unit tests for the risk manager.

Tests pass an explicit config_override wherever a numeric threshold matters, so
they verify behaviour rather than the current contents of config.yaml.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.risk_manager import (
    add_position,
    apply_risk_management,
    calculate_position_size,
    calculate_stop_loss,
    calculate_take_profit,
    calculate_trailing_stop,
    reset_risk_state,
    update_pnl,
)
from src.signal_engine import TradeSignal

# Explicit risk config for tests: 2.0 ATR stop, 2R target, trailing on.
TEST_RM = {
    "capital": 100000,
    "risk_per_trade_pct": 1.5,
    "stop_loss": {
        "method": "atr",
        "atr_multiplier": 2.0,
        "percentage": 1.5,
        "min_stop_distance_pct": 0.0,
    },
    "take_profit": {"min_risk_reward": 2.0},
    "trailing_stop": {
        "enabled": True,
        "method": "atr",
        "atr_multiplier": 1.5,
        "percentage": 1.0,
    },
    "max_open_positions": 5,
    "max_daily_loss_pct": 5.0,
}


def setup_function():
    reset_risk_state()


def _live_cfg_capital_100k():
    """Live config with `capital` pinned to the value these tests were
    written against (100000). The account moved to the MegaBull Rs5L
    virtual balance on 2026-10-06; behaviour tests must verify mechanics
    (exposure cap, halt thresholds), not the current account size."""
    from unittest.mock import patch
    from src.utils import load_config
    cfg = dict(load_config())
    rm = dict(cfg.get("risk_management", {}))
    rm["capital"] = 100000
    cfg["risk_management"] = rm
    return patch("src.risk_manager.load_config", return_value=cfg)


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------

def test_position_size_fixed_risk():
    # risk budget = 100000 * 1.5% = 1500; distance = 10 => 150 shares.
    # capital pinned: account size is config, mechanics are the contract.
    size = calculate_position_size(entry_price=100.0, stop_loss=90.0,
                                   capital=100000)
    assert size == 150


def test_position_size_capped_by_exposure():
    # raw size would be 1500, but 20% of capital at price 100 is only 200
    size = calculate_position_size(entry_price=100.0, stop_loss=99.0,
                                   capital=100000)
    assert size == 200


def test_position_size_zero_when_stop_equals_entry():
    assert calculate_position_size(entry_price=100.0, stop_loss=100.0) == 0


# ---------------------------------------------------------------------------
# Stop loss
# ---------------------------------------------------------------------------

def test_stop_loss_atr_buy_and_sell():
    buy_sl = calculate_stop_loss(100.0, atr_value=2.0, action="BUY", config_override=TEST_RM)
    sell_sl = calculate_stop_loss(100.0, atr_value=2.0, action="SELL", config_override=TEST_RM)
    # atr_multiplier 2.0 => distance 4
    assert buy_sl == 96.0
    assert sell_sl == 104.0


def test_stop_loss_swing_places_below_recent_low():
    """The validated strategy: stop sits below the recent swing low."""
    rm = {
        **TEST_RM,
        "stop_loss": {
            "method": "swing",
            "swing_buffer_pct": 0.0,
            "atr_multiplier": 2.0,
            "min_stop_distance_pct": 0.0,
        },
    }
    sl = calculate_stop_loss(100.0, atr_value=2.0, action="BUY",
                             config_override=rm, swing_level=95.0)
    assert sl == 95.0


def test_stop_loss_swing_above_recent_high_for_short():
    rm = {
        **TEST_RM,
        "stop_loss": {
            "method": "swing",
            "swing_buffer_pct": 0.0,
            "atr_multiplier": 2.0,
            "min_stop_distance_pct": 0.0,
        },
    }
    sl = calculate_stop_loss(100.0, atr_value=2.0, action="SELL",
                             config_override=rm, swing_level=105.0)
    assert sl == 105.0


def test_stop_loss_falls_back_to_atr_without_swing_level():
    """No swing data available -> use ATR rather than failing."""
    rm = {
        **TEST_RM,
        "stop_loss": {"method": "swing", "atr_multiplier": 2.0, "min_stop_distance_pct": 0.0},
    }
    sl = calculate_stop_loss(100.0, atr_value=2.0, action="BUY", config_override=rm)
    assert sl == 96.0


def test_stop_loss_respects_min_distance_floor():
    """A stop closer than min_stop_distance_pct gets pushed out to the floor."""
    rm = {
        **TEST_RM,
        "stop_loss": {
            "method": "swing",
            "swing_buffer_pct": 0.0,
            "min_stop_distance_pct": 2.0,
        },
    }
    # swing low is only 0.2% below entry, but floor demands 2.0%
    sl = calculate_stop_loss(100.0, atr_value=1.0, action="BUY",
                             config_override=rm, swing_level=99.8)
    assert sl == 98.0


# ---------------------------------------------------------------------------
# Take profit
# ---------------------------------------------------------------------------

def test_take_profit_respects_risk_reward():
    tp = calculate_take_profit(entry_price=100.0, stop_loss=96.0, action="BUY", min_rr=2.0)
    assert tp == 108.0
    tp_sell = calculate_take_profit(entry_price=100.0, stop_loss=104.0, action="SELL", min_rr=2.0)
    assert tp_sell == 92.0


def test_take_profit_uses_validated_1_5_ratio():
    tp = calculate_take_profit(entry_price=100.0, stop_loss=93.0, action="BUY", min_rr=1.5)
    assert tp == 110.5


# ---------------------------------------------------------------------------
# Trailing stop
# ---------------------------------------------------------------------------

def test_trailing_stop_when_enabled():
    trail = calculate_trailing_stop(110.0, 100.0, atr_value=2.0, action="BUY",
                                    config_override=TEST_RM)
    # TEST_RM atr_multiplier 1.5 => distance 3
    assert trail == 107.0


def test_trailing_stop_returns_none_when_disabled():
    # Live config disables trailing because it failed out-of-sample testing.
    assert calculate_trailing_stop(110.0, 100.0, atr_value=2.0, action="BUY") is None
    rm = {**TEST_RM, "trailing_stop": {"enabled": False}}
    assert calculate_trailing_stop(110.0, 100.0, atr_value=2.0, action="BUY",
                                   config_override=rm) is None


# ---------------------------------------------------------------------------
# Apply risk management
# ---------------------------------------------------------------------------

def test_apply_risk_fills_trade_fields():
    signal = TradeSignal(symbol="TEST", action="BUY", entry_price=100.0, confidence=80)
    with _live_cfg_capital_100k():
        result = apply_risk_management(signal, atr_value=2.0, swing_level=96.0)

    # Uses live config: swing method with 0.25% buffer, 1.5R target
    # (measured config — all 21-day session replays ran at 1.5R).
    # 96.0 - 0.25% of 96 = 95.76
    assert result.stop_loss == 95.76
    assert result.target == 106.36  # 100 + (100 - 95.76)*1.5
    assert result.risk_reward_ratio == "1:1.5"
    assert result.position_size == 200  # capped by 20% exposure at price 100
    assert result.trailing_stop is False  # trailing is disabled in live config
    assert result.risk_check["halt_active"] is False


def test_apply_risk_hold_signal_leaves_levels_empty():
    signal = TradeSignal(symbol="TEST", action="HOLD", entry_price=100.0)
    result = apply_risk_management(signal, atr_value=2.0)
    assert result.stop_loss is None
    assert result.target is None
    assert result.position_size == 0


def test_daily_loss_halt_blocks_signal():
    with _live_cfg_capital_100k():
        update_pnl(-5000.0)  # 5% of 100000
        signal = TradeSignal(symbol="TEST", action="BUY", entry_price=100.0, confidence=90)
        result = apply_risk_management(signal, atr_value=2.0)
    assert result.action == "HOLD"
    assert result.confidence == 0
    assert result.risk_check["halt_active"] is True
    assert any("HALTED" in r for r in result.reasoning)


def test_max_positions_halt():
    for i in range(5):
        add_position(f"SYM{i}", size=1, entry=10.0, risk_amount=10.0)
    signal = TradeSignal(symbol="TEST", action="SELL", entry_price=50.0, confidence=70)
    result = apply_risk_management(signal, atr_value=1.0)
    assert result.action == "HOLD"
    assert result.risk_check["halt_active"] is True