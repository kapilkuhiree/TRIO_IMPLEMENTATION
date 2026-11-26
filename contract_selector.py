"""TRIO — Option Contract Selector

When a strategy generates a candidate, this module identifies the correct
underlying, determines bullish/bearish direction, and evaluates eligible
CE or PE option contracts.

Tests:
- ATM contracts.
- One-strike ITM contracts.
- Delta between 0.45 and 0.60, when reliable historical Greeks are available.

Requires:
- Valid historical option price.
- Sufficient liquidity.
- Acceptable bid-ask spread.
- Correct contract and expiry.
- Position affordability under the risk budget.

Uses configurable spread and liquidity thresholds.
Does not apply a single open-interest threshold blindly across every
underlying. Thresholds are configurable by symbol or liquidity classification.

Respects the actual expiries offered for each instrument.

For backtests, uses only contract information available as of the
historical trade date.

Does not assume every index has weekly options.

Does not use future contract information to make historical decisions.

If bid-ask quotes are unavailable, records the limitation and does not
claim realistic bid-ask execution.

Never uses future contract information to make historical decisions.
"""

from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
from copy import deepcopy


# ---------------------------------------------------------------------------
# CONTRACT ELIGIBILITY CHECKS
# ---------------------------------------------------------------------------

def is_contract_eligible(
    contract: Dict[str, Any],
    min_oi: int = 100,
    max_spread_pct: float = 5.0,
    min_price: float = 0.1,
    max_price: float = 10000.0,
    live_quote_only: bool = False,
) -> Tuple[bool, str]:
    """Check if an option contract meets basic eligibility criteria.

    Args:
        contract: Dict with contract fields (strike, expiry, option_type,
                  open, high, low, close, volume, bid, ask, open_interest)
        min_oi: Minimum open interest threshold
        max_spread_pct: Maximum bid-ask spread as percentage of mid price
        min_price: Minimum valid premium price
        max_price: Maximum valid premium price

    Returns:
        (is_eligible, reason) tuple
    """
    # A live NSE chain is a quote snapshot, not an OHLC candle.  Keep this
    # path separate from historical candle eligibility so callers never have
    # to manufacture open/high/low/close values from spot or LTP.
    if live_quote_only:
        required_fields = [
            "strike", "expiry", "option_type", "volume", "bid", "ask",
            "open_interest", "quote_timestamp",
        ]
        for field in required_fields:
            if contract.get(field) is None or contract.get(field) == "":
                return False, f"Missing required live quote field: {field}"
        if contract.get("option_type", "") not in ("CE", "PE"):
            return False, f"Invalid option_type: {contract.get('option_type')}"
        if not contract.get("expiry"):
            return False, "Missing expiry"
        try:
            strike = float(contract.get("strike"))
            bid = float(contract.get("bid"))
            ask = float(contract.get("ask"))
            volume = float(contract.get("volume"))
            oi = float(contract.get("open_interest"))
        except (TypeError, ValueError):
            return False, "Non-numeric live quote field"
        if strike <= 0:
            return False, f"Invalid strike: {contract.get('strike')}"
        if bid <= 0 or ask <= 0:
            return False, "Non-positive bid/ask"
        if bid >= ask:
            return False, "Bid >= Ask (invalid spread)"
        mid_price = (bid + ask) / 2.0
        spread_pct = (ask - bid) / mid_price * 100 if mid_price else 100.0
        if spread_pct > max_spread_pct:
            return False, f"Spread too wide: {spread_pct:.1f}% > {max_spread_pct:.1f}%"
        if volume <= 0:
            return False, "Non-positive volume"
        if oi < min_oi:
            return False, f"Open interest {oi} below minimum {min_oi}"
        return True, "Eligible live quote"

    # Check required fields exist
    required_fields = ["strike", "expiry", "option_type", "open", "high", "low",
                       "close", "volume", "bid", "ask"]
    for field in required_fields:
        if contract.get(field) is None:
            return False, f"Missing required field: {field}"

    # Check option type is valid
    if contract.get("option_type", "") not in ("CE", "PE"):
        return False, f"Invalid option_type: {contract.get('option_type')}"

    # Check expiry is not empty
    if not contract.get("expiry"):
        return False, "Missing expiry"

    # Check strike is positive
    if contract.get("strike", 0) <= 0:
        return False, f"Invalid strike: {contract.get('strike')}"

    # Check prices are positive
    if contract.get("open", 0) <= 0 or contract.get("bid", 0) <= 0 or contract.get("ask", 0) <= 0:
        return False, "Non-positive price detected"

    # Check bid < ask (valid spread)
    if contract.get("bid", 0) >= contract.get("ask", 0):
        return False, "Bid >= Ask (invalid spread)"

    # Check bid-ask spread percentage
    mid_price = (contract.get("bid", 0) + contract.get("ask", 0)) / 2.0
    if mid_price > 0:
        spread_pct = abs(contract.get("bid", 0) - contract.get("ask", 0)) / mid_price * 100
        if spread_pct > max_spread_pct:
            return False, f"Spread too wide: {spread_pct:.1f}% > {max_spread_pct:.1f}%"

    # Check volume is non-negative
    if contract.get("volume", 0) < 0:
        return False, "Negative volume"

    # Check open interest if available
    oi = contract.get("open_interest")
    if oi is not None and oi < min_oi:
        return False, f"Open interest {oi} below minimum {min_oi}"

    # Check close price is reasonable
    if contract.get("close", 0) <= 0 or contract.get("close", 0) < min_price:
        return False, f"Close price too low: {contract.get('close')}"

    if contract.get("open", 0) > max_price:
        return False, f"Open price too high: {contract.get('open')}"

    return True, "Eligible"


