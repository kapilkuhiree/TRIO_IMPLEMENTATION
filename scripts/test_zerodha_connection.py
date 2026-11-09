import os
from src.utils import load_env
from src.zerodha_market_data import ZerodhaMarketData

# Load .env file
load_env()

def test_connection():
    print("Attempting to connect to Zerodha API...")
    try:
        # This will initialize the connection using your .env variables
        zmd = ZerodhaMarketData.from_env()
        print("Successfully authenticated with Zerodha!")
        
        # Try fetching a quote for NIFTY to verify data availability
        # NIFTY 50 trading symbol on NSE
        print("Fetching quote for NIFTY 50...")
        quote = zmd.quote("NSE", ["NIFTY 50"])
        
        if quote:
            print("Successfully fetched data from Zerodha:")
            print(quote)
        else:
            print("Connected, but failed to fetch data.")
            
    except Exception as e:
        print(f"Connection/Fetch failed: {e}")

if __name__ == "__main__":
    test_connection()
