"""Run direct standalone Options Intraday scan and management loop.
Bypasses equity scanners and builds/executes option plans directly in real-time.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    env_path = Path(r"C:\Users\Kapil Kuhire\Downloads\TRIO_IMPLEMENTATION\.env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, val = line.split("=", 1)
                os.environ[key.strip()] = val.strip().strip('"').strip("'")
                
    os.environ["TRIO_BROKER_PROVIDER"] = "megabull"
    os.environ["PAPER_TRADING"] = "true"
    os.environ["TRADING_MODE"] = "options_intraday"
    os.environ["TRIO_REMOTE_REQUIRED"] = "true"

    import yaml
    config_path = ROOT / "config" / "options_intraday.yaml"
    cfg = {}
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

    from src.broker.megabull import MegaBullBroker
    from src.broker.megabull_options import MegaBullOptionsBroker
    from src.options_intraday_runtime import OptionsIntradayRuntime

    mega = MegaBullBroker(api_key=os.environ.get("MEGABULL_API_KEY"))
    opt_broker = MegaBullOptionsBroker(mega)
    
    class OptionsOnlyTrader:
        def __init__(self, broker, opt_broker):
            self.broker = broker
            self.opt_broker = opt_broker
            self.trading_mode = "options_intraday"
            self.execution_class = "remote_paper"
            self.options_only = True
            
    trader = OptionsOnlyTrader(mega, opt_broker)
    runtime = OptionsIntradayRuntime(trader, cfg)
    
    print("============================================================")
    print("      STANDALONE OPTIONS-ONLY RUNTIME ACTIVE                ")
    print("============================================================")
    print("Mode: options_intraday (No equity scan, direct options plan)")
    print("Scanning NIFTY and BANKNIFTY live chain...")
    
    try:
        placed = runtime.scan_and_execute()
        print(f"Scan complete. Placed option orders: {len(placed)}")
        for order in placed:
            print(f"  -> Executed Option: {order.get('trading_symbol')} | Lots: {order.get('lots')}")
    except Exception as exc:
        print(f"Scan encountered safety guard or market condition: {type(exc).__name__}: {exc}")
        
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