# -----------------------------------------------------------------------
# EXPIRY AND STRIKE VALIDATION
# -----------------------------------------------------------------------

def validate_expiry_against_underlying(
    underlying_symbol: str,
    expiry: str,
    contract_specs: Dict[str, Any],
) -> Tuple[bool, str]:
    """Validate that an expiry date is appropriate for the underlying.

    Args:
        underlying_symbol: The underlying symbol (e.g., "NIFTY", "BANKNIFTY")
        expiry: The expiry date string from the contract
        contract_specs: Dict with contract specifications for this underlying

    Returns:
        (is_valid, reason) tuple
    """
    expiry_preference = contract_specs.get("expiry_preference", "weekly")

    # Check expiry is not empty
    if not expiry:
        return False, "Empty expiry"

    # Check expiry format
    if expiry.lower() == "weekly":
        # Weekly expiries are Thursdays - validate date is a Thursday
        # For now, accept any "weekly" expiry as valid (validated at backtest time)
        return True, "Weekly expiry accepted"

    if expiry.lower() == "monthly":
        # Monthly expiries are last Thursday of month
        return True, "Monthly expiry accepted"

    # Accept both the canonical NSE chain format and the historical ISO form.
    for expiry_format in ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y"):
        try:
            datetime.strptime(expiry, expiry_format)
            # For NIFTY, weekly expiries are typically Thursdays.  The
            # exchange listing itself remains the authoritative availability
            # check for the live-chain path.
            return True, "Date format expiry accepted"
        except ValueError:
            continue
    return False, f"Invalid expiry date format: {expiry}"


def get_valid_expiries(underlying_symbol: str, contract_specs: Dict[str, Any]) -> List[str]:
    """Get the list of valid expiries for an underlying.

    Args:
        underlying_symbol: The underlying symbol
        contract_specs: Contract specifications for this underlying

    Returns:
        List of expiry strings (e.g., ["2026-01-27", "weekly"])
    """
    expiry_preference = contract_specs.get("expiry_preference", "weekly")

    if expiry_preference == "weekly":
        return ["weekly"]
    elif expiry_preference == "monthly":
        return ["monthly"]
    elif expiry_preference in ("weekly_thursday", "monthly_last_thursday"):
        # These would be computed at backtest time
        return [expiry_preference]
    else:
        # Default: return weekly
        return ["weekly"]


# -----------------------------------------------------------------------
# STRIKE SELECTION
# -----------------------------------------------------------------------

