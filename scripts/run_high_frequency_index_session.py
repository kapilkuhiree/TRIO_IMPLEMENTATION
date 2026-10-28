"""Continuous High-Frequency Index Options Local Runner
Scans NIFTY and BANKNIFTY every 10 seconds, evaluates breakout & momentum signals,
and executes live remote-paper orders on MegaBull with zero rate-limit errors.
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

    from src.options_intraday import load_options_intraday_config
    cfg = load_options_intraday_config()
    cfg["options_intraday"]["scan_underlyings"] = ["NIFTY", "BANKNIFTY"]
    cfg["options_intraday"].pop("scan_basket", None)
    # --- probability‑boosting risk controls (still within §3 limits) ---
    cfg["options_intraday"]["risk_per_trade_pct"] = 0.70          # 0.70 % of capital per trade
    cfg["options_intraday"]["max_open_positions"] = 3            # allow up to 3 simultaneous option legs
    cfg["options_intraday"]["max_trades_per_day"] = 4            # allow up to 4 approvals per day
    cfg["options_intraday"]["time_cutoff_ist"] = "15:15"         # keep the session open until 15:15 IST

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
    print("      HIGH-FREQUENCY 10-SECOND INDEX OPTIONS RUNNER         ")
    print("============================================================")
    print("Scanning NIFTY & BANKNIFTY continuously every 10 seconds...")
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

            # Sleep 10 seconds before next scan
            time.sleep(10)
            
    except KeyboardInterrupt:
        print("\nContinuous session stopped by user.")
        
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
