"""Run MegaBull paper session safely loading external credentials from .env."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.paper_trader import PaperTrader


def main() -> int:
    env_path = Path(r"C:\Users\Kapil Kuhire\Downloads\TRIO_IMPLEMENTATION\.env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, val = line.split("=", 1)
                os.environ[key.strip()] = val.strip().strip('"').strip("'")
                
    print("Environment loaded. MEGABULL_API_KEY present:", bool(os.environ.get("MEGABULL_API_KEY")))
    
    try:
        trader = PaperTrader()
        print("PaperTrader initialized successfully with MegaBull adapter.")
        print("Running connection / balance check...")
        if hasattr(trader, "broker") and hasattr(trader.broker, "get_balance"):
            print("Balance:", trader.broker.get_balance())
        print("MegaBull broker connection verified successfully.")
    except Exception as exc:
        print(f"MegaBull initialization failed: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
