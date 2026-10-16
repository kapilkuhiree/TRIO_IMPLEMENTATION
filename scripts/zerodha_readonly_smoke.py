"""Run a credential-safe Zerodha read-only connectivity smoke test.

This script intentionally does not import or call any order-placement API.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.utils import load_env  # noqa: E402
from src.zerodha_market_data import read_only_smoke  # noqa: E402


def main() -> int:
    load_env()
    try:
        result = read_only_smoke()
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "orders_placed": 0,
        }, indent=2))
        return 1
    print(json.dumps({"ok": True, **result}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
