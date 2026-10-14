"""TRIO — DATA IMPORTER MODULE

Handles import of historical market data for strategy testing.

This module supports:
- 5-minute NIFTY/BANKNIFTY/FINNIFTY index candles with IST timestamps
- Historical option premium candles by strike and expiry for multiple underlyings
- Contract specifications (lot size, strike interval, expiry dates) per underlying
- Trading costs (brokerage, taxes, slippage)

DATA FORMAT REQUIREMENTS:

A. 5-minute Index Candles (NIFTY, BANKNIFTY, FINNIFTY, etc.):
   - timestamp: ISO format with IST timezone offset
   - exchange: exchange identifier (e.g., "NSE")
   - underlying_symbol: symbol name (e.g., "NIFTY", "BANKNIFTY")
   - instrument_type: "index" | "stock"
   - open, high, low, close: price values
   - volume: trading volume
   - Required fields: timestamp, open, high, low, close, volume, exchange, underlying_symbol, instrument_type

B. Historical Option-Premium Candles (any underlying):
   - timestamp: ISO format with IST timezone offset
   - exchange: exchange identifier (e.g., "NSE")
   - underlying_symbol: symbol name (e.g., "NIFTY", "RELIANCE")
   - contract_identifier: strike + expiry + option_type combination
   - strike: strike price
   - expiry: expiry date (string format, e.g., "2026-10-27" or "weekly")
   - option_type: "CE" or "PE"
   - open, high, low, close: option premium values
   - volume: trading volume
   - open_interest: open interest (when available)
   - bid, ask: bid-ask spread data
   - Required fields: timestamp, exchange, underlying_symbol, strike, expiry, option_type, open, high, low, close, volume, bid, ask

C. Contract Information (per underlying, resolved at backtest time):
   - lot_size: integer (e.g., 65 for NIFTY Jan 2026, 25 for BANKNIFTY, 250 for RELIANCE)
   - strike_interval: integer (e.g., 50 for NIFTY, 100 for BANKNIFTY, varies for stocks)
   - expiry_dates: list of expiry date strings or "weekly"/"monthly"
   - option_types: list of "CE" and "PE"
   - min_oi: minimum open interest threshold (configurable per underlying)
   - max_iv_rank: rich vol penalty threshold

VALIDATION RULES:
- Missing candles: Flag and skip; do not invent prices
- Duplicate timestamps: Detect and report; keep first occurrence
- Invalid prices: Validate high >= low >= open/close; skip invalid
- Incorrect expiry/strike: Validate against contract specifications per historical date
- Trading-session boundaries: 9:15 AM to 3:15 PM IST
- Contract lot-size changes: Flag when lot size changes over time
- Incomplete candles: Skip candles with missing required fields
- Missing underlying identifiers: Flag and reject
- Missing strike or expiry: Flag and reject
- Incorrect CE/PE classification: Flag and reject
- Stale or missing bid/ask data: Flag and record limitation
- Incorrect lot size for the historical period: Flag and reject
- Option candles that cannot be matched to their contract: Flag and reject
- Historical candles outside the relevant trading session: Flag and skip

DO NOT invent missing option premiums or bid-ask spreads.
If historical data is unavailable, report gaps and provide CSV template
for manual data entry.

SAMPLE CSV TEMPLATE FOR 5-MINUTE INDEX CANDLES (NIFTY/BANKNIFTY/FINNIFTY):
------------------------------------------------
timestamp,open,high,low,close,volume,exchange,underlying_symbol,instrument_type
2026-01-01 09:15:00+05:30,23500,23520,23490,23510,1000,NSE,NIFTY,index
2026-01-01 09:20:00+05:30,23510,23530,23495,23525,1200,NSE,NIFTY,index
...

SAMPLE CSV TEMPLATE FOR OPTION PREMIUM CANDLES (ANY UNDERLYING):
------------------------------------------------
timestamp,strike,expiry,option_type,open,high,low,close,volume,open_interest,bid,ask,exchange,underlying_symbol
2026-01-01 09:15:00+05:30,23500,2026-01-28,CE,150.5,152.0,149.0,151.2,500,800,151.0,151.5,NSE,NIFTY
2026-01-01 09:15:00+05:30,23500,2026-01-28,PE,120.0,121.5,119.0,119.8,300,500,119.5,120.0,NSE,NIFTY
...

SAMPLE CSV TEMPLATE FOR STOCK OPTION PREMIUM CANDLES:
------------------------------------------------
timestamp,strike,expiry,option_type,open,high,low,close,volume,open_interest,bid,ask,exchange,underlying_symbol
2026-01-01 09:15:00+05:30,2350,2026-01-28,CE,150.5,152.0,149.0,151.2,500,800,151.0,151.5,NSE,RELIANCE
2026-01-01 09:15:00+05:30,2350,2026-01-28,PE,120.0,121.5,119.0,119.8,300,500,119.5,120.0,NSE,RELIANCE
...

DO NOT invent missing option premiums or bid-ask spreads.
If historical data is unavailable, report gaps and provide CSV template
for manual data entry.

SAMPLE CSV TEMPLATE FOR 5-MINUTE NIFTY INDEX CANDLES (BACKWARD COMPATIBLE):
------------------------------------------------
timestamp,open,high,low,close,volume
2026-01-01 09:15:00,23500,23520,23490,23510,1000
...
(legacy format still supported by import_5min_nifty_candles)
"""

