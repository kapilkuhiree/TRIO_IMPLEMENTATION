"""Run local paper session with explicit dotenv loading."""

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

    os.environ["TRIO_BROKER_PROVIDER"] = "megabull"
    os.environ["PAPER_TRADING"] = "true"
    os.environ["TRADING_MODE"] = "options_intraday"
    os.environ["TRIO_REMOTE_REQUIRED"] = "true"

    sys.argv = ["session.py", "--trading-mode", "options_intraday", "--interval", "60", "--max-seconds", "120"]
    import scripts.session as session_script
    session_script.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