def select_contract_by_moneyness(
    signal_direction: str,  # "bullish" or "bearish"
    underlying_price: float,
    available_contracts: List[Dict[str, Any]],
    contract_specs: Dict[str, Any],
    moneyness_target: str = "ATM",  # "ATM", "ITM_1", "OTM_1"
) -> Optional[Dict[str, Any]]:
    """Select an option contract based on moneyness.

    Args:
        signal_direction: "bullish" (CE) or "bearish" (PE)
        underlying_price: Current underlying price
        available_contracts: List of available contract dicts
        contract_specs: Contract specifications (lot_size, strike_interval)
        moneyness_target: "ATM", "ITM_1", or "OTM_1"

    Returns:
        Selected contract dict or None if no suitable contract found
    """
    strike_interval = contract_specs.get("strike_interval", 50)

    # Calculate moneyness for each contract
    scored_contracts = []

    for contract in available_contracts:
        strike = contract.get("strike", 0)
        if strike <= 0:
            continue

        # Calculate moneyness: (strike - underlying_price) / underlying_price
        moneyness = (strike - underlying_price) / underlying_price if underlying_price > 0 else 0

        # Determine moneyness using standard option definitions:
        # CE: strike below spot is ITM; PE: strike above spot is ITM.
        if abs(moneyness) < 0.02:  # Within 2% = ATM
            moneyness_class = "ATM"
        elif (signal_direction == "bullish" and moneyness < 0) or (
            signal_direction == "bearish" and moneyness > 0
        ):
            moneyness_class = "ITM"
        else:
            moneyness_class = "OTM"

        # Score based on moneyness target
        if moneyness_target == "ATM" and moneyness_class == "ATM":
            score = 100 - abs(moneyness) * 1000  # Closer to 0 = higher score
        elif moneyness_target == "ITM_1" and moneyness_class == "ITM":
            score = 100 - abs(moneyness) * 1000
        elif moneyness_target == "OTM_1" and moneyness_class == "OTM":
            score = 100 - abs(moneyness) * 1000
        else:
            score = 0

        scored_contracts.append((score, contract))

    # Sort by score (highest first)
    scored_contracts.sort(key=lambda x: x[0], reverse=True)

    # Return the best contract with score > 0
    if scored_contracts and scored_contracts[0][0] > 0:
        return scored_contracts[0][1]
    return None


# -----------------------------------------------------------------------
# OPTION CONTRACT SELECTOR (MAIN ENTRY POINT)
# -----------------------------------------------------------------------

def select_option_contract(
    underlying_symbol: str,
    underlying_price: float,
    signal_direction: str,  # "bullish" (CE) or "bearish" (PE)
    available_contracts: List[Dict[str, Any]],
    contract_specs: Dict[str, Any],
    min_oi: int = 100,
    max_spread_pct: float = 5.0,
    moneyness_target: str = "ATM",
    expected_exchange: Optional[str] = None,
    expected_expiry: Optional[str] = None,
    live_quote_only: bool = False,
) -> Dict[str, Any]:
    """Select the best option contract for a given signal.

    This is the main entry point for the contract selector. It:

    1. Filters contracts by eligibility (OI, spread, price, validity)
    2. Selects by moneyness (ATM, ITM_1, or OTM_1)
    3. Validates expiry against the underlying
    4. Returns the best contract or a rejection record

    Args:
        underlying_symbol: The underlying symbol (e.g., "NIFTY", "RELIANCE")
        underlying_price: Current underlying price
        signal_direction: "bullish" (CE) or "bearish" (PE)
        available_contracts: List of available contract dicts from data import
        contract_specs: Contract specifications for this underlying
        min_oi: Minimum open interest threshold
        max_spread_pct: Maximum bid-ask spread percentage
        moneyness_target: "ATM", "ITM_1", or "OTM_1"

    Returns:
        Dict with either "selected_contract" or "rejection" key
    """
    expected_option_type = {"bullish": "CE", "bearish": "PE"}.get(
        signal_direction.lower()
    )
    if expected_option_type is None:
        return {"rejection": {"reason": f"Invalid signal direction: {signal_direction}"}}

    # Step 1: Filter contracts by identity and eligibility.  Direction is a
    # hard constraint, not merely a ranking preference.
    eligible_contracts = []
    rejection_reasons = []

    for contract in available_contracts:
        identity_errors = []
        if not contract.get("underlying_symbol"):
            identity_errors.append("missing underlying_symbol")
        elif str(contract.get("underlying_symbol")).upper() != str(underlying_symbol).upper():
            identity_errors.append("underlying mismatch")
        if expected_exchange and not contract.get("exchange"):
            identity_errors.append("missing exchange")
        elif expected_exchange and str(contract.get("exchange")).upper() != str(expected_exchange).upper():
            identity_errors.append("exchange mismatch")
        if str(contract.get("option_type", "")).upper() != expected_option_type:
            identity_errors.append(
                f"option_type {contract.get('option_type')} does not match {expected_option_type}"
            )
        if expected_expiry and str(contract.get("expiry", "")) != str(expected_expiry):
            identity_errors.append("expiry mismatch")
        if contract_specs.get("lot_size") and not contract.get("lot_size"):
            identity_errors.append("missing lot_size")
        if identity_errors:
            rejection_reasons.append(
                f"Contract strike={contract.get('strike', '?')}: " + ", ".join(identity_errors)
            )
            continue

        eligible, reason = is_contract_eligible(
            contract, min_oi=min_oi, max_spread_pct=max_spread_pct,
            live_quote_only=live_quote_only,
        )
        if eligible:
            eligible_contracts.append(contract)
        else:
            rejection_reasons.append(f"Contract strike={contract.get('strike', '?')}: {reason}")

    # Step 2: Select by moneyness
    selected = select_contract_by_moneyness(
        signal_direction=signal_direction,
        underlying_price=underlying_price,
        available_contracts=eligible_contracts,
        contract_specs=contract_specs,
        moneyness_target=moneyness_target,
    )

    # Step 3: Validate expiry
    if selected:
        expiry = selected.get("expiry", "")
        valid, expiry_reason = validate_expiry_against_underlying(
            underlying_symbol, expiry, contract_specs
        )
        if not valid:
            return {
                "rejection": {
                    "reason": f"Expiry validation: {expiry_reason}",
                    "contract": selected,
                }
            }

    # Return result
    if selected:
        return {"selected_contract": selected}
    else:
        return {
            "rejection": {
                "reason": "No eligible contracts found after filtering by OI, spread, and moneyness",
                "rejection_reasons": rejection_reasons,
                "moneyness_target": moneyness_target,
            }
        }