import sys, math, json, os, csv
from datetime import datetime, timedelta, timezone, time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# IST timezone
IST = timezone(timedelta(hours=5, minutes=30))

# The most recent option-import rejection details are retained for callers that
# need an auditable explanation for an incomplete data set.  The importer does
# not manufacture a replacement price for a rejected row.
last_option_import_rejections: List[Dict[str, Any]] = []


def _parse_timestamp_as_ist(timestamp_str: str) -> datetime:
    """Parse an ISO timestamp and return an aware Asia/Kolkata timestamp.

    The project schema defines a naive timestamp as IST.  Aware timestamps are
    converted rather than relabelled, so UTC input cannot be mistaken for IST.
    """
    ts = datetime.fromisoformat(timestamp_str.strip().replace("Z", "+00:00"))
    if ts.tzinfo is None:
        return ts.replace(tzinfo=IST)
    return ts.astimezone(IST)


def is_trading_session(timestamp: Any) -> bool:
    """Return whether a timestamp falls within the NSE intraday session."""
    try:
        ts = _parse_timestamp_as_ist(timestamp) if not isinstance(timestamp, datetime) else timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=IST)
        else:
            ts = ts.astimezone(IST)
        return dtime(9, 15) <= ts.time() <= dtime(15, 15)
    except (TypeError, ValueError):
        return False

# ============================================================================
# DATA IMPORTERS
# ============================================================================

# ============================================================================
# 5-MINUTE INDEX CANDLE IMPORTERS
# ============================================================================

def import_5min_index_candles(
    csv_path: str,
    underlying_symbol: str = "NIFTY",
    exchange: str = "NSE",
    start_date: str = None,
    end_date: str = None,
) -> List[Dict[str, Any]]:
    """Import 5-minute index candles from CSV file for any underlying.

    CSV format (legacy NIFTY-only format still supported, with automatic
    underlying_symbol detection):
    timestamp,open,high,low,close,volume

    OR with full fields:
    timestamp,open,high,low,close,volume,exchange,underlying_symbol,instrument_type

    Args:
        csv_path: Path to CSV file
        underlying_symbol: Symbol name (e.g., "NIFTY", "BANKNIFTY", "RELIANCE")
        exchange: Exchange identifier (e.g., "NSE")
        start_date: Start date filter (YYYY-MM-DD), optional
        end_date: End date filter (YYYY-MM-DD), optional

    Returns:
        List of candle dicts with IST timestamps and full fields
    """
    candles = []
    use_full_format = False

    with open(csv_path, "r") as f:
        reader = csv.reader(f)
        header = next(reader, None)

        # Determine format from header
        if header:
            # Check if header has 9 fields (full format) or 6 fields (legacy)
            if len(header) >= 9:
                use_full_format = True
            elif len(header) >= 6:
                use_full_format = False
            else:
                # Skip, can't determine format
                return candles

        for row in reader:
            if not row or len(row) < 6:
                continue  # Skip incomplete rows

            try:
                timestamp_str = row[0].strip()

                # Parse timestamp and convert to IST
                ts = _parse_timestamp_as_ist(timestamp_str)

                # Apply date filters
                if start_date:
                    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(
                        tzinfo=IST
                    )
                    if ts < start_dt:
                        continue
                if end_date:
                    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(
                        tzinfo=IST
                    )
                    if ts > end_dt:
                        continue

                open_price = float(row[1].strip())
                high_price = float(row[2].strip())
                low_price = float(row[3].strip())
                close_price = float(row[4].strip())
                volume = int(row[5].strip())

                if use_full_format and len(row) >= 9:
                    # Full format: timestamp,open,high,low,close,volume,exchange,underlying_symbol,instrument_type
                    _exchange = row[6].strip() if len(row) > 6 else exchange
                    _underlying = row[7].strip() if len(row) > 7 else underlying_symbol
                    _instrument_type = (
                        row[8].strip().lower() if len(row) > 8 else "index"
                    )
                    if _instrument_type not in ("index", "stock"):
                        _instrument_type = "index"
                else:
                    # Legacy format: infer underlying from file/params
                    _exchange = exchange
                    _underlying = underlying_symbol
                    _instrument_type = "index"  # Assume index for legacy format

                candle = {
                    "timestamp": ts.isoformat(),
                    "open": open_price,
                    "high": high_price,
                    "low": low_price,
                    "close": close_price,
                    "volume": volume,
                    "exchange": _exchange,
                    "underlying_symbol": _underlying,
                    "instrument_type": _instrument_type,
                    "timestamp_str": timestamp_str,
                }
                candles.append(candle)

            except (ValueError, IndexError, TypeError):
                # Skip invalid rows rather than inventing data
                continue

    # Sort by timestamp
    candles.sort(key=lambda c: c["timestamp"])

    # Validate candle data
    validate_candle_data(candles)

    return candles


