"""
TRIO — Robust Backtest Evaluator
Runs the verified strategy (daily pullback) on 2yr history.
"""

import sys
from pathlib import Path
import logging
logging.disable(logging.CRITICAL)

# Add project root
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.backtester import run_backtest

SYMBOLS = ['RELIANCE.NS', 'TCS.NS', 'INFY.NS', 'HDFCBANK.NS', 'SBIN.NS']

def run():
    print(f"Backtesting verified Pullback Long-Only strategy (2yr history)...\n")
    
    for sym in SYMBOLS:
        try:
            r = run_backtest(sym, timeframe='1d', period='2y')
            # Detail the trades so we can see why it won/lost
            print(f"--- {sym} ---")
            print(r.summary_str())
            
            # Count exits
            exits = [t.exit_reason for t in r.trades]
            print(f"Exit counts: {dict((x, exits.count(x)) for x in set(exits))}")
            print("\n")
        except Exception as e:
            print(f"{sym} failed: {e}")

if __name__ == "__main__":
    run()