"""Run local options session with Zerodha data provider and Telegram alerts enabled."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


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

    # Force Zerodha market data provider and keep remote paper active
    os.environ["TRIO_MARKET_DATA_PROVIDER"] = "zerodha"
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

    from src.broker.megabull import MegaBullBroker, load_dotenv_key
    from src.broker.megabull_options import MegaBullOptionsBroker
    from src.options_intraday_runtime import OptionsIntradayRuntime

    key = os.environ.get("MEGABULL_API_KEY") or load_dotenv_key("MEGABULL_API_KEY")
    mega = MegaBullBroker(api_key=key)
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
    print("      STANDALONE OPTIONS RUNTIME (ZERODHA DATA + MEGABULL)   ")
    print("============================================================")
    print("Scanning NIFTY and BANKNIFTY live chain using Zerodha candles...")

    try:
        placed = runtime.scan_and_execute()
        print(f"Scan complete. Placed option orders: {len(placed)}")
        for order in placed:
            print(f"  -> Executed Option: {order.get('trading_symbol')} | Lots: {order.get('lots')}")
            
        # Send Telegram notification if tokens available
        try:
            from src.alerts import send_options_telegram
            send_options_telegram(f"TRIO Local Session executed. Placed option orders: {len(placed)}")
            print("Telegram update sent successfully.")
        except Exception as t_exc:
            print(f"Telegram alert skipped/failed: {t_exc}")
            
    except Exception as exc:
        print(f"Scan encountered safety guard or market condition: {type(exc).__name__}: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