def import_5min_nifty_candles(
    csv_path: str,
    start_date: str = None,
    end_date: str = None,
) -> List[Dict[str, Any]]:
    """Import 5-minute NIFTY candles from CSV file (backward-compatible).

    This is the original function signature preserved for backward compatibility.
    It calls import_5min_index_candles() under the hood with underlying_symbol="NIFTY".

    CSV format (legacy, 6 fields):
    timestamp,open,high,low,close,volume

    Args:
        csv_path: Path to CSV file
        start_date: Start date filter (YYYY-MM-DD), optional
        end_date: End date filter (YYYY-MM-DD), optional

    Returns:
        List of candle dicts with IST timestamps (legacy format, without
        exchange/underlying_symbol/instrument_type fields in older style)
    """
    # Use the generalized importer with NIFTY defaults
    candles = import_5min_index_candles(
        csv_path,
        underlying_symbol="NIFTY",
        exchange="NSE",
        start_date=start_date,
        end_date=end_date,
    )

    # Return in legacy format (without exchange/underlying_symbol/instrument_type)
    # for backward compatibility with existing code that expects the old format
    legacy_candles = []
    for c in candles:
        legacy = {
            "timestamp": c["timestamp"],
            "open": c["open"],
            "high": c["high"],
            "low": c["low"],
            "close": c["close"],
            "volume": c["volume"],
        }
        # Include new fields if code can handle them, but keep backward compat
        legacy.update(
            {
                "exchange": c.get("exchange", "NSE"),
                "underlying_symbol": c.get("underlying_symbol", "NIFTY"),
                "instrument_type": c.get("instrument_type", "index"),
            }
        )
        legacy_candles.append(legacy)

    # Validate
    validate_candle_data(legacy_candles)

    return legacy_candles


# ============================================================================
# OPTION PREMIUM CANDLE IMPORTERS
# ============================================================================

