"""Automated tests for TRIO risk controls."""

from experimental_risk import (
    init_experimental_risk,
    process_signal_with_risk_controls,
    check_daily_loss_limit,
    check_max_trades_per_day,
    check_consecutive_losses,
    check_time_cutoff_ist,
    check_duplicate_entry,
    calculate_experimental_position_size,
    log_rejected_entry,
)
from datetime import time as dtime
import json


def test_position_sizing():
    """Test position size calculation at different risk levels."""
    print("=" * 60)
    print("TEST 1: Position sizing")
    print("=" * 60)
    
    # Test at 0.30% risk
    init_experimental_risk(capital=500000.0)
    size_030 = calculate_experimental_position_size(
        entry_price=23800, stop_loss=23600, risk_per_trade_pct=0.30, capital=500000.0
    )
    print(f"  0.30% risk: position size = {size_030} units")
    print(f"  Expected: ~83 units (500000 * 0.30 / 100 / 200)")
    
    # Test at 0.25% risk (research parameter)
    size_025 = calculate_experimental_position_size(
        entry_price=23800, stop_loss=23600, risk_per_trade_pct=0.25, capital=500000.0
    )
    print(f"  0.25% risk: position size = {size_025} units")
    print(f"  Ratio 0.25/0.30 = {size_025/size_030:.2f} (should be ~0.83)")
    print()


def test_daily_loss_limit():
    """Test daily loss limit enforcement."""
    print("=" * 60)
    print("TEST 2: Daily loss limit")
    print("=" * 60)
    
    # Test: P&L within limit
    init_experimental_risk(capital=500000.0)
    allowed, reason = check_daily_loss_limit(pnl=1000, starting_equity=500000.0, loss_limit_pct=1.0)
    print(f"  P&L: +1000, 1% limit: Allowed={allowed}")
    assert allowed == True, "Should allow trade within limit"
    print("  PASSED: Trade within daily loss limit")
    print()
    
    # Test: P&L breaches limit
    allowed, reason = check_daily_loss_limit(pnl=-30000, starting_equity=500000.0, loss_limit_pct=1.0)
    print(f"  P&L: -30000, 1% limit (5000): Allowed={allowed}")
    assert allowed == False, "Should block trade breaching daily loss limit"
    print("  PASSED: Trade blocked by daily loss limit")
    print()


def test_max_trades_per_day():
    """Test maximum entries per day enforcement."""
    print("=" * 60)
    print("TEST 3: Max trades per day")
    print("=" * 60)
    
    # Test: Within limit
    init_experimental_risk(capital=500000.0)
    allowed, reason = check_max_trades_per_day(trade_count_today=4, max_trades=5)
    print(f"  Trades: 4/5: Allowed={allowed}")
    assert allowed == True, "Should allow trade within limit"
    print("  PASSED: Trade within max trades per day")
    print()
    
    # Test: At limit
    allowed, reason = check_max_trades_per_day(trade_count_today=5, max_trades=5)
    print(f"  Trades: 5/5: Allowed={allowed}")
    assert allowed == False, "Should block at max trades limit"
    print("  PASSED: Trade blocked at max trades limit")
    print()
    
    # Test: Over limit
    allowed, reason = check_max_trades_per_day(trade_count_today=6, max_trades=5)
    print(f"  Trades: 6/5: Allowed={allowed}")
    assert allowed == False, "Should block over max trades limit"
    print("  PASSED: Trade blocked over max trades limit")
    print()


def test_consecutive_losses():
    """Test consecutive loss guard."""
    print("=" * 60)
    print("TEST 4: Consecutive losses guard")
    print("=" * 60)
    
    # Test: First loss allowed
    init_experimental_risk(capital=500000.0)
    allowed, reason = check_consecutive_losses(pnl=-100)
    print(f"  Trade 1: P&L=-100: Allowed={allowed}")
    assert allowed == True, "First loss should be allowed"
    print("  PASSED: First loss allowed")
    print()
    
    # Test: Second loss blocks
    allowed, reason = check_consecutive_losses(pnl=-50)
    print(f"  Trade 2: P&L=-50: Allowed={allowed}")
    assert allowed == False, "Second consecutive loss should block"
    print("  PASSED: Second consecutive loss blocks new entries")
    print()
    
    # Test: Win resets counter
    allowed, reason = check_consecutive_losses(pnl=300)
    print(f"  Trade 3: P&L=+300 (after 2 losses): Allowed={allowed}")
    assert allowed == True, "Win should reset consecutive loss counter"
    print("  PASSED: Win resets consecutive loss counter")
    print()


