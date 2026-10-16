"""Research-only options strategy comparison gate.

This command intentionally does not run the old Black-Scholes model harness.
Without validated historical option-premium rows it emits a reproducible
comparison plan and ``NOT_READY_FOR_OPTIONS_PNL`` instead of fake metrics.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.options_research import (  # noqa: E402
    build_research_manifest,
    comparison_plan,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="TRIO options research gate")
    parser.add_argument("--premium-json", type=Path,
                        help="Validated historical option rows as a JSON list")
    parser.add_argument("--provenance", default="",
                        help="Must be validated_historical to open the P&L gate")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    rows = None
    inputs = []
    if args.premium_json:
        rows = json.loads(args.premium_json.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise SystemExit("--premium-json must contain a JSON list")
        inputs.append(args.premium_json)
    if rows is None:
        result = comparison_plan()
    else:
        result = build_research_manifest(
            input_paths=inputs, provenance=args.provenance, rows=rows)
    result["orders_placed"] = 0
    payload = json.dumps(result, indent=2, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