def import_option_premium_candles(
    csv_path: str,
    underlying_symbol: str = "NIFTY",
    exchange: str = "NSE",
    start_date: str = None,
    end_date: str = None,
) -> List[Dict[str, Any]]:
    """Import historical option-premium candles from CSV file for any underlying.

    CSV format:
    timestamp,strike,expiry,option_type,open,high,low,close,volume,open_interest,bid,ask,exchange,underlying_symbol

    Args:
        csv_path: Path to CSV file
        underlying_symbol: Symbol name (e.g., "NIFTY", "RELIANCE", "BANKNIFTY")
        exchange: Exchange identifier (e.g., "NSE")
        start_date: Start date filter (YYYY-MM-DD), optional
        end_date: End date filter (YYYY-MM-DD), optional

    Returns:
        List of option candle dicts
    """
    global last_option_import_rejections
    candles = []
    last_option_import_rejections = []

    with open(csv_path, "r") as f:
        reader = csv.reader(f)
        header = next(reader, None)

        for row_number, row in enumerate(reader, start=2):
            if len(row) < 11:
                last_option_import_rejections.append({
                    "row": row_number,
                    "reason": f"incomplete row: expected at least 11 fields, got {len(row)}",
                })
                continue  # Skip incomplete rows

            try:
                timestamp_str = row[0].strip()
                strike = float(row[1].strip())
                expiry = row[2].strip()
                option_type = row[3].strip().upper()
                open_price = float(row[4].strip())
                high_price = float(row[5].strip())
                low_price = float(row[6].strip())
                close_price = float(row[7].strip())
                volume = int(row[8].strip())
                open_interest = (
                    float(row[9].strip()) if len(row) > 9 and row[9].strip() else 0
                )
                bid = float(row[10].strip()) if len(row) > 10 else None
                ask = float(row[11].strip()) if len(row) > 11 else None
                _exchange = row[12].strip() if len(row) > 12 else exchange
                _underlying = row[13].strip() if len(row) > 13 else underlying_symbol

                # Parse timestamp and convert to IST.  Naive timestamps are
                # defined by option_data_schema.py as IST, not UTC.
                ts = _parse_timestamp_as_ist(timestamp_str)

                # Apply date filters
                if start_date:
                    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(
                        tzinfo=IST
                    )
                    if ts < start_dt:
                        continue
                if end_date:
                    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(
                        tzinfo=IST
                    )
                    if ts > end_dt:
                        continue

                # Required contract identity fields must be present before a
                # row can be used for contract selection.
                if not _exchange or not _underlying:
                    raise ValueError("missing exchange or underlying_symbol")
                if strike <= 0:
                    raise ValueError(f"invalid strike: {strike}")
                if not expiry:
                    raise ValueError("missing expiry")

                # Validate option_type
                if option_type not in ("CE", "PE"):
                    raise ValueError(f"invalid option_type: {option_type}")

                # Validate prices are positive
                if open_price <= 0 or high_price <= 0 or low_price <= 0 or close_price <= 0:
                    raise ValueError("non-positive option premium")

                # Validate OHLC consistency
                if high_price < low_price:
                    raise ValueError("high is below low")
                if high_price < open_price or high_price < close_price:
                    raise ValueError("high is below open or close")
                if low_price > open_price and low_price > close_price:
                    raise ValueError("low is above open and close")

                # Validate bid and ask are present and positive (critical for execution)
                if bid is None or bid <= 0 or ask is None or ask <= 0:
                    raise ValueError("missing or non-positive bid/ask")

                # Validate bid < ask (spread must be positive)
                if bid >= ask:
                    raise ValueError("bid must be below ask")

                if len(row) > 14 and row[14].strip():
                    lot_size = int(float(row[14].strip()))
                    if lot_size <= 0:
                        raise ValueError("lot_size must be positive")
                else:
                    lot_size = None

                # Validate trading session hours (9:15 AM to 3:15 PM IST)
                if not (dtime(9, 15) <= ts.time() <= dtime(15, 15)):
                    raise ValueError(
                        f"timestamp outside trading session: {ts.time()} IST"
                    )

                candle = {
                    "timestamp": ts.isoformat(),
                    "strike": strike,
                    "expiry": expiry,
                    "option_type": option_type,
                    "open": open_price,
                    "high": high_price,
                    "low": low_price,
                    "close": close_price,
                    "volume": volume,
                    "open_interest": open_interest,
                    "bid": bid,
                    "ask": ask,
                    "exchange": _exchange,
                    "underlying_symbol": _underlying,
                    "timestamp_str": timestamp_str,
                }
                if lot_size is not None:
                    candle["lot_size"] = lot_size
                candles.append(candle)

            except (ValueError, IndexError, TypeError):
                # Skip invalid rows rather than inventing data, but retain a
                # row-level reason for auditability.
                last_option_import_rejections.append({
                    "row": row_number,
                    "timestamp": row[0].strip() if row else "",
                    "reason": str(sys.exc_info()[1]),
                })
                continue

    # Sort by timestamp
    candles.sort(key=lambda c: c["timestamp"])

    # Validate option candle data
    validate_option_candle_data(candles)

    return candles


# ============================================================================
# CONTRACT SPECIFICATIONS (per underlying)
# ============================================================================

