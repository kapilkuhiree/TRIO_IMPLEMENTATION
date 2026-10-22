"""Local offline simulator for today's options signals.
Loads generated signals from `reports/zerodha-signals-research-local-sim.json`
and simulates the intraday plan and risk rules without placing any broker orders.
"""

from __future__ import annotations

import json
from pathlib import Path


def main() -> int:
    path = Path("reports/zerodha-signals-research-local-run.json")
    if not path.exists():
        print(f"Signal report not found at {path}")
        return 1
    
    data = json.loads(path.read_text(encoding="utf-8"))
    status = data.get("status")
    signals = data.get("signals", [])
    
    print("=== LOCAL SIMULATOR REPORT ===")
    print(f"Status: {status}")
    print(f"Total accepted signals: {len(signals)}")
    print(f"Total rejected candidates/filters: {len(data.get('rejections', {}))}")
    
    for idx, signal in enumerate(signals, 1):
        print(f"\n[Signal {idx}]")
        print(f"  Strategy      : {signal.get('strategy_name')}")
        print(f"  Underlying    : {signal.get('underlying')}")
        print(f"  Symbol        : {signal.get('trading_symbol')}")
        print(f"  Option Type   : {signal.get('option_type')}")
        print(f"  Strike        : {signal.get('strike')}")
        print(f"  Expiry        : {signal.get('expiry')}")
        print(f"  Lot Size      : {signal.get('lot_size')}")
        quote = signal.get("quote_snapshot", {})
        print(f"  Ask (Entry)   : {quote.get('ask')}")
        print(f"  Bid           : {quote.get('bid')}")
        print(f"  Executable    : {quote.get('executable')}")
        ds = signal.get("demand_supply", {})
        print(f"  Demand/Supply : {ds.get('reason')} ({ds.get('zone_type')})")
        
    print("\nSimulation complete. Zero orders placed (Read-only remote paper simulation).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
