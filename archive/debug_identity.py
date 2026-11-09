"""Debug NSE symbol identity mismatch."""
from src.options_chain import _symbol_identity, _response_identity_failure
import json

# Test 1: Basic identity function
print("=== Test 1: _symbol_identity ===")
print(f"_symbol_identity('NIFTY'): {_symbol_identity('NIFTY')}")
print(f"_symbol_identity('NIFTY.NS'): {_symbol_identity('NIFTY.NS')}")

# Test 2: Simulate NSE response identities
print("\n=== Test 2: Identity extraction ===")
test_data = {
    "records": {
        "data": [
            {
                "symbol": "NIFTY",  # This is how NSE returns it
                "underlying_symbol": "NIFTY",
                "underlyingSymbol": "NIFTYIDX",  # Maybe NSE uses this format now?
            }
        ]
    }
}

symbol = "NIFTY"
identities = []
for key in ("symbol", "underlying_symbol", "underlyingSymbol", "underlying"):
    val = test_data["records"]["data"][0].get(key)
    ident = _symbol_identity(val) if val else ""
    identities.append(f"{key}={val!r} → {ident!r}")
    print(f"  {identities[-1]}")

# Test 3: Full identity failure check
print("\n=== Test 3: _response_identity_failure (require_identity=False) ===")
result = _response_identity_failure(test_data, symbol, False)
print(f"Result (require_identity=False): {result}")

print("\n=== Test 4: _response_identity_failure (require_identity=True) ===")
result2 = _response_identity_failure(test_data, symbol, True)
print(f"Result (require_identity=True): {result2}")

# Test 5: What if NSE returns underlying_symbol as something different?
print("\n=== Test 5: Alternative underlying format ===")
test_data2 = {
    "records": {
        "data": [
            {
                "symbol": "NIFTY",
                "underlying_symbol": "NIFTYIDX",  # NSE might use this format
                "underlying": "NIFTY"
            }
        ]
    }
}
result3 = _response_identity_failure(test_data2, symbol, True)
print(f"Result with underlying_symbol=NIFTYIDX: {result3}")

# Test 6: What if response has "NIFTY" in a different field?
print("\n=== Test 6: Different field layouts ===")
# NSE might put the symbol in different places
test_cases = [
    {"symbol": "NIFTY"},
    {"underlying_symbol": "NIFTY"},
    {"underlyingSymbol": "NIFTY"},
    {"underlying": "NIFTY"},
    {"symbol": "NIFTYIDX"},
    {"underlying_symbol": "NIFTYIDX"},
    {"underlyingSymbol": "NIFTYIDX"},
]
for tc in test_cases:
    data = {"records": {"data": [tc]}} if tc.get("symbol") or tc.get("underlying_symbol") or tc.get("underlyingSymbol") or tc.get("underlying") else {"records": {"data": []}}
    # Build proper data dict
    rows = []
    if tc.get("symbol"): rows.append({"symbol": tc["symbol"]})
    if tc.get("underlying_symbol"): rows.append({"underlying_symbol": tc["underlying_symbol"]})
    if tc.get("underlyingSymbol"): rows.append({"underlyingSymbol": tc["underlyingSymbol"]})
    if tc.get("underlying"): rows.append({"underlying": tc["underlying"]})
    if not rows:
        data = {"records": {"data": []}}
    else:
        data = {"records": {"data": rows}}
    r = _response_identity_failure(data, "NIFTY", True)
    print(f"  {str(tc)[:40]:40s} → result={r}")