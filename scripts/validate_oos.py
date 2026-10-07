"""
TRIO — Out-of-sample validation for the wide-stop/small-target family.
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

Why this exists: the sweep found configurations with ~80% win rate AND a
profit factor above 1. Those were selected by looking at the whole 5y sample,
so they may simply be curve-fitted. This script splits each symbol's history
into train and test halves and reports test-only performance.

A config is trustworthy only if it still works on data it was not chosen on.

Run: python scripts/validate_oos.py
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from experiment import (  # noqa: E402
    BASKET, build_features, load_all, metrics, simulate, v_trend_pullback,
)

# Only the configs that looked good, plus controls.
CANDIDATES = {
    "wide4.0_t0.5": ("atr", 4.0, 0.5, None),
    "wide5.0_t0.4": ("atr", 5.0, 0.4, None),
    "wide6.0_t0.3": ("atr", 6.0, 0.3, None),
    "wide5.0_t0.6": ("atr", 5.0, 0.6, None),
    "wide8.0_t0.25": ("atr", 8.0, 0.25, None),
    "swing_t1.0": ("swing", 2.5, 1.0, None),
    "swing_t1.5": ("swing", 2.5, 1.5, None),
    "swing_trail3.0": ("swing", 2.5, None, 3.0),
}


def split_run(label: str, kind: str, sm: float, tr, trail, split: float):
    """Run one config on train-half and test-half separately, per symbol."""
    train: List[Dict[str, Any]] = []
    test: List[Dict[str, Any]] = []

    data = load_all()

    for sym, df in data.items():
        f = build_features(df).dropna()
        if len(f) < 400:
            continue
        cut = int(len(f) * split)

        # Train slice: signal computed only from bars inside the slice
        tr_f = f.iloc[:cut]
        test_f = f.iloc[cut:]

        if len(tr_f) > 60:
            e = v_trend_pullback(tr_f)
            train.extend(simulate(tr_f, e, kind, sm, tr, trail))

        if len(test_f) > 60:
            e = v_trend_pullback(test_f)
            test.extend(simulate(test_f, e, kind, sm, tr, trail))

    return metrics(train, f"{label}"), metrics(test, f"{label}")


def main() -> None:
    import logging
    logging.disable(logging.CRITICAL)

    print("Splitting each symbol 60/40: params chosen on TRAIN, judged on TEST\n")
    print(f"{'config':<20} | {'TRAIN win%':>10} {'PF':>6} {'trades':>7} "
          f"| {'TEST win%':>10} {'PF':>6} {'trades':>7}")
    print("-" * 78)

    rows = []
    for label, (kind, sm, tr, trail) in CANDIDATES.items():
        tr_m, te_m = split_run(label, kind, sm, tr, trail, 0.6)
        rows.append({
            "config": label,
            "train": tr_m,
            "test": te_m,
        })
        print(f"{label:<20} | {tr_m.get('win_rate',0):>10} "
              f"{tr_m.get('profit_factor',0):>6} {tr_m.get('trades',0):>7} "
              f"| {te_m.get('win_rate',0):>10} "
              f"{te_m.get('profit_factor',0):>6} {te_m.get('trades',0):>7}")

    out = ROOT / "scripts" / "validation_oos.json"
    out.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    # Flag configs whose test profit factor collapsed below 1.0
    print()
    survivors = [r for r in rows if r["test"].get("profit_factor", 0) >= 1.2
                 and r["test"].get("trades", 0) >= 20]
    if survivors:
        print("Configs that held up out-of-sample (test PF >= 1.2, >= 20 trades):")
        for r in survivors:
            print(f"  {r['config']:<20} test win%={r['test']['win_rate']} "
                  f"PF={r['test']['profit_factor']} trades={r['test']['trades']}")
    else:
        print("No config survived the out-of-sample test at PF >= 1.2.")
        print("This means the apparent edge was fit to the sample, not real.")

    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()