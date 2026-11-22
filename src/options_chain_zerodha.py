import math
import os
import time
from typing import Any, Dict, List, Optional
from datetime import datetime, date, timedelta, timezone
from src.zerodha_market_data import ZerodhaMarketData, get_shared_adapter
from src.options_chain import ChainUnavailable
from src.utils import get_logger

logger = get_logger("options_chain_zerodha")

# Kite's quote endpoint accepts at most 500 instruments per call, but the
# request is a GET with every symbol in the query string, so a full 500-symbol
# batch can exceed the gateway's header/URL limit.  100 is the proven value in
# this codebase; 200 halves the call count and stays well inside the URL limit.
_DEFAULT_QUOTE_BATCH = 100
_MAX_QUOTE_BATCH = 500
_DEFAULT_QUOTE_SPACING = 0.25

# Monotonic timestamp of the last Kite quote request.  Module level on purpose:
# the limit is per API key, so pacing must hold across chain fetches, not just
# across batches inside one fetch.
_LAST_QUOTE_AT: Optional[float] = None


def _finite_option(value: Any, default: float) -> float:
    """Coerce ``value`` to a finite, non-negative float, else return default."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(number) or number < 0:
        return default
    return number


def _quote_batch_size(configured: Optional[int] = None) -> int:
    """Instruments per Kite quote() request (config > env > safe default)."""
    if configured is not None:
        raw = _finite_option(configured, _DEFAULT_QUOTE_BATCH)
    else:
        raw = _finite_option(os.environ.get("TRIO_ZERODHA_QUOTE_BATCH"),
                             _DEFAULT_QUOTE_BATCH)
    return int(min(_MAX_QUOTE_BATCH, max(1, raw)))


def _quote_spacing_seconds(configured: Optional[float] = None) -> float:
    """Minimum gap between Kite quote() requests (config > env > default)."""
    if configured is not None:
        return _finite_option(configured, _DEFAULT_QUOTE_SPACING)
    return _finite_option(os.environ.get("TRIO_ZERODHA_QUOTE_SPACING"),
                          _DEFAULT_QUOTE_SPACING)


def _pace_quote(spacing: float) -> None:
    """Wait out the remainder of ``spacing`` since the previous quote call."""
    global _LAST_QUOTE_AT
    if spacing > 0 and _LAST_QUOTE_AT is not None:
        remaining = spacing - (time.monotonic() - _LAST_QUOTE_AT)
        if remaining > 0:
            time.sleep(remaining)
    _LAST_QUOTE_AT = time.monotonic()


def reset_rate_limit_state() -> None:
    """Forget pacing state (tests and operator recovery)."""
    global _LAST_QUOTE_AT
    _LAST_QUOTE_AT = None


def _safe_getquote_value(value: Any, default: Any = 0.0) -> Any:
    """Safely convert a quote value, handling None and type issues."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _safe_getquote_dict(q: Any, key: str, default: Any = 0.0) -> Any:
    """Safely get a value from a Zerodha quote dict, avoiding None/type errors."""
    if isinstance(q, dict):
        val = q.get(key, default)
        if val is None:
            return default
        return _safe_getquote_value(val, default)
    return _safe_getquote_value(q, default)


def _quote_bid_ask(q: Any) -> tuple:
    """Extract best bid/ask from a Zerodha quote (top-level or depth book)."""
    if not isinstance(q, dict):
        return 0.0, 0.0
    bid = _safe_getquote_value(q.get("bid"), 0.0)
    ask = _safe_getquote_value(q.get("ask"), 0.0)
    if bid <= 0 or ask <= 0:
        depth = q.get("depth") or {}
        buy = (depth.get("buy") or []) if isinstance(depth, dict) else []
        sell = (depth.get("sell") or []) if isinstance(depth, dict) else []
        if buy and isinstance(buy[0], dict):
            bid = _safe_getquote_value(buy[0].get("price"), bid)
        if sell and isinstance(sell[0], dict):
            ask = _safe_getquote_value(sell[0].get("price"), ask)
    return bid, ask


def _ist_now() -> str:
    """Return current IST timestamp as ISO string with +05:30 offset."""
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).isoformat()


def _is_rate_limited(exc: BaseException) -> bool:
    """True when Kite rejected the call because of throttling."""
    text = f"{type(exc).__name__}: {exc}".lower()
    collapsed = text.replace(" ", "").replace("_", "")
    return ("toomanyrequests" in collapsed or "ratelimit" in collapsed
            or "status429" in collapsed or "429" in text)


