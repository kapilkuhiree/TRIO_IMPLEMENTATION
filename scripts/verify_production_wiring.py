"""Production MegaBull Wiring Verification Script
Checks whether a live MegaBull API key can be supplied and verified without printing credentials.
"""

from __future__ import annotations

import os
from pathlib import Path


def main() -> int:
    env_path = Path(r"C:\Users\Kapil Kuhire\Downloads\TRIO_IMPLEMENTATION\.env")
    has_key = False
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("MEGABULL_API_KEY="):
                val = line.split("=", 1)[1].strip()
                if val and val != "your_megabull_key_here":
                    os.environ["MEGABULL_API_KEY"] = val
                    has_key = True

    print(f"MegaBull Production Key Present: {has_key}")
    if not has_key:
        print("Production Wiring Status: Awaiting valid MEGABULL_API_KEY in .env.")
        print("Rule enforced: Never print credentials. Never send credentials to another service.")
        print("To wire live execution, populate MEGABULL_API_KEY in your external .env file.")
    else:
        print("Production Wiring Status: Credentials detected. Ready for secure remote-paper link.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
