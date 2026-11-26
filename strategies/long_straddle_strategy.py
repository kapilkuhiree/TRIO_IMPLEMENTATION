"""TRIO — Long Straddle Strategy Module

Booklet page 18: buy ATM call and ATM put (bias ``ANY``).
Direction-neutral long-volatility structure triggered by an allowed causal
ORB / pullback / momentum signal on a completed 5-minute candle.

Premiums are never fabricated.  Pass quotes in ``contract_specs['option_quotes']``
to make the emitted structure executable; otherwise the research signal is
still produced with ``execution_ready=False``.

KEEP IN RESEARCH MODE ONLY — no modelled option P&L is computed here.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

# Allow direct execution as well as package import.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategies.long_volatility_base import (  # noqa: E402
    LongVolatilitySignalResult,
    LongVolatilityStructure,
    StructureLeg,
    run_structure_backtest,
)

STRATEGY_NAME = "LONG_STRADDLE"
STRATEGY_KEY = "long_straddle"


class LongStraddleStrategy(LongVolatilityStructure):
    """Buy ATM CE + ATM PE; profit from a large move in either direction."""

    NAME = STRATEGY_NAME
    KEY = STRATEGY_KEY
    PAGE = "p18"
    BIAS = "ANY"
    DESCRIPTION = "Buy ATM call and ATM put; long volatility."
    LEGS = (
        StructureLeg(kind="CE", side="BUY", offset=0),
        StructureLeg(kind="PE", side="BUY", offset=0),
    )


def run_long_straddle_backtest(
    csv_path: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    lot_size: int = 65,
    initial_equity: float = 500000.0,
    contract_specs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Collect LONG_STRADDLE signals; option P&L remains NOT_READY."""
    return run_structure_backtest(
        LongStraddleStrategy,
        csv_path,
        start_date=start_date,
        end_date=end_date,
        lot_size=lot_size,
        initial_equity=initial_equity,
        contract_specs=contract_specs,
    )


if __name__ == "__main__":
    print(
        f"{STRATEGY_NAME} (page {LongStraddleStrategy.PAGE}): "
        f"{LongStraddleStrategy.DESCRIPTION}\n"
        "Trigger: first allowed ORB / pullback / momentum signal on a "
        "completed NSE 5-minute candle.\n"
        "Usage: evaluate_signal(candles, index, contract_specs, "
        'underlying_symbol="NIFTY") -> LongVolatilitySignalResult'
    )
