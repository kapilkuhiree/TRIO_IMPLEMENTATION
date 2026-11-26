"""TRIO — Opening Range Breakout with SMT structure confirmation.

A thin, additive subclass of :class:`strategies.orb_strategy.OrbStrategy`.

The breakout logic is *identical* to plain ORB: the parent decides whether a
confirmed close has cleared the 09:15-09:30 opening range.  This class only
adds a second, independent gate on top of an already-valid ORB signal:

    SMT structure = the last N confirmed swing highs are ascending AND the
    last N confirmed swing lows are ascending (bullish), or the mirror image
    (bearish), measured over a bounded lookback window.

Why a subclass and not a copy: the breakout geometry, opening-range
construction, confirmation-count handling and stop/target derivation are all
already validated and covered by tests.  Duplicating them here would create a
second, drifting source of truth for the same rules.

Design constraints:

* The gate is **fail-closed**.  Too few bars, too few swings, or a non-finite
  price all reject the signal rather than silently passing it.  This matches
  ``_vwap_allows`` in ``src/options_live_scan.py``, which returns
  ``vwap_unavailable`` rather than allowing a trade on missing data.
* The gate never *creates* a signal.  It can only reject one the parent
  produced, so this sleeve is strictly a subset of plain ORB.
* Defaults are deliberately loose enough to be measurable: N=2 swings over a
  20-bar window is reachable by the 10:00 IST ORB entry, whereas a strict
  "every bar makes a higher high and higher low" rule fires almost never on
  5-minute index candles.

DISCLAIMER: Educational purposes only. Not financial advice.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from strategies.orb_strategy import OrSignalResult, OrbStrategy

# Defaults.  Kept module-level so tests and calibration tooling can import the
# exact values the strategy uses instead of restating them.
SMT_LOOKBACK_BARS = 20
SMT_SWINGS = 2
SMT_MIN_SCORE = 2


def smt_pivots(values: List[float], kind: str) -> List[int]:
    """Indices of confirmed swing highs (``kind="high"``) or lows.

    Retained as a diagnostic only.  It is deliberately *not* the gate: a
    strictly monotonic rally has no interior pivot at all (every bar is higher
    than the one before, so no bar is a local maximum), which would make the
    gate reject the strongest trends.  :func:`smt_structure` uses contiguous
    blocks instead.
    """
    out: List[int] = []
    for i in range(1, len(values) - 1):
        if kind == "high":
            if values[i] >= values[i - 1] and values[i] >= values[i + 1]:
                out.append(i)
        else:
            if values[i] <= values[i - 1] and values[i] <= values[i + 1]:
                out.append(i)
    return out


def smt_blocks(
    candles: List[Dict[str, Any]], swings: int
) -> List[Tuple[float, float]]:
    """Split ``candles`` into ``swings`` contiguous blocks of (high, low).

    Blocks are the right primitive here because "higher highs and higher lows"
    is a statement about successive *ranges*, not about local extremes.  A
    clean rally produces strictly ascending block highs and lows, while a
    choppy market produces overlapping blocks -- which is exactly the
    distinction the gate is meant to draw.
    """
    count = max(1, int(swings))
    size = max(1, len(candles) // count)
    blocks: List[Tuple[float, float]] = []
    for i in range(count):
        chunk = candles[i * size:(i + 1) * size]
        if not chunk:
            continue
        highs = [float(row.get("high")) for row in chunk]
        lows = [float(row.get("low")) for row in chunk]
        blocks.append((max(highs), min(lows)))
    return blocks


def smt_structure(
    candles: List[Dict[str, Any]],
    direction: str,
    *,
    lookback: int = SMT_LOOKBACK_BARS,
    swings: int = SMT_SWINGS,
    min_score: int = SMT_MIN_SCORE,
) -> Tuple[bool, str, Dict[str, Any]]:
    """Return ``(allowed, reason, detail)`` for the SMT structure gate.

    Bullish structure: the block highs are strictly ascending AND the block
    lows are strictly ascending over the lookback window.  Bearish is the
    mirror image.  ``min_score`` is how many of the two legs must hold, so 2
    demands a clean trend and 1 tolerates a flat high or a flat low.

    ``detail`` is always populated so callers can log why a signal was
    rejected without re-deriving anything.
    """
    detail: Dict[str, Any] = {
        "lookback": lookback,
        "swings": swings,
        "min_score": min_score,
        "bars": 0,
        "high_pivots": 0,
        "low_pivots": 0,
        "blocks": [],
        "highs_ascending": False,
        "lows_ascending": False,
        "score": 0,
        "direction": str(direction or "").strip().lower(),
        "structure": "unknown",
    }

    if not candles:
        return False, "smt_no_candles", detail

    window = candles[-max(1, int(lookback)):]
    detail["bars"] = len(window)
    if len(window) < max(2, int(swings) + 1):
        return False, "smt_insufficient_bars", detail

    try:
        highs = [float(row.get("high")) for row in window]
        lows = [float(row.get("low")) for row in window]
        blocks = smt_blocks(window, swings)
    except (TypeError, ValueError):
        return False, "smt_non_finite_bar", detail
    if any(value <= 0 for value in highs) or any(value <= 0 for value in lows):
        return False, "smt_non_finite_bar", detail
    if len(blocks) < 2:
        return False, "smt_insufficient_bars", detail

    detail["blocks"] = [[round(high, 2), round(low, 2)] for high, low in blocks]
    detail["high_pivots"] = len(smt_pivots(highs, "high"))
    detail["low_pivots"] = len(smt_pivots(lows, "low"))

    direction = detail["direction"]
    if direction in {"bullish", "long"}:
        rising = True
    elif direction in {"bearish", "short"}:
        rising = False
    else:
        return False, "smt_unknown_direction", detail

    block_highs = [high for high, _ in blocks]
    block_lows = [low for _, low in blocks]

    def _ascending(values: List[float]) -> bool:
        pairs = list(zip(values, values[1:]))
        if not pairs:
            return False
        return all((b > a) if rising else (b < a) for a, b in pairs)

    highs_ascending = _ascending(block_highs)
    lows_ascending = _ascending(block_lows)
    detail["highs_ascending"] = highs_ascending
    detail["lows_ascending"] = lows_ascending

    score = int(highs_ascending) + int(lows_ascending)
    detail["score"] = score
    if score >= min_score:
        detail["structure"] = "bullish" if rising else "bearish"
    else:
        detail["structure"] = "mixed"

    if score >= min_score:
        return True, "", detail
    return False, f"smt_structure_against_trend(score={score}<{min_score})", detail


@dataclass
class SmtSignalResult(OrSignalResult):
    """An ORB signal plus the SMT verdict that gated it."""

    smt: Optional[Dict[str, Any]] = None


class OrbSmtStrategy(OrbStrategy):
    """ORB breakout that additionally requires SMT structure alignment.

    Constructor signature matches :class:`OrbStrategy` exactly so the shared
    dispatch in ``candidate_pipeline.evaluate_strategy_signals_pure`` can build
    either class through the same branch.
    """

    strategy_name = "orb_smt"

    def __init__(self, lot_size: int = 65, fill_model: str = "bidask",
                 orb_confirm_closes: int = 1,
                 smt_lookback_bars: int = SMT_LOOKBACK_BARS,
                 smt_swings: int = SMT_SWINGS,
                 smt_min_score: int = SMT_MIN_SCORE):
        super().__init__(lot_size=lot_size, fill_model=fill_model,
                         orb_confirm_closes=orb_confirm_closes)
        self.smt_lookback_bars = max(1, int(smt_lookback_bars or SMT_LOOKBACK_BARS))
        self.smt_swings = max(1, int(smt_swings or SMT_SWINGS))
        self.smt_min_score = max(1, int(smt_min_score or SMT_MIN_SCORE))

    def evaluate_signal(
        self,
        candles_5min: List[Dict[str, Any]],
        index: int,
        contract_specs: Dict[str, Any],
        underlying_symbol: str = "NIFTY",
    ) -> SmtSignalResult:
        """Return the parent ORB signal, rejected unless SMT structure agrees."""
        base = super().evaluate_signal(
            candles_5min, index, contract_specs,
            underlying_symbol=underlying_symbol)

        result = SmtSignalResult(**base.__dict__)
        result.strategy = self.strategy_name

        # No breakout, nothing to confirm.  The gate can only remove signals.
        if not result.allowed:
            return result

        # Judge structure on completed bars up to and including the signal bar,
        # never on candles after it.
        history = candles_5min[:index + 1]
        allowed, reason, detail = smt_structure(
            history,
            result.variant,
            lookback=self.smt_lookback_bars,
            swings=self.smt_swings,
            min_score=self.smt_min_score,
        )
        detail["reason"] = reason
        detail["allowed"] = allowed
        result.smt = detail

        if not allowed:
            result.allowed = False
            result.signal_type = "none"
            base_reasoning = str(result.reasoning or "").strip()
            result.reasoning = (
                f"{base_reasoning} | rejected: {reason}" if base_reasoning else reason
            )
        return result


__all__ = [
    "OrbSmtStrategy",
    "SmtSignalResult",
    "smt_blocks",
    "smt_pivots",
    "smt_structure",
    "SMT_LOOKBACK_BARS",
    "SMT_SWINGS",
    "SMT_MIN_SCORE",
]
