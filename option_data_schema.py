"""TRIO — Historical Option-Data Schema Documentation

One canonical schema for the historical options importer.

TIMESTAMP CONVENTION:
  - All timestamps are explicit Asia/Kolkata (IST) timezone
  - Format: ISO 8601 with +05:30 offset or "Z" for UTC converted to IST
  - Naive timestamps are assumed IST (not UTC)
  - Reject or flag ambiguous timestamps (do not silently interpret as UTC)

REQUIRED FIELDS (must be present for every candle):
  - timestamp: str  - Timestamp with IST timezone
  - underlying_symbol: str  - e.g., "NIFTY", "BANKNIFTY", "RELIANCE"
  - exchange: str  - e.g., "NSE", "BSE"
  - option_type: str  - "CE" or "PE"
  - strike: float  - Strike price
  - expiry: str  - Expiry date in YYYY-MM-DD format

EXECUTABLE PREMIUM FIELDS (mandatory for an options fill):
  - open: float  - Opening premium
  - high: float  - High premium during the period
  - low: float  - Low premium during the period
  - close: float - Closing premium
  - bid: float  - Best bid price; required for a realistic exit
  - ask: float  - Best ask price; required for a realistic entry

OPTIONAL FIELDS:
  - volume: int  - Trading volume (required for liquidity filtering when used)
  - open_interest: int  - Open interest (required for liquidity filtering when used)
  - lot_size: int  - Contract lot size; required before position sizing

VALIDATION RULES:
  - Duplicate timestamps: REJECT (within same underlying+option_type+expiry)
  - Invalid OHLC values: REJECT if high < low or close outside [high, low] range
  - Invalid premiums: REJECT if open/high/low/close <= 0 or negative
  - Expiry mismatch: REJECT if expiry date doesn't match the underlying's
    contract specifications for the given date
  - Contract lot size: IF provided, must match verified contract specifications
    for the underlying and historical trade date

NON-FABRICATION RULE:
  - Do not invent missing option premiums or substitute index prices silently
  - If the required quote is missing from historical data, REJECT the fill
  - Or use a separately identified fallback scenario with explicit assumptions
    and costs clearly documented
  - If neither actual option-premium history nor a clearly labelled research
    fill scenario is available, return NOT_READY for actual options P&L testing

SCHEMA EXAMPLE (JSON-like):
  {
    "timestamp": "2026-01-01 09:15:00+05:30",
    "underlying_symbol": "NIFTY",
    "exchange": "NSE",
    "option_type": "CE",
    "strike": 23500.0,
    "expiry": "2026-01-28",
    "open": 150.5,
    "high": 152.0,
    "low": 149.0,
    "close": 151.2,
    "volume": 500,
    "bid": 151.0,
    "ask": 151.5,
    "open_interest": 800,
    "lot_size": 25
  }

VALIDATION TESTS REQUIRED:
  1. Missing fields test - verify rejection when required fields absent
  2. Invalid OHLC values test - high < low or close outside range
  3. Duplicate timestamps test - within same underlying+option_type+expiry
  4. Invalid premiums test - negative or zero prices where invalid
  5. Mismatched contracts test - expiry not matching underlying/strike
  6. Lot-size validation test - provided lot_size doesn't match contract specs
  7. Spread constraints test - bid > ask or unreasonable spread
  8. No eligible contract available test - all contracts filtered out

DO NOT FABRICATE:
  - Missing option premiums
  - Index prices as option fills
  - Synthetic win rates or outcomes
  - Arbitrary P&L scaling factors
"""

NOT_READY_FOR_OPTIONS_PNL = "NOT_READY_FOR_OPTIONS_PNL"


def options_pnl_readiness(
    option_records,
    *,
    provenance: str = "unavailable",
) -> str:
    """Classify option-P&L readiness without treating fixtures as history.

    ``provenance`` must be explicitly set to ``validated_historical`` by the
    caller after independent data-quality checks.  Presence of rows alone is
    intentionally insufficient because the repository also contains synthetic
    software-test fixtures.
    """
    if provenance != "validated_historical":
        return NOT_READY_FOR_OPTIONS_PNL
    if not option_records:
        return NOT_READY_FOR_OPTIONS_PNL
    required = ("timestamp", "strike", "expiry", "option_type", "bid", "ask")
    if any(any(row.get(field) in (None, "") for field in required) for row in option_records):
        return NOT_READY_FOR_OPTIONS_PNL
    return "OPTIONS_PNL_READY"