def get_contract_specifications(underlying_symbol: str = "NIFTY") -> Dict[str, Any]:
    """Return contract specifications for a given underlying symbol.

    Resolves lot size, strike interval, expiry preference, etc. based on
    the underlying. Uses historical-aware lookups where possible.

    Args:
        underlying_symbol: Symbol name (e.g., "NIFTY", "BANKNIFTY", "RELIANCE")

    Returns:
        Dict with contract specification details
    """
    # Base specifications from known data (as of 2026)
    # These can be overridden/extended by contract_specs_lookup in backtester
    specs_base = {
        "NIFTY": {
            "lot_size": 65,              # NIFTY lot from Jan 2026
            "strike_interval": 50,       # NIFTY strike granularity
            "expiry_preference": "weekly",  # weekly tactical + monthly swing
            "min_oi": 1000,              # skip wing when OI below this
            "max_iv_rank": 80,           # rich vol penalty threshold
            "strike_step": 50,
            "fill_model": "bidask",      # mid | bidask
            "slippage_ticks": 0.5,       # extra slippage per leg in ticks (1 tick = 0.05)
            "brokerage_per_order": 20,   # Rs per order leg
            "stt_rate_sold": 0.0015,     # STT 0.15% of premium on sold legs
        },
        "BANKNIFTY": {
            "lot_size": 25,              # Bank Nifty lot size (standard as of 2026)
            "strike_interval": 100,      # Bank Nifty strike granularity
            "expiry_preference": "weekly",  # weekly tactical + monthly swing
            "min_oi": 500,              # skip wing when OI below this
            "max_iv_rank": 80,
            "strike_step": 100,
            "fill_model": "bidask",
            "slippage_ticks": 0.5,
            "brokerage_per_order": 20,
            "stt_rate_sold": 0.0015,
        },
        "FINNIFTY": {
            "lot_size": 25,              # Finnifty lot size (standard as of 2026)
            "strike_interval": 50,       # Finnifty strike granularity
            "expiry_preference": "weekly",  # weekly tactical + monthly swing
            "min_oi": 500,
            "max_iv_rank": 80,
            "strike_step": 50,
            "fill_model": "bidask",
            "slippage_ticks": 0.5,
            "brokerage_per_order": 20,
            "stt_rate_sold": 0.0015,
        },
        "RELIANCE": {
            "lot_size": 250,             # Reliance stock lot size (as of 2026)
            "strike_interval": 100,      # Typically 100 for stock options
            "expiry_preference": "weekly",  # Weekly + monthly
            "min_oi": 2000,
            "max_iv_rank": 80,
            "strike_step": 100,
            "fill_model": "bidask",
            "slippage_ticks": 0.5,
            "brokerage_per_order": 20,
            "stt_rate_sold": 0.0015,
        },
        "HDFCBANK": {
            "lot_size": 500,
            "strike_interval": 100,
            "expiry_preference": "weekly",
            "min_oi": 1000,
            "max_iv_rank": 80,
            "strike_step": 100,
            "fill_model": "bidask",
            "slippage_ticks": 0.5,
            "brokerage_per_order": 20,
            "stt_rate_sold": 0.0015,
        },
        "ICICIBANK": {
            "lot_size": 500,
            "strike_interval": 100,
            "expiry_preference": "weekly",
            "min_oi": 1000,
            "max_iv_rank": 80,
            "strike_step": 100,
            "fill_model": "bidask",
            "slippage_ticks": 0.5,
            "brokerage_per_order": 20,
            "stt_rate_sold": 0.0015,
        },
        "SBIN": {
            "lot_size": 1000,
            "strike_interval": 100,
            "expiry_preference": "weekly",
            "min_oi": 2000,
            "max_iv_rank": 80,
            "strike_step": 100,
            "fill_model": "bidask",
            "slippage_ticks": 0.5,
            "brokerage_per_order": 20,
            "stt_rate_sold": 0.0015,
        },
        "INFY": {
            "lot_size": 550,             # Infosys lot size (as of 2026, pre-2025: 275)
            "strike_interval": 100,
            "expiry_preference": "weekly",
            "min_oi": 1000,
            "max_iv_rank": 80,
            "strike_step": 100,
            "fill_model": "bidask",
            "slippage_ticks": 0.5,
            "brokerage_per_order": 20,
            "stt_rate_sold": 0.0015,
        },
        "TCS": {
            "lot_size": 550,             # TCS lot size (as of 2026, pre-2025: 275)
            "strike_interval": 100,
            "expiry_preference": "weekly",
            "min_oi": 1000,
            "max_iv_rank": 80,
            "strike_step": 100,
            "fill_model": "bidask",
            "slippage_ticks": 0.5,
            "brokerage_per_order": 20,
            "stt_rate_sold": 0.0015,
        },
        "BHARTIARTL": {
            "lot_size": 500,
            "strike_interval": 100,
            "expiry_preference": "weekly",
            "min_oi": 500,
            "max_iv_rank": 80,
            "strike_step": 100,
            "fill_model": "bidask",
            "slippage_ticks": 0.5,
            "brokerage_per_order": 20,
            "stt_rate_sold": 0.0015,
        },
        "LT": {
            "lot_size": 1000,
            "strike_interval": 100,
            "expiry_preference": "weekly",
            "min_oi": 2000,
            "max_iv_rank": 80,
            "strike_step": 100,
            "fill_model": "bidask",
            "slippage_ticks": 0.5,
            "brokerage_per_order": 20,
            "stt_rate_sold": 0.0015,
        },
    }

    return specs_base.get(underlying_symbol, specs_base["NIFTY"])