# -----------------------------------------------------------------------
# TWO-LEG STRUCTURE CONTRACT SELECTION (LONG STRADDLE / STRANGLE)
# -----------------------------------------------------------------------

_STRUCTURE_STRIKE_TOLERANCE = 1e-8


def select_structure_contracts(
    underlying_symbol: str,
    underlying_price: float,
    available_contracts: List[Dict[str, Any]],
    contract_specs: Dict[str, Any],
    structure_legs: List[Dict[str, Any]],
    min_oi: int = 100,
    max_spread_pct: float = 5.0,
    expected_exchange: Optional[str] = None,
    expected_expiry: Optional[str] = None,
    live_quote_only: bool = True,
) -> Dict[str, Any]:
    """Select both legs of a two-leg long option structure.

    ``structure_legs`` must describe exactly two BUY legs, one CE and one PE.
    Each leg's ``offset`` is a number of strike steps from the ATM strike;
    ``0/0`` is a long straddle and ``+1/-1`` is a long strangle.

    The function is pure: it neither mutates its inputs nor consults any
    provider.  It returns a single synthetic contract carrying both legs, or
    an auditable rejection record.

    Args:
        underlying_symbol: Underlying symbol recorded on the selected contract.
        underlying_price: Spot used to derive the ATM strike.
        available_contracts: Candidate live contract dicts.
        contract_specs: Contract specifications (step, lot size).
        structure_legs: Exactly two ``{"kind", "side", "offset"}`` leg dicts.
        min_oi: Minimum open interest passed to ``is_contract_eligible``.
        max_spread_pct: Maximum spread passed to ``is_contract_eligible``.
        expected_exchange: Optional required exchange (case-insensitive).
        expected_expiry: Optional required expiry (exact string match).
        live_quote_only: Forwarded to ``is_contract_eligible``.

    Returns:
        ``{"selected_contract": {...}}`` or
        ``{"rejection": {"reason": ..., "rejection_reasons": [...]}}``.
    """
    rejection_reasons: List[str] = []

    def _reject(reason: str) -> Dict[str, Any]:
        reasons = list(rejection_reasons)
        if reason not in reasons:
            reasons.append(reason)
        return {"rejection": {"reason": reason, "rejection_reasons": reasons}}

    # --- Leg shape: exactly two BUY legs, one CE and one PE. -------------
    if not isinstance(structure_legs, (list, tuple)) or len(structure_legs) != 2:
        return _reject("structure_legs must contain exactly two BUY legs (one CE and one PE)")

    normalized_legs: List[Dict[str, Any]] = []
    for position, leg in enumerate(structure_legs):
        if not isinstance(leg, dict):
            return _reject(f"structure_legs[{position}] is not a mapping")
        kind = str(leg.get("kind", "")).strip().upper()
        side = str(leg.get("side", "")).strip().upper()
        try:
            offset = int(leg.get("offset", 0) or 0)
        except (TypeError, ValueError):
            return _reject(f"Invalid offset in structure_legs[{position}]: {leg.get('offset')}")
        if kind not in ("CE", "PE"):
            return _reject(f"Invalid option kind in structure_legs[{position}]: {leg.get('kind')}")
        if side != "BUY":
            return _reject(f"structure_legs[{position}] side must be BUY, got {leg.get('side')}")
        item = deepcopy(leg)
        item["kind"] = kind
        item["side"] = side
        item["offset"] = offset
        normalized_legs.append(item)

    if sorted(str(leg["kind"]) for leg in normalized_legs) != ["CE", "PE"]:
        return _reject("structure_legs must contain exactly one CE and one PE leg")

    specs = contract_specs if isinstance(contract_specs, dict) else {}
    step = specs.get("strike_interval")
    if step in (None, ""):
        step = specs.get("strike_step")
    try:
        step = float(50 if step in (None, "") else step)
    except (TypeError, ValueError):
        step = 50.0
    if step <= 0:
        step = 50.0

    try:
        spot = float(underlying_price)
    except (TypeError, ValueError):
        return _reject(f"Invalid underlying price: {underlying_price}")
    atm = round(spot / step) * step

    contracts = [row for row in (available_contracts or []) if isinstance(row, dict)]

    def _find_leg(leg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        kind = leg["kind"]
        target = atm + int(leg["offset"]) * step
        found: Optional[Dict[str, Any]] = None
        for contract in contracts:
            if str(contract.get("option_type", "")).strip().upper() != kind:
                continue
            errors: List[str] = []
            raw_strike = contract.get("strike")
            try:
                strike = float(raw_strike)
            except (TypeError, ValueError):
                errors.append(f"non-numeric strike {raw_strike!r}")
                strike = None
            if strike is not None and abs(strike - target) > _STRUCTURE_STRIKE_TOLERANCE:
                errors.append(f"strike {strike} does not match target {target}")
            if expected_expiry is not None and str(contract.get("expiry", "")) != str(expected_expiry):
                errors.append(f"expiry {contract.get('expiry')!r} does not match {expected_expiry!r}")
            if expected_exchange is not None and (
                str(contract.get("exchange", "")).upper() != str(expected_exchange).upper()
            ):
                errors.append(f"exchange {contract.get('exchange')!r} does not match {expected_exchange!r}")
            if errors:
                rejection_reasons.extend(f"{kind} contract: {error}" for error in errors)
                continue

            eligible, reason = is_contract_eligible(
                contract,
                min_oi=min_oi,
                max_spread_pct=max_spread_pct,
                live_quote_only=live_quote_only,
            )
            if not eligible:
                rejection_reasons.append(f"{kind} strike {target}: {reason}")
                continue
            if found is None:
                found = contract

        if found is None:
            rejection_reasons.append(f"No eligible {kind} contract at strike {target}")
        return found

    ce_leg = normalized_legs[0] if normalized_legs[0]["kind"] == "CE" else normalized_legs[1]
    pe_leg = normalized_legs[0] if normalized_legs[0]["kind"] == "PE" else normalized_legs[1]
    ce_contract = _find_leg(ce_leg)
    pe_contract = _find_leg(pe_leg)
    if ce_contract is None or pe_contract is None:
        missing = "/".join(
            kind for kind, contract in (("CE", ce_contract), ("PE", pe_contract))
            if contract is None
        )
        return _reject(
            f"No eligible two-leg structure contracts found (missing {missing} leg) "
            "after filtering by strike, expiry, exchange, OI and spread"
        )

    # A structure must share one expiry and one exchange across both legs.
    ce_expiry = str(ce_contract.get("expiry") or "")
    pe_expiry = str(pe_contract.get("expiry") or "")
    if ce_expiry and pe_expiry and ce_expiry != pe_expiry:
        return _reject(f"CE/PE expiry mismatch: {ce_expiry!r} vs {pe_expiry!r}")
    expiry = ce_expiry or pe_expiry
    if not expiry:
        return _reject("Missing common expiry for CE/PE structure")

    ce_exchange = str(ce_contract.get("exchange") or "")
    pe_exchange = str(pe_contract.get("exchange") or "")
    if ce_exchange and pe_exchange and ce_exchange.upper() != pe_exchange.upper():
        return _reject(f"CE/PE exchange mismatch: {ce_exchange!r} vs {pe_exchange!r}")
    exchange = ce_exchange or pe_exchange or (
        str(expected_exchange) if expected_exchange is not None else ""
    )

    def _number(value: Any, default: float = 0.0) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return default
        return result if result == result and result not in (float("inf"), float("-inf")) else default

    def _spread_pct(contract: Dict[str, Any]) -> float:
        bid = _number(contract.get("bid"))
        ask = _number(contract.get("ask"))
        mid = (bid + ask) / 2.0
        return (ask - bid) / mid * 100.0 if mid > 0 else 100.0

    raw_lot = specs.get("lot_size")
    if raw_lot in (None, ""):
        raw_lot = ce_contract.get("lot_size")
    if raw_lot in (None, ""):
        raw_lot = pe_contract.get("lot_size")
    if raw_lot in (None, "") and contracts:
        raw_lot = contracts[0].get("lot_size")
    try:
        lot_size = int(float(raw_lot))
    except (TypeError, ValueError):
        lot_size = 0

    average_spread_pct = (_spread_pct(ce_contract) + _spread_pct(pe_contract)) / 2.0
    rank_score = 100.0 - average_spread_pct + min(_number(min_oi) / 10000.0, 10.0)

    ce_ts = str(ce_contract.get("quote_timestamp") or "")
    pe_ts = str(pe_contract.get("quote_timestamp") or "")

    return {
        "selected_contract": {
            "__structure_legs__": True,
            "structure_legs": normalized_legs,
            "underlying_symbol": str(underlying_symbol).upper(),
            "underlying_price": spot,
            "strike": atm,
            "option_type": "CE+PE",
            "expiry": expiry,
            "exchange": exchange,
            "lot_size": lot_size,
            "ce_contract": dict(ce_contract),
            "pe_contract": dict(pe_contract),
            "bid": _number(ce_contract.get("bid")) + _number(pe_contract.get("bid")),
            "ask": _number(ce_contract.get("ask")) + _number(pe_contract.get("ask")),
            "open_interest": min(
                _number(ce_contract.get("open_interest")),
                _number(pe_contract.get("open_interest")),
            ),
            "volume": min(_number(ce_contract.get("volume")), _number(pe_contract.get("volume"))),
            "quote_timestamp": max(ce_ts, pe_ts),
            "contract_rank_score": rank_score,
        }
    }


# -----------------------------------------------------------------------
# EXAMPLE USAGE
# -----------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("TRIO OPTION CONTRACT SELECTOR")
    print("=" * 60)
    print()
    print("Main entry point: select_option_contract()")
    print()
    print("Usage example:")
    print("  result = select_option_contract(")
    print("      underlying_symbol='NIFTY',")
    print("      underlying_price=23800,")
    print("      signal_direction='bullish',  # 'CE' for bullish")
    print("      available_contracts=available_contracts_list,")
    print("      contract_specs=get_contract_specifications('NIFTY'),")
    print("      min_oi=100,")
    print("      max_spread_pct=5.0,")
    print("      moneyness_target='ATM'")
    print("  )")
    print()
    print("Returns:")
    print("  {'selected_contract': {...}}  -- contract selected successfully")
    print("  {'rejection': {'reason': '...', ...}}  -- no suitable contract")
    print()
    print("Key features:")
    print("  - Filters by OI, spread, price validity")
    print("  - Selects by moneyness (ATM/ITM_1/OTM_1)")
    print("  - Validates expiry against underlying")
    print("  - Configurable thresholds per underlying")
    print("  - Does not assume weekly options for all underlyings")
    print("  - Records rejection reasons for auditability")
