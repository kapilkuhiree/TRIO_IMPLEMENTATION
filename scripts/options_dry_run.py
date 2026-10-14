"""Research-only options candidate diagnostic.

This command never imports PaperTrader, a broker adapter, or an order path.
It either builds a fresh premarket plan (network/data fetch only) or replays
an archived plan and reports the preserved option-selection stage.

Examples:
    python scripts/options_dry_run.py
    python scripts/options_dry_run.py --plan output/plans/2026-10-13.json
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.options_chain import expiry_status  # noqa: E402
from option_data_schema import NOT_READY_FOR_OPTIONS_PNL  # noqa: E402


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _archived_candidate_diagnostic(candidate: Dict[str, Any],
                                   chain: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    opt = candidate.get("option_legs") or {}
    expiry = str(opt.get("expiry") or "")
    status = expiry_status(chain, expiry) if chain is not None else {
        "expiry": expiry,
        "listed_by_exchange": None,
        "strike_rows": None,
        "ce_records": None,
        "pe_records": None,
        "usable": False,
        "reason": "chain_artifact_unavailable",
    }
    strategy = opt.get("strategy")
    selected = strategy not in (None, "", "none") and bool(opt.get("legs"))
    reasons = list(opt.get("reasoning") or [])
    normalized_reason = None
    if any("no strikes for expiry" in str(reason).lower() for reason in reasons):
        normalized_reason = "zero_strikes_for_expiry"
    return {
        "symbol": candidate.get("symbol", ""),
        "signal_underlying": candidate.get("symbol", ""),
        "option_underlying": opt.get("underlying", "NIFTY"),
        "underlying_mapping": "configured_index_overlay",
        "action": candidate.get("action", "HOLD"),
        "strategy": candidate.get("setup_name", "unattributed"),
        "requested_expiry": expiry,
        "expiry_status": status,
        "contract_selection": "selected" if selected else "rejected",
        "risk_approval": "not_reached" if not selected else "recorded_in_plan",
        "rejection_stage": None if selected else "contract_selection",
        "rejection_reasons": reasons,
        "normalized_rejection_reason": normalized_reason,
        "simulated_execution": False,
    }


def build_diagnostic(plan: Dict[str, Any],
                     *,
                     source: str,
                     chain: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    candidates = plan.get("option_candidates") or []
    rows = []
    for candidate in candidates:
        if candidate.get("option_diagnostic"):
            diag = dict(candidate["option_diagnostic"])
            option = candidate.get("option_legs") or {}
            rows.append({
                "symbol": candidate.get("symbol", ""),
                "signal_underlying": diag.get("signal_underlying", candidate.get("symbol", "")),
                "option_underlying": diag.get("option_underlying", option.get("underlying", "NIFTY")),
                "underlying_mapping": diag.get("underlying_mapping", "configured_index_overlay"),
                "action": candidate.get("action", "HOLD"),
                "strategy": candidate.get("setup_name", "unattributed"),
                "requested_expiry": diag.get("requested_expiry", option.get("expiry", "")),
                "expiry_status": diag.get("expiry_status", {}),
                "contract_selection": "selected" if option.get("strategy") not in (None, "", "none") else "rejected",
                "risk_approval": "recorded_in_plan" if option.get("strategy") not in (None, "", "none") else "not_reached",
                "rejection_stage": diag.get("selection_stage"),
                "rejection_reasons": diag.get("rejection_reasons", option.get("reasoning", [])),
                "normalized_rejection_reason": (
                    "zero_strikes_for_expiry"
                    if any("no strikes for expiry" in str(reason).lower()
                           for reason in diag.get("rejection_reasons", option.get("reasoning", [])))
                    else None),
                "simulated_execution": False,
            })
        else:
            rows.append(_archived_candidate_diagnostic(candidate, chain))

    counts = Counter()
    for row in rows:
        counts["options_candidates"] += 1
        if row["contract_selection"] == "selected":
            counts["eligible_contracts"] += 1
        else:
            counts["contract_rejections"] += 1
            for reason in row.get("rejection_reasons") or ["unspecified"]:
                if "expiry" in str(reason).lower() or "strike" in str(reason).lower():
                    counts["expiry_or_strike_rejections"] += 1

    return {
        "date": plan.get("date"),
        "generated_at": plan.get("generated_at"),
        "source": source,
        "research_only": True,
        "broker_orders_submitted": False,
        "historical_options_pnl": NOT_READY_FOR_OPTIONS_PNL,
        "signals_generated": len(plan.get("candidates") or []),
        "counts": dict(counts),
        "candidates": rows,
        "option_chain_diagnostic": plan.get("option_chain_diagnostic") or {},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="TRIO options dry-run diagnostic")
    parser.add_argument("--plan", type=Path,
                        help="Replay an archived plan without network or broker access")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "output" / "runtime" / "options_dry_run.json")
    args = parser.parse_args()

    if args.plan:
        plan = _load_json(args.plan)
        chain = None
        chain_path = ROOT / "output" / "chain_cache" / "chain_NIFTY.json"
        if chain_path.exists():
            try:
                chain = _load_json(chain_path).get("chain")
            except (OSError, json.JSONDecodeError):
                chain = None
        report = build_diagnostic(plan, source=str(args.plan), chain=chain)
    else:
        from scripts.premarket import build_plan
        from src.utils import load_config
        cfg = load_config()
        universe = cfg.get("universe", {}).get("premarket", "nifty100")
        top_n = int(cfg.get("screener", {}).get("max_positions_to_open", 3))
        plan = build_plan(universe, "1d", top_n, with_options=True)
        report = build_diagnostic(plan, source="fresh_dry_run")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({
        "date": report.get("date"),
        "source": report.get("source"),
        "counts": report.get("counts"),
        "historical_options_pnl": report.get("historical_options_pnl"),
        "output": str(args.output),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