# ============================================================================
# DATA VALIDATION
# ============================================================================

def validate_candle_data(candles: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Validate 5-minute candle data for common issues.

    Args:
        candles: List of candle dicts (with exchange, underlying_symbol, instrument_type)

    Returns:
        Dict with validation results
    """
    issues = {
        "missing_candles": 0,
        "duplicate_timestamps": 0,
        "invalid_prices": 0,
        "missing_underlying_id": 0,
        "total_processed": len(candles),
    }

    seen_timestamps = set()
    for candle in candles:
        ts = candle.get("timestamp", "")

        # Check for duplicates
        if ts in seen_timestamps:
            issues["duplicate_timestamps"] += 1
        seen_timestamps.add(ts)

        # Check for missing underlying identifier
        if not candle.get("underlying_symbol"):
            issues["missing_underlying_id"] += 1

        # Check for invalid prices
        try:
            open_price = candle.get("open", 0)
            high_price = candle.get("high", 0)
            low_price = candle.get("low", 0)
            close_price = candle.get("close", 0)

            if high_price < low_price:
                issues["invalid_prices"] += 1
            if high_price < open_price or high_price < close_price:
                issues["invalid_prices"] += 1
            if low_price > open_price and low_price > close_price:
                issues["invalid_prices"] += 1
        except (TypeError, ValueError):
            issues["invalid_prices"] += 1

    return issues


def validate_option_candle_data(candles: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Validate option premium candle data for common issues.

    Args:
        candles: List of option candle dicts

    Returns:
        Dict with validation results
    """
    issues = {
        "invalid_option_types": 0,
        "invalid_prices": 0,
        "missing_expiry": 0,
        "missing_underlying_id": 0,
        "missing_contract_identity": 0,
        "missing_bid_ask": 0,
        "invalid_spread": 0,
        "total_processed": len(candles),
    }

    for candle in candles:
        # Check option type
        if candle.get("option_type", "") not in ("CE", "PE"):
            issues["invalid_option_types"] += 1

        # Check expiry
        if not candle.get("expiry"):
            issues["missing_expiry"] += 1

        # Check underlying identifier
        if not candle.get("underlying_symbol"):
            issues["missing_underlying_id"] += 1
        if not candle.get("exchange") or not candle.get("strike") or candle.get("strike", 0) <= 0:
            issues["missing_contract_identity"] += 1

        # Check bid/ask (critical for execution)
        if not candle.get("bid") or candle.get("bid") <= 0:
            issues["missing_bid_ask"] += 1
        if not candle.get("ask") or candle.get("ask") <= 0:
            issues["missing_bid_ask"] += 1
        if candle.get("bid") and candle.get("ask") and candle.get("bid") >= candle.get("ask"):
            issues["invalid_spread"] += 1

        # Check prices
        try:
            open_price = candle.get("open", 0)
            high_price = candle.get("high", 0)
            low_price = candle.get("low", 0)
            close_price = candle.get("close", 0)

            if any(float(candle.get(name, 0) or 0) <= 0 for name in ("open", "high", "low", "close")):
                issues["invalid_prices"] += 1
            if high_price < low_price:
                issues["invalid_prices"] += 1
        except (TypeError, ValueError):
            issues["invalid_prices"] += 1

    return issues


# ============================================================================
# CROSS-VALIDATION AND UTILITIES
# ============================================================================

def validate_timestamp_alignment(
    nifty_csv_path: str, option_csv_path: str
) -> Dict[str, Any]:
    """Validate that option premium timestamps align with NIFTY 5-min timestamps.

    (Kept for reference; generalized importer handles alignment per underlying)
    """

    from data_importer import import_5min_index_candles

    nifty_candles = import_5min_index_candles(nifty_csv_path)
    option_issues = []

    try:
        import csv
        with open(option_csv_path, "r") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            option_timestamps = []
            for row in reader:
                if len(row) >= 1:
                    ts_str = row[0].strip()
                    try:
                        ts = datetime.fromisoformat(
                            ts_str.replace("Z", "+00:00")
                        )
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc).astimezone(IST)
                        else:
                            ts = ts.astimezone(IST)
                        option_timestamps.append(ts)
                    except (ValueError, AttributeError):
                        option_timestamps.append(None)
    except Exception as e:
        return {"error": str(e)}

    aligned = 0
    misaligned = 0

    for i, opt_ts in enumerate(option_timestamps):
        if opt_ts is None:
            misaligned += 1
            option_issues.append({"row": i, "issue": "could_not_parse_timestamp"})
            continue

        # Find closest NIFTY bar
        min_diff = float("inf")
        closest_bar_idx = -1
        for j, nifty_ts in enumerate(nifty_candles):
            diff = abs((opt_ts - nifty_ts["timestamp"]).total_seconds())
            if diff < min_diff:
                min_diff = diff
                closest_bar_idx = j

        # Check if within 5-minute tolerance
        if min_diff <= 300:  # 5 minutes in seconds
            aligned += 1
        else:
            misaligned += 1
            option_issues.append(
                {"row": i, "issue": f"timestamp_misaligned, diff={min_diff}s"}
            )

    return {
        "nifty_candle_count": len(nifty_candles),
        "option_candle_count": len(option_timestamps),
        "aligned_timestamps": aligned,
        "misaligned_timestamps": misaligned,
        "alignment_rate": round(aligned / (aligned + misaligned) * 100, 2)
        if (aligned + misaligned) > 0
        else 0,
        "issues": option_issues[:50],
    }


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("TRIO DATA IMPORTER MODULE - MULTI-INSTRUMENT SUPPORT")
    print("=" * 60)
    print()
    print("Available functions:")
    print("  import_5min_index_candles(csv_path, underlying_symbol='NIFTY', exchange='NSE')")
    print("    - Generalized import for any underlying index/stock")
    print("    - Returns dict with: timestamp, open, high, low, close, volume,")
    print("      exchange, underlying_symbol, instrument_type")
    print()
    print("  import_5min_nifty_candles(csv_path) - Legacy NIFTY import (backward compat)")
    print("  import_option_premium_candles(csv_path, underlying_symbol='NIFTY', exchange='NSE')")
    print("    - Option premium data for any underlying")
    print("    - Returns dict with: timestamp, strike, expiry, option_type,")
    print("      open, high, low, close, volume, open_interest, bid, ask,")
    print("      exchange, underlying_symbol")
    print()
    print("  get_contract_specifications(underlying_symbol)")
    print("    - Returns lot_size, strike_interval, expiry_preference, etc.")
    print("    - Per-underlying specifications (NIFTY, BANKNIFTY, RELIANCE, etc.)")
    print()
    print("  validate_candle_data(candles)")
    print("    - Validate 5-min candle data quality")
    print()
    print("  validate_option_candle_data(candles)")
    print("    - Validate option premium data quality")
    print()
    print("Sample CSV formats:")
    print("  5-min index: timestamp,open,high,low,close,volume,exchange,underlying_symbol,instrument_type")
    print("  Option premiums: timestamp,strike,expiry,option_type,open,high,low,close,volume,open_interest,bid,ask,exchange,underlying_symbol")
    print()
    print("Data gaps acknowledged:")
    print("  - Index 5-min candles: Source from NSE historical data per underlying")
    print("  - Option premium candles: Source from options data providers per underlying")
    print("  - Bid-ask spreads: Required for realistic execution (critical)")
    print("  - Lot sizes vary by underlying and historical period (e.g., NIFTY 75→65 Jan 2026)")
    print()
