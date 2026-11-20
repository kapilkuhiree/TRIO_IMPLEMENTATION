import os
from src.utils import load_env
from src.zerodha_market_data import ZerodhaMarketData
from datetime import datetime

# Load .env file
load_env()

def test_contract_generation():
    print("Connecting to Zerodha API...")
    try:
        zmd = ZerodhaMarketData.from_env()
        print("Authenticated successfully.")
        
        # Test finding a specific option contract
        # Using a common NIFTY strike as an example
        underlying = "NIFTY"
        # Use a datetime object for the expiry
        expiry_date = datetime.strptime("09-Nov-2026", "%d-%b-%Y").date()
        strike = 24100.0
        option_type = "CE"
        
        print(f"Searching for contract: {underlying} {expiry_date} {strike} {option_type}")
        
        # This will list instruments to find the match
        contract = zmd.find_option(underlying, expiry_date, strike, option_type)
        print("Successfully found contract:")
        print(contract)
        
        # Now fetch the quote for this contract
        print(f"Fetching quote for: {contract['tradingsymbol']}")
        quote = zmd.option_quote(contract)
        print("Successfully fetched quote:")
        print(quote)
            
    except Exception as e:
        print(f"Test failed: {e}")

if __name__ == "__main__":
    test_contract_generation()
