"""Dedicated Nifty and BankNifty Options-Only Local Runner (Robust & Rate-Limit Safe)
Scans exclusively NIFTY and BANKNIFTY using yfinance for fast, un-rate-limited candle data,
fetches live option chains from Zerodha/NSE, and executes remote-paper orders on MegaBull.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

IST = timezone(timedelta(hours=5, minutes=30))


def main() -> int:
    env_path = Path(r"C:\Users\Kapil Kuhire\Downloads\TRIO_IMPLEMENTATION\.env")
    if env_path.exists():
        from dotenv import load_dotenv
        load_dotenv(env_path)

    if not os.environ.get("MEGABULL_API_KEY"):
        from src.broker.megabull import load_dotenv_key
        key = load_dotenv_key("MEGABULL_API_KEY")
        if key:
            os.environ["MEGABULL_API_KEY"] = key

    os.environ["TRIO_BROKER_PROVIDER"] = "megabull"
    os.environ["PAPER_TRADING"] = "true"
    os.environ["TRADING_MODE"] = "options_intraday"
    os.environ["TRIO_REMOTE_REQUIRED"] = "true"
    os.environ["TRIO_MARKET_DATA_PROVIDER"] = "yfinance"

    import yaml
    config_path = ROOT / "config" / "options_intraday.yaml"
    cfg = {}
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

    cfg.setdefault("options_intraday", {})
    cfg["options_intraday"]["scan_basket"] = "indices"
    cfg["options_intraday"]["scan_underlyings"] = ["NIFTY", "BANKNIFTY"]

    from src.broker.megabull import MegaBullBroker, load_dotenv_key
    from src.broker.megabull_options import MegaBullOptionsBroker
    from src.options_intraday_runtime import OptionsIntradayRuntime

    key = os.environ.get("MEGABULL_API_KEY") or load_dotenv_key("MEGABULL_API_KEY")
    mega = MegaBullBroker(api_key=key)
    opt_broker = MegaBullOptionsBroker(mega)

    class IndicesOnlyTrader:
        def __init__(self, broker, opt_broker):
            self.broker = broker
            self.opt_broker = opt_broker
            self.trading_mode = "options_intraday"
            self.execution_class = "remote_paper"
            self.options_only = True

    trader = IndicesOnlyTrader(mega, opt_broker)
    runtime = OptionsIntradayRuntime(trader, cfg)

    print("============================================================")
    print("      NIFTY & BANKNIFTY OPTIONS-ONLY LOCAL RUNNER           ")
    print("============================================================")
    print("Scanning ONLY NIFTY and BANKNIFTY continuously...")
    print("Press Ctrl+C to stop.\n")

    scan_count = 0
    try:
        while True:
            scan_count += 1
            now_ist = datetime.now(IST)
            print(f"[{now_ist.strftime('%Y-%m-%d %H:%M:%S IST')}] --- Scan #{scan_count} ---")
            
            try:
                placed = runtime.scan_and_execute()
                print(f"  -> Placed option orders this scan: {len(placed)}")
                for order in placed:
                    print(f"     *** EXECUTED OPTION ORDER: {order.get('trading_symbol')} | Lots: {order.get('lots')} ***")
                    try:
                        from src.alerts import send_options_telegram
                        send_options_telegram(f"TRIO Index Trade Executed: {order.get('trading_symbol')} | Lots: {order.get('lots')}")
                    except Exception:
                        pass
            except Exception as scan_exc:
                print(f"  -> Scan iteration warning: {scan_exc}")

            time.sleep(10)
            
    except KeyboardInterrupt:
        print("\nContinuous session stopped by user.")
        
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
