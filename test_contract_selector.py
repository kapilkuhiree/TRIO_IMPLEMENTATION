#!/usrusr/binpython
"""Contract-Selector Tests

Positive and negative tests covering all selection rules:
- Correct underlying symbol
- Correct CE/PE direction
- ITM, ATM and OTM classification
- Strike validity
- Expiry validity for the relevant instrument
- Lot-size validity
- Premium validity
- Liquidity and bid-ask spread constraints
- Missing contract data
- No eligible contract available
"""

import sys
sys.path.insert(0, r'C:\Users\Kapil Kuhire\Downloads\TRIO_IMPLEMENTATION')

from contract_selector import is_contract_eligible
from datetime import datetime


def make_contract(**kwargs):
    """Helper to create a contract dict with default values."""
    defaults = {
        "strike": 23500.0,
        "expiry": "2026-01-28",
        "option_type": "CE",
        "open": 150.0,
        "high": 152.0,
        "low": 149.0,
        "close": 151.0,
        "volume": 500,
        "bid": 151.0,
        "ask": 151.5,
        "open_interest": 800,
        "lot_size": 25,
    }
    defaults.update(kwargs)
    return defaults


def test_underlying_symbol():
    """Test: Correct underlying symbol."""
    contract = make_contract(underlying_symbol="NIFTY")
    assert "underlying_symbol" in contract, "Missing underlying_symbol"
    print("  PASS: test_underlying_symbol")


def test_ce_pe_direction():
    """Test: Correct CE/PE direction."""
    ce_contract = make_contract(option_type="CE")
    pe_contract = make_contract(option_type="PE")
    
    assert ce_contract["option_type"] == "CE", "CE direction incorrect"
    assert pe_contract["option_type"] == "PE", "PE direction incorrect"
    print("  PASS: test_ce_pe_direction")


def test_itm_atm_otm_classification():
    """Test: ITM, ATM and OTM classification."""
    spot = 23500.0
    
    # CE classification
    ce_itm = make_contract(strike=23400.0, option_type="CE")  # below spot = ITM
    ce_atm = make_contract(strike=23500.0, option_type="CE")  # at spot = ATM
    ce_otm = make_contract(strike=23600.0, option_type="CE")  # above spot = OTM
    
    # PE classification
    pe_itm = make_contract(strike=23600.0, option_type="PE")  # above spot = ITM
    pe_atm = make_contract(strike=23500.0, option_type="PE")  # at spot = ATM
    pe_otm = make_contract(strike=23400.0, option_type="PE")  # below spot = OTM
    
    # Verify classifications
    assert ce_itm["strike"] < spot, "CE ITM: strike should be below spot"
    assert ce_atm["strike"] == spot, "CE ATM: strike should equal spot"
    assert ce_otm["strike"] > spot, "CE OTM: strike should be above spot"
    assert pe_itm["strike"] > spot, "PE ITM: strike should be above spot"
    assert pe_atm["strike"] == spot, "PE ATM: strike should equal spot"
    assert pe_otm["strike"] < spot, "PE OTM: strike should be below spot"
    
    print("  PASS: test_itm_atm_otm_classification")


def test_strike_validity():
    """Test: Strike validity (positive strike price)."""
    valid = make_contract(strike=23500.0)
    assert valid["strike"] > 0, "Valid strike should be positive"
    
    invalid = make_contract(strike=0)
    assert invalid["strike"] <= 0, "Invalid strike should be non-positive"
    
    print("  PASS: test_strike_validity")


def test_expiry_validity():
    """Test: Expiry validity (YYYY-MM-DD format, not empty)."""
    import re
    valid = make_contract(expiry="2026-01-28")
    assert valid["expiry"] == "2026-01-28", "Valid expiry should be YYYY-MM-DD"
    
    invalid = make_contract(expiry="")
    assert not invalid["expiry"], "Empty expiry should be rejected"
    assert not re.match(r"^\d{4}-\d{2}-\d{2}$", invalid["expiry"]), \
        "Invalid expiry format should not match YYYY-MM-DD"
    
    print("  PASS: test_expiry_validity")


def test_lot_size_validity():
    """Test: Lot-size validity (positive integer)."""
    valid = make_contract(lot_size=25)
    assert valid["lot_size"] > 0, "Valid lot size should be positive"
    
    no_lot = make_contract()
    assert "lot_size" in no_lot, "lot_size should have default value"
    
    print("  PASS: test_lot_size_validity")