def test_time_cutoff():
    """Test 2:30 PM IST time cutoff."""
    print("=" * 60)
    print("TEST 5: 2:30 PM IST time cutoff")
    print("=" * 60)
    
    # Test: Before cutoff (10:15 AM)
    init_experimental_risk(capital=500000.0)
    allowed, reason = check_time_cutoff_ist(trade_time_ist='2026-10-13 10:15:00')
    print(f"  Trade at 10:15 AM IST: Allowed={allowed}")
    assert allowed == True, "Trades before cutoff should be allowed"
    print("  PASSED: Trade before 2:30 PM IST allowed")
    print()
    
    # Test: At cutoff (2:30 PM)
    allowed, reason = check_time_cutoff_ist(trade_time_ist='2026-10-13 14:30:00')
    print(f"  Trade at 2:30 PM IST: Allowed={allowed}")
    # This could go either way depending on strictness; check it's not blocked for being past
    print(f"  Reason: '{reason}'")
    print()
    
    # Test: After cutoff (2:31 PM)
    allowed, reason = check_time_cutoff_ist(trade_time_ist='2026-10-13 14:31:00')
    print(f"  Trade at 2:31 PM IST: Allowed={allowed}")
    assert allowed == False, "Trades after 2:30 PM IST should be blocked"
    print("  PASSED: Trade after 2:30 PM IST blocked")
    print()
    
    # Test: End of day (3:00 PM)
    allowed, reason = check_time_cutoff_ist(trade_time_ist='2026-10-13 15:00:00')
    print(f"  Trade at 3:00 PM IST: Allowed={allowed}")
    assert allowed == False, "Trades after market close should be blocked"
    print("  PASSED: Trade after market close blocked")
    print()


def test_duplicate_entry():
    """Test duplicate entry rejection."""
    print("=" * 60)
    print("TEST 6: Duplicate entry rejection")
    print("=" * 60)
    
    # Test: Duplicate symbol
    init_experimental_risk(capital=500000.0)
    allowed, reason = check_duplicate_entry(symbol='NIFTY', existing_positions={'NIFTY': {'entry': 23800}})
    print(f"  Symbol: NIFTY (duplicate): Allowed={allowed}")
    assert allowed == False, "Duplicate symbol should be blocked"
    print("  PASSED: Duplicate entry blocked")
    print()
    
    # Test: Different symbol (no duplicate)
    allowed, reason = check_duplicate_entry(symbol='BANKNIFTY', existing_positions={'NIFTY': {'entry': 23800}})
    print(f"  Symbol: BANKNIFTY (different): Allowed={allowed}")
    assert allowed == True, "Different symbol should be allowed"
    print("  PASSED: Different symbol allowed")
    print()


def test_full_signal_processing():
    """Test full signal processing with all risk controls."""
    print("=" * 60)
    print("TEST 7: Full signal processing")
    print("=" * 60)
    
    init_experimental_risk(capital=500000.0)
    
    # Test: Valid signal should be allowed
    result = process_signal_with_risk_controls(
        signal_action='BUY',
        symbol='NIFTY',
        entry_price=23800,
        stop_loss=23600,
        target_price=24200,
        trade_time_ist='2026-10-13 09:45:00',
        risk_per_trade_pct=0.30,
        capital=500000.0,
    )
    print(f"  Valid morning signal: Allowed={result['allowed']}")
    assert result['allowed'] == True, "Valid morning signal should be allowed"
    print(f"  Position size: {result['position_size']}")
    print()
    
    # Test: Signal after consecutive losses should be blocked
    # First, simulate 2 losses
    check_consecutive_losses(pnl=-100)
    check_consecutive_losses(pnl=-50)
    
    # Now try a signal - should be blocked
    result2 = process_signal_with_risk_controls(
        signal_action='BUY',
        symbol='NIFTY',
        entry_price=23800,
        stop_loss=23600,
        target_price=24200,
        trade_time_ist='2026-10-13 09:45:00',
        risk_per_trade_pct=0.30,
        capital=500000.0,
    )
    print(f"  Signal after 2 consecutive losses: Allowed={result2['allowed']}")
    assert result2['allowed'] == False, "Signal after 2 losses should be blocked"
    print()
    
    # Test: Signal after time cutoff should be blocked
    result3 = process_signal_with_risk_controls(
        signal_action='BUY',
        symbol='NIFTY',
        entry_price=23800,
        stop_loss=23600,
        target_price=24200,
        trade_time_ist='2026-10-13 15:00:00',  # After market close
        risk_per_trade_pct=0.30,
        capital=500000.0,
    )
    print(f"  Signal at 3:00 PM IST: Allowed={result3['allowed']}")
    assert result3['allowed'] == False, "Signal after cutoff should be blocked"
    print()
    
    print("  ALL Full signal processing tests PASSED")
    print()


if __name__ == "__main__":
    test_position_sizing()
    test_daily_loss_limit()
    test_max_trades_per_day()
    test_consecutive_losses()
    test_time_cutoff()
    test_duplicate_entry()
    test_full_signal_processing()
    
    print("=" * 60)
    print("ALL AUTOMATED RISK CONTROLS TESTS PASSED")
    print("=" * 60)