def _chain_failure(reason: str, detail: str,
                   raise_errors: bool) -> Optional[Dict[str, Any]]:
    """Abort with a typed reason so the router can fail over.

    Returns ``None`` for direct callers (the historical contract); raises the
    typed error only when the router asked for it.
    """
    if raise_errors:
        raise ChainUnavailable("zerodha", reason, detail)
    return None


def _convert_to_ist_iso(dt: Any) -> str:
    """Convert a datetime-like value to IST ISO format string."""
    if dt is None:
        return _ist_now()
    if isinstance(dt, datetime):
        ist = timezone(timedelta(hours=5, minutes=30))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ist)
        return dt.astimezone(ist).isoformat()
    # fallback: return now
    return _ist_now()


def fetch_zerodha_chain(symbol: str,
                        raise_errors: bool = False,
                        max_expiries: Optional[int] = None,
                        max_strike_distance_pct: Optional[float] = None,
                        quote_batch_size: Optional[int] = None,
                        quote_spacing_seconds: Optional[float] = None
                        ) -> Optional[Dict[str, Any]]:
    """Fetch option chain from Zerodha Kite API and format for TRIO.

    Returns None on unrecoverable errors; transient issues are handled
    gracefully by the caller (diagnostics['fetch_errors'] incremented).

    ``raise_errors`` is used by the provider router: a throttled or partial
    chain must be reported as a typed ``ChainUnavailable`` so the caller can
    fall back to NSE instead of trading a half-quoted chain.

    ``max_expiries`` optionally restricts the quoted contracts to the nearest
    N listed expiries.  The default (``None``) keeps the historical behaviour
    of quoting every listed expiry.  A fast intraday scan can pass ``1`` to
    stay well inside Kite's quote rate limit.

    ``max_strike_distance_pct`` optionally drops strikes further than that
    fraction of spot (for example ``0.10`` for +/-10%).  The default keeps
    every listed strike.  The planner only selects near-the-money contracts,
    so a narrow band cuts batch-quote calls without shrinking the tradable
    set in practice.

    ``quote_batch_size`` and ``quote_spacing_seconds`` bound the request
    volume: at most that many contracts per Kite ``quote()`` call, with at
    least that gap between consecutive calls (including calls made by a
    previous chain fetch, because Kite's limit is per API key).
    """
    try:
        adapter = get_shared_adapter()

        # 1. Get Spot Price
        spot_map = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK"}
        spot_symbol = spot_map.get(symbol, symbol)
        spot_quote = adapter.quote("NSE", [spot_symbol])
        # spot_quote is dict like: {'NSE:NIFTY 50': {'last_price': 24563.2, ...}}
        # We need to extract the inner dict
        inner_key = f"NSE:{spot_symbol}"
        spot_price_data = spot_quote.get(inner_key, {}) if isinstance(spot_quote, dict) else {}
        spot_price = float(_safe_getquote_dict(spot_price_data, "last_price", 0.0))

        if spot_price == 0.0:
            logger.error(f"Could not fetch spot price for {spot_symbol}")
            return _chain_failure("empty", f"spot price unavailable for {spot_symbol}",
                                  raise_errors)

        # 2. Fetch Instruments
        try:
            instruments = adapter.instruments("NFO")
        except Exception as e:
            logger.warning(f"Failed to fetch instruments list: {e}")
            return _chain_failure("rate_limited" if _is_rate_limited(e) else "instruments",
                                  f"{type(e).__name__}: {e}", raise_errors)

        # 3. Filter for symbol (name must match)
        options = [i for i in instruments if i.get("name") == symbol
                   and i.get("instrument_type") in ["CE", "PE"]]

        if not options:
            logger.warning(f"No option instruments found for underlying: {symbol}")
            return _chain_failure("empty", f"no option instruments for {symbol}",
                                  raise_errors)

        # 4. Group by Expiry
        expiries = sorted(list(set(i.get("expiry") for i in options)))
        if max_expiries is not None:
            limit = max(1, int(max_expiries))
            ist_today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date()

            def _day(value: Any) -> Any:
                if isinstance(value, datetime):
                    return value.date()
                if isinstance(value, date):
                    return value
                return ist_today

            upcoming = [e for e in expiries if _day(e) >= ist_today] or expiries
            expiries = upcoming[:limit]
            selected = set(expiries)
            options = [i for i in options if i.get("expiry") in selected]

        if max_strike_distance_pct is not None and spot_price > 0:
            band = float(spot_price) * abs(float(max_strike_distance_pct))
            if band > 0:
                near = []
                for instrument in options:
                    try:
                        strike = float(instrument.get("strike") or 0.0)
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if abs(strike - float(spot_price)) <= band:
                        near.append(instrument)
                # Never return a filtered-to-nothing chain; the caller will
                # fail closed on an empty strike set either way, but keeping
                # the full list makes that diagnostic explicit.
                if near:
                    options = near
                    kept = {item.get("expiry") for item in options}
                    expiries = [e for e in expiries if e in kept]

        chain = {
            "underlying": spot_price,
            "symbol": symbol,
            "expiries": [e.strftime("%d-%b-%Y") for e in expiries],
            "by_expiry": {}
        }

        # 5. Fetch all quotes in batches to be efficient and stay within limits
        all_symbols = [f"NFO:{i['tradingsymbol']}" for i in options]
        quotes: Dict[str, Any] = {}

        # Pace every Kite quote call.  A persistent ORB signal refetching the
        # chain every 10s used to re-arm the rate limiter with unspaced bursts
        # (30 "Too many requests" errors on 2026-11-20).
        batch_size = _quote_batch_size(quote_batch_size)
        spacing = _quote_spacing_seconds(quote_spacing_seconds)
        for i in range(0, len(all_symbols), batch_size):
            batch = all_symbols[i:i + batch_size]
            _pace_quote(spacing)
            try:
                batch_quotes = adapter.quote("NFO", [s.split("NFO:")[1] for s in batch])
                if isinstance(batch_quotes, dict):
                    quotes.update(batch_quotes)
            except Exception as e:
                # A partially quoted chain silently narrows the strike set and
                # can produce a plan from stale/empty legs.  Abort and let the
                # router fall back to the other provider instead.
                logger.warning(
                    "Quote fetch batch failed (%d-%d of %d): %s; aborting Zerodha chain.",
                    i, min(i + batch_size, len(all_symbols)), len(all_symbols), e)
                return _chain_failure(
                    "rate_limited" if _is_rate_limited(e) else "quote_failed",
                    f"{type(e).__name__}: {e}", raise_errors)

        # 6. Build structure per expiry/strike
        for exp in expiries:
            exp_str = exp.strftime("%d-%b-%Y")
            exp_opts = [i for i in options if i.get("expiry") == exp]
            strikes = sorted(list(set(i.get("strike") for i in exp_opts)))

            rows = []
            for strike in strikes:
                row = {
                    "strike": strike,
                    "expiry": exp_str,
                    "CE": {},
                    "PE": {}
                }

                for t in ["CE", "PE"]:
                    inst = next((i for i in exp_opts
                                if i["strike"] == strike and i["instrument_type"] == t), None)
                    if inst:
                        q = quotes.get(f"NFO:{inst['tradingsymbol']}", {})
                        # Convert Zerodha timestamp to IST ISO format for planner
                        qts = _convert_to_ist_iso(q.get("timestamp"))
                        bid, ask = _quote_bid_ask(q)
                        # Zerodha uses "oi"; NSE-style rows use "openInterest".
                        oi = _safe_getquote_dict(q, "oi", None)
                        if oi is None:
                            oi = _safe_getquote_dict(q, "open_interest", 0.0)
                        row[t] = {
                            "lastPrice": _safe_getquote_dict(q, "last_price", 0.0),
                            "bidPrice": bid,
                            "askPrice": ask,
                            "openInterest": oi,
                            "volume": _safe_getquote_dict(q, "volume", 0.0),
                            "trading_symbol": inst["tradingsymbol"],
                            "quote_timestamp": qts,
                            "row_timestamp": qts,
                        }
                    # If inst is None, row[t] stays as {} (empty dict) - not an error

                rows.append(row)
            chain["by_expiry"][exp_str] = rows

        return chain

    except ChainUnavailable:
        raise
    except Exception as e:
        logger.error(f"Unexpected error fetching chain from Zerodha: {type(e).__name__}: {e}")
        return _chain_failure(
            "rate_limited" if _is_rate_limited(e) else "error",
            f"{type(e).__name__}: {e}", raise_errors)