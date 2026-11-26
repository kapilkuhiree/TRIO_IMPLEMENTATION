"""Spread-id allocation shared by the option broker adapters.

Every adapter used to refuse a second position on the same contract
(``already holding this spread``).  That made the four-sleeve book impossible:
``orb`` and ``orb_smt`` usually want the *same* strike, so the second sleeve
could never trade.  The operator's rule is that a sleeve that generates its
own signal may take the same contract (2026-11-24), so a collision now gets a
numeric suffix (``...PE``, ``...PE#2``, ...) instead of a rejection.

The suffix stays inside the adapter's own book: ``close_spread`` is keyed by
spread id, and the remote leg carries its own quantity, so each sleeve closes
only its own lots.  The runtime's ``max_positions_per_contract`` is the
business limit; ``limit`` here is only a hard safety net.
"""

from __future__ import annotations

from typing import Container, Optional


def stacked_spread_id(base: str, taken: Container[str],
                      limit: int = 4) -> Optional[str]:
    """Return a free spread id for ``base``, or ``None`` when exhausted.

    ``base`` itself is preferred so single-sleeve behaviour (and every
    existing log line and test) is unchanged.
    """
    base = str(base or "")
    if not base:
        return None
    if base not in taken:
        return base
    for index in range(2, max(2, int(limit)) + 1):
        candidate = f"{base}#{index}"
        if candidate not in taken:
            return candidate
    return None