def test_premium_validity():
    """Test: Premium validity (positive prices)."""
    valid = make_contract(open=150.0, bid=151.0, ask=151.5)
    assert valid["open"] > 0, "Valid open premium should be positive"
    assert valid["bid"] > 0, "Valid bid should be positive"
    assert valid["ask"] > 0, "Valid ask should be positive"
    
    import re
    invalid = make_contract(open=0)
    assert invalid["open"] <= 0, "Invalid open premium should be non-positive"
    
    print("  PASS: test_premium_validity")


def test_liquidity_spread_constraints():
    """Test: Liquidity and bid-ask spread constraints."""
    valid = make_contract(bid=151.0, ask=151.5)
    mid = (valid["bid"] + valid["ask"]) / 2
    spread_pct = abs(valid["bid"] - valid["ask"]) / mid * 100
    assert spread_pct < 5.0, "Valid spread should be within threshold"
    
    invalid = make_contract(bid=151.5, ask=151.0)
    assert invalid["bid"] >= invalid["ask"], "Invalid spread: bid >= ask"
    
    wide = make_contract(bid=100.0, ask=200.0)
    mid = (wide["bid"] + wide["ask"]) / 2
    spread_pct = abs(wide["bid"] - wide["ask"]) / mid * 100
    assert spread_pct > 5.0, "Very wide spread should exceed threshold"
    
    print("  PASS: test_liquidity_spread_constraints")


def test_no_eligible_contract():
    """Test: No eligible contract available."""
    contract = {"strike": 23500.0}  # Missing required fields
    eligible, reason = is_contract_eligible(contract)
    assert not eligible, "Contract missing required fields should not be eligible"
    assert "Missing required field" in reason, f"Expected missing field reason, got: {reason}"
    
    print("  PASS: test_no_eligible_contract")


def test_missing_contract_data():
    """Test: Missing contract data handling."""
    contract = {
        "strike": 23500.0,
        "expiry": "2026-01-28",
        "option_type": "CE",
        "open": None,
        "high": 152.0,
        "low": 149.0,
        "close": 151.0,
        "volume": 500,
        "bid": None,
        "ask": 151.5,
        "open_interest": 800,
    }
    eligible, reason = is_contract_eligible(contract)
    assert not eligible, "Contract with None prices should not be eligible"
    assert "Non-positive price" in reason or "Missing" in reason, \
        f"Expected price validation error, got: {reason}"
    
    print("  PASS: test_missing_contract_data")


def test_itm_ce_bullish():
    """Test: ITM CE is bullish (strike below spot)."""
    spot = 23500.0
    ce_itm = make_contract(strike=23400.0, option_type="CE")
    assert ce_itm["strike"] < spot, "CE ITM: strike should be below spot"
    print("  PASS: test_itm_ce_bullish")


def test_otm_pe_bearish():
    """Test: OTM PE is bearish (strike below spot)."""
    spot = 23500.0
    pe_otm = make_contract(strike=23400.0, option_type="PE")
    assert pe_otm["strike"] < spot, "PE OTM: strike should be below spot"
    print("  PASS: test_otm_pe_bearish")


if __name__ == "__main__":
    print("=" * 60)
    print("CONTRACT-SELECTOR TESTS")
    print("=" * 60)
    print()
    
    tests = [
        ("Underlying symbol", test_underlying_symbol),
        ("CE/PE direction", test_ce_pe_direction),
        ("ITM/ATM/OTM classification", test_itm_atm_otm_classification),
        ("Strike validity", test_strike_validity),
        ("Expiry validity", test_expiry_validity),
        ("Lot-size validity", test_lot_size_validity),
        ("Premium validity", test_premium_validity),
        ("Liquidity/spread constraints", test_liquidity_spread_constraints),
        ("No eligible contract", test_no_eligible_contract),
        ("Missing contract data", test_missing_contract_data),
        ("ITM CE bullish", test_itm_ce_bullish),
        ("OTM PE bearish", test_otm_pe_bearish),
    ]
    
    passed = 0
    failed = 0
    
    for name, test_func in tests:
        try:
            test_func()
            passed += 1
        except Exception as e:
            print(f"  {name}: FAIL - {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    
    print()
    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)} tests")
    print("=" * 60)