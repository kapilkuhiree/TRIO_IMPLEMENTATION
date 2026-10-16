"""Reproducible research manifest and hard profitability gate.

This module does not price options and does not substitute underlying or
Black-Scholes results for option P&L.  It prepares the comparison contract for
ORB, VWAP, pullback, momentum/volatility, and a documented hybrid once genuine
premium candles are archived.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence


STRATEGY_CANDIDATES = (
    "orb", "vwap", "pullback", "momentum_volatility", "hybrid",
)
NOT_READY = "NOT_READY_FOR_OPTIONS_PNL"


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _has(value: Any) -> bool:
    return value not in (None, "")


def assess_historical_option_rows(
    rows: Optional[Iterable[Mapping[str, Any]]],
    *,
    provenance: str = "",
) -> Dict[str, Any]:
    """Check the minimum evidence required before option P&L is calculated."""
    required_identity = (
        "timestamp", "underlying", "trading_symbol", "expiry",
        "strike", "option_type",
    )
    required_prices = ("open", "high", "low", "close", "bid", "ask")
    blockers = []
    checked = 0
    invalid = 0
    for row in rows or []:
        checked += 1
        if any(not _has(row.get(key)) for key in required_identity):
            invalid += 1
            continue
        if any(not _has(row.get(key)) for key in required_prices):
            invalid += 1
            continue
        try:
            prices = [float(row[key]) for key in required_prices]
            if any(value <= 0 for value in prices) or prices[5] <= prices[4]:
                invalid += 1
                continue
        except (TypeError, ValueError, OverflowError):
            invalid += 1
    if str(provenance).strip().lower() != "validated_historical":
        blockers.append("premium provenance is not validated_historical")
    if checked == 0:
        blockers.append("no historical option-premium rows supplied")
    if invalid:
        blockers.append(f"{invalid} option-premium rows failed identity/price validation")
    # Even a valid historical candle archive does not prove historical bid/ask
    # alignment unless every row carries both executable sides.
    ready = not blockers
    return {
        "options_pnl_status": "READY_FOR_OPTIONS_PNL" if ready else NOT_READY,
        "profitability_claim_allowed": ready,
        "rows_checked": checked,
        "invalid_rows": invalid,
        "claim_blockers": blockers,
        "synthetic_results_allowed": True,
        "synthetic_results_label": "SYNTHETIC_MODEL_ONLY",
    }


def comparison_spec() -> Dict[str, Any]:
    """Return the fixed, order-independent research comparison policy."""
    return {
        "strategies": list(STRATEGY_CANDIDATES),
        "signal_boundary": "completed underlying candle",
        "entry_policy": "next eligible option candle ask",
        "exit_policy": "actual option bid; stop before target; same-day hard-flat",
        "cost_policy": {
            "brokerage": "configured per order",
            "slippage": "configured adverse bid/ask slippage",
            "taxes": "configured statutory charges",
        },
        "contract_policy": "exact active contract identity as-of signal time",
        "lookahead_policy": "no future candles, quotes, or contract rows",
        "missing_data_policy": "reject trade and block profitability claim",
        "hybrid_policy": "must record all component decisions and be deterministic",
    }


def build_research_manifest(
    *,
    input_paths: Optional[Sequence[str | Path]] = None,
    parameters: Optional[Mapping[str, Any]] = None,
    provenance: str = "",
    data_coverage: Optional[Mapping[str, Any]] = None,
    rows: Optional[Iterable[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build a JSON-serializable research manifest with the hard data gate."""
    paths = [Path(item) for item in (input_paths or [])]
    evidence = assess_historical_option_rows(rows, provenance=provenance)
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": [
            {"path": str(path), "sha256": sha256_file(path)}
            for path in paths if path.exists()
        ],
        "parameters": dict(parameters or {}),
        "comparison": comparison_spec(),
        "provenance": provenance,
        "coverage": dict(data_coverage or {}),
        "evidence": evidence,
        "research_only": True,
    }


def comparison_plan() -> Dict[str, Any]:
    """Return candidates without implying that any has been backtested."""
    return {
        "research_only": True,
        "strategies": [
            {"id": strategy, "status": "awaiting_validated_option_candles"}
            for strategy in STRATEGY_CANDIDATES
        ],
        "options_pnl_status": NOT_READY,
        "profitability_claim_allowed": False,
        "claim_blockers": [
            "genuine historical option-premium candles with aligned bid/ask are required",
            "chronological out-of-sample execution has not run",
        ],
        "comparison": comparison_spec(),
    }


__all__ = [
    "NOT_READY", "STRATEGY_CANDIDATES", "assess_historical_option_rows",
    "build_research_manifest", "comparison_plan", "comparison_spec",
    "sha256_file",
]
