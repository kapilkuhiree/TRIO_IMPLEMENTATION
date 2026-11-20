"""Quick live test of Zerodha integration."""
import os
from src.utils import load_env
from src.zerodha_market_data import ZerodhaMarketData

# Load environment variables
load_env()

print("=" * 50)
print("ZERODHA INTEGRATION TEST")
print("=" * 50)

# 1. Check environment variable
provider = os.environ.get("TRIO_MARKET_DATA_PROVIDER", "NOT SET")
print("\n1. TRIO_MARKET_DATA_PROVIDER: " + provider)
if provider == "zerodha":
    print("   STATUS: [OK] Zerodha provider ACTIVE")
else:
    print("   STATUS: [FAIL] Not Zerodha (integration disabled)")

# 2. Initialize adapter
try:
    adapter = ZerodhaMarketData.from_env()
    print("\n2. ZerodhaMarketData.from_env(): [OK] SUCCESS")

    # 3. Fetch live quote
    print("\n3. Fetching live NIFTY 50 quote...")
    quote = adapter.quote("NSE", ["NIFTY 50"])

    if quote and "NSE:NIFTY 50" in quote:
        data = quote["NSE:NIFTY 50"]
        last_price = data.get("last_price", "N/A")
        bid = data.get("bid", "N/A")
        ask = data.get("ask", "N/A")
        oi = data.get("open_interest", "N/A")
        volume = data.get("volume", "N/A")
        timestamp = data.get("timestamp", "N/A")

        print("   [OK] LIVE QUOTE SUCCESSFULLY FETCHED")
        print("   Last Price:  " + str(last_price))
        print("   Bid:         " + str(bid))
        print("   Ask:         " + str(ask))
        print("   Open Interest: " + str(oi))
        print("   Volume:      " + str(volume))
        print("   Timestamp:   " + str(timestamp))
        print("\nOVERALL STATUS: [OK] ZERODHA INTEGRATION 100% WORKING")
    else:
        print("   [WARN] Quote returned but unexpected format")
        print("   Raw: " + str(quote))

except Exception as e:
    print("\n[FAILED] " + type(e).__name__ + ": " + str(e))
    print("   Ensure .env file has valid ZERODHA_API_KEY and ZERODHA_ACCESS_TOKEN")