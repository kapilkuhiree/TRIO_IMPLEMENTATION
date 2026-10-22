"""Run production paper loop with pre-loaded environment."""

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
                
    # Now invoke main.py paper
    sys.argv = ["main.py", "paper"]
    import main as trio_main
    trio_main.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
