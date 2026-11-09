"""Diagnostic script to check Zerodha integration with TRIO options system."""

import os
import sys
from pathlib import Path

# Add project root to path
root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))

# 1. Check environment variable
print("=" * 60)
print("ENVIRONMENT CHECK")
print("=" * 60)
provider = os.environ.get("TRIO_MARKET_DATA_PROVIDER", "NOT SET")
print(f"TRIO_MARKET_DATA_PROVIDER = '{provider}'")
if provider == "zerodha":
    print("Status: [OK] Zerodha Active")
else:
    print("Status: [WARN] Not Zerodha (or not set)")
print()

# 2. Try importing the Zerodha adapter
print("=" * 60)
print("ADAPTER IMPORT CHECK")
print("=" * 60)
try:
    from src.zerodha_market_data import ZerodhaMarketData
    print("OK Successfully imported ZerodhaMarketData")

    # Try to initialize from env
    try:
        adapter = ZerodhaMarketData.from_env()
        print("OK Successfully initialized from .env environment variables")

        # Try a simple quote fetch
        print("\nAttempting to fetch NIFTY 50 quote...")
        quote = adapter.quote("NSE", ["NIFTY 50"])
        if quote and "NSE:NIFTY 50" in quote:
            data = quote["NSE:NIFTY 50"]
            print("OK Quote fetched successfully!")
            print(f"   Last Price: {data.get('last_price', 'N/A')}")
            print(f"   Bid: {data.get('bid', 'N/A')}")
            print(f"   Ask: {data.get('ask', 'N/A')}")
            print(f"   Timestamp: {data.get('timestamp', 'N/A')}")
        else:
            print("WARNING Quote returned but unexpected format")
            print(f"   Raw quote: {quote}")

    except Exception as e:
        print(f"FAILED Failed to initialize from .env: {type(e).__name__}: {e}")

except Exception as e:
    print(f"FAILED Failed to import ZerodhaMarketData: {e}")
    print("   Is kiteconnect installed? pip install kiteconnect")

print()

# 3. Check the options_chain_zerodha module
print("=" * 60)
print("OPTIONS CHAIN ZERODHA MODULE")
print("=" * 60)
try:
    from src.options_chain_zerodha import fetch_zerodha_chain
    print("OK Successfully imported fetch_zerodha_chain")

    # Try to fetch a chain
    print("\nAttempting to fetch NIFTY option chain from Zerodha...")
    chain = fetch_zerodha_chain("NIFTY")
    if chain:
        print("OK Chain fetched successfully!")
        print(f"   Underlying: {chain.get('underlying')}")
        print(f"   Symbol: {chain.get('symbol')}")
        by_expiry = chain.get('by_expiry', {})
        if by_expiry:
            first_expiry = list(by_expiry.keys())[0]
            print(f"   First expiry: {first_expiry}")
            first_row = by_expiry[first_expiry][0] if by_expiry[first_expiry] else {}
            print(f"   First row strike: {first_row.get('strike')}")
            print(f"   First row CE last: {first_row.get('CE', {}).get('lastPrice')}")
    else:
        print("WARNING Chain fetch returned None (may be expected or error)")

except Exception as e:
    print(f"FAILED Failed to import fetch_zerodha_chain: {e}")

print()
print("=" * 60)
print("DIAGNOSIS SUMMARY")
print("=" * 60)
print("""
If the Environment Check shows [OK] Zerodha Active but the Adapter/Chain checks fail,
the issue is likely a bridging problem between the Zerodha adapter and the 
options_intraday_runtime.py expectations.

The 'data_ok: 0' in your session logs means data is reaching the system but 
failing the internal validation checks (likely strike mapping, format mismatches, 
or missing contract metadata).

Recommended actions:
1. Ensure your .env file has valid ZERODHA_API_KEY and ZERODHA_ACCESS_TOKEN
2. Run this diagnostic to confirm connectivity
3. If diagnostics pass but trading still won't execute, we may need to verify 
   the chain format compatibility between zerodha_adapter and runtime.
""")