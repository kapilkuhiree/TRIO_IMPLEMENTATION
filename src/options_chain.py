"""
TRIO — NSE Options Chain Fetcher
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

Free chain source: NSE public API (index and individual equity options).
Cookie session required; cloud IPs get blocked often. Failure returns None;
this module never substitutes synthetic data. Strict use_cache=False also
requires response-side underlying identity and never reads cached chains.

Chain row per strike: CE/PE {lastPrice, bidPrice, askPrice, openInterest,
changeinOpenInterest, impliedVolatility, totalTradedVolume}.

Cache: output/options_chain_<SYMBOL>.json, TTL from config
`options.chain_cache_minutes` (default 15 intraday).

DISCLAIMER: Educational purposes only. Not financial advice.
"""

import json
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

from src.utils import get_logger, load_config, utc_now

logger = get_logger("options_chain")

NSE_BASE = "https://www.nseindia.com"
# Live shape (probed 2026-10-08): option-chain-indices 404s from most IPs;
# option-chain-v3?type=Indices&symbol=NIFTY&expiry=.. works. We try v3 first,
# then contract-info, then the legacy indices path.
NSE_CHAIN_PATHS = [
    "/api/option-chain-v3?type=Indices&symbol={symbol}&expiry={expiry}",
    "/api/option-chain-v3?type=Indices&symbol={symbol}",
    "/api/option-chain-indices?symbol={symbol}",
]
NSE_EXPIRY_PATH = "/api/option-chain-contract-info?symbol={symbol}"
NSE_HOME = "https://www.nseindia.com/option-chain"
INDEX_SYMBOLS = frozenset({"NIFTY", "BANKNIFTY", "FINNIFTY", "NIFTYNXT50", "MIDCPNIFTY"})

TIMEOUT = 20
RETRIES = 2
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHAIN_CACHE_DIR = PROJECT_ROOT / "output" / "chain_cache"
CHAIN_DIAGNOSTIC_DIR = PROJECT_ROOT / "output" / "chain_diagnostics"


class ChainUnavailable(RuntimeError):
    """Typed provider failure that drives option-chain failover.

    ``reason`` is one of ``rate_limited``, ``instruments``, ``quote_failed``,
    ``empty`` or ``error``.  Only ``rate_limited`` arms the sticky cooldown
    that stops the process from re-hammering a throttled provider.
    """

    def __init__(self, provider: str, reason: str, detail: str = ""):
        self.provider = provider
        self.reason = reason
        self.detail = detail
        message = f"{provider}:{reason}"
        if detail:
            message += f": {detail}"
        super().__init__(message)


CHAIN_PROVIDER_MODES = frozenset({"auto", "zerodha", "nse"})
# Escalating cooldown for a throttled primary provider.  Without it the scan
# loop retries every 10s, which re-arms the rate limit and never recovers.
_PRIMARY_BACKOFF_MINUTES = (5, 10, 20, 30)
_PRIMARY_BLOCK_UNTIL: Optional[datetime] = None
_PRIMARY_FAILURE_STREAK = 0


def chain_provider_mode() -> str:
    """Resolve the chain provider policy.

    ``TRIO_OPTIONS_CHAIN_PROVIDER`` wins when set to auto/zerodha/nse.  The
    legacy ``TRIO_MARKET_DATA_PROVIDER=zerodha`` setting keeps its historical
    meaning (Zerodha only) so existing behaviour is unchanged unless opted in.
    """
    explicit = str(os.environ.get("TRIO_OPTIONS_CHAIN_PROVIDER", "")).strip().lower()
    if explicit in CHAIN_PROVIDER_MODES:
        return explicit
    if str(os.environ.get("TRIO_MARKET_DATA_PROVIDER", "")).strip().lower() == "zerodha":
        return "zerodha"
    return "nse"


# Live-chain request budget defaults.  A persistent ORB signal used to refetch
# the full chain every 10s (30 Kite "Too many requests" bursts and 1,021 NSE
# fallbacks on 2026-11-20).  Quote only the nearest expiries and a bounded
# strike band around spot; the planner never selects a far wing anyway.
DEFAULT_CHAIN_MAX_EXPIRIES = 2
DEFAULT_CHAIN_STRIKE_BAND_PCT = 0.10
DEFAULT_NSE_MAX_EXPIRIES = 2
DEFAULT_NSE_COOKIE_TTL_SECONDS = 600.0
DEFAULT_ZERODHA_QUOTE_BATCH = 100
DEFAULT_ZERODHA_QUOTE_SPACING = 0.25

# One warmed NSE session is reused across fetches.  Warming a brand-new cookie
# jar for every chain fetch doubled the request load during the 2026-11-20
# fallback storm; NSE blocks bare requests, so the warmup cannot be skipped,
# only amortised.
_NSE_SESSION: Optional["requests.Session"] = None
_NSE_SESSION_AT: Optional[float] = None


def _options_section() -> Dict[str, Any]:
    """The ``options`` config block, or an empty dict when unavailable."""
    try:
        return load_config().get("options", {}) or {}
    except Exception:
        return {}


def _finite_number(value: Any, default: float) -> float:
    """Coerce ``value`` to a finite float, else return ``default``."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _chain_budget() -> Dict[str, Any]:
    """Resolve the live-chain request budget from ``config/config.yaml``."""
    cfg = _options_section()
    expiries = int(max(1, _finite_number(
        cfg.get("chain_max_expiries"), DEFAULT_CHAIN_MAX_EXPIRIES)))
    band = _finite_number(cfg.get("chain_strike_band_pct"),
                          DEFAULT_CHAIN_STRIKE_BAND_PCT)
    if not 0 < band <= 1:
        band = DEFAULT_CHAIN_STRIKE_BAND_PCT
    batch = int(max(1, _finite_number(
        cfg.get("zerodha_quote_batch_size"), DEFAULT_ZERODHA_QUOTE_BATCH)))
    spacing = max(0.0, _finite_number(
        cfg.get("zerodha_quote_spacing_seconds"), DEFAULT_ZERODHA_QUOTE_SPACING))
    return {
        "max_expiries": expiries,
        "strike_band_pct": band,
        "quote_batch_size": batch,
        "quote_spacing_seconds": spacing,
    }


def _nse_max_expiries() -> int:
    cfg = _options_section()
    return int(max(1, _finite_number(
        cfg.get("nse_max_expiries"), DEFAULT_NSE_MAX_EXPIRIES)))


def _nse_cookie_ttl_seconds() -> float:
    cfg = _options_section()
    return max(0.0, _finite_number(
        cfg.get("nse_cookie_ttl_seconds"), DEFAULT_NSE_COOKIE_TTL_SECONDS))


def _primary_blocked_until() -> Optional[datetime]:
    return _PRIMARY_BLOCK_UNTIL


def _primary_is_blocked(now: Optional[datetime] = None) -> bool:
    if _PRIMARY_BLOCK_UNTIL is None:
        return False
    return (now or datetime.now(timezone.utc)) < _PRIMARY_BLOCK_UNTIL


def _note_primary_failure(reason: str) -> None:
    global _PRIMARY_BLOCK_UNTIL, _PRIMARY_FAILURE_STREAK
    if reason != "rate_limited":
        return
    index = min(_PRIMARY_FAILURE_STREAK, len(_PRIMARY_BACKOFF_MINUTES) - 1)
    minutes = _PRIMARY_BACKOFF_MINUTES[index]
    _PRIMARY_FAILURE_STREAK += 1
    _PRIMARY_BLOCK_UNTIL = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    logger.warning(
        "Zerodha chain throttled; using NSE for %d minutes (retry at %s)",
        minutes, _PRIMARY_BLOCK_UNTIL.isoformat())


def _reset_primary_failure() -> None:
    global _PRIMARY_BLOCK_UNTIL, _PRIMARY_FAILURE_STREAK
    _PRIMARY_BLOCK_UNTIL = None
    _PRIMARY_FAILURE_STREAK = 0


def reset_chain_provider_state() -> None:
    """Clear the sticky backoff and the warmed NSE cookie session."""
    _reset_primary_failure()
    _drop_nse_session()


# ---------------------------------------------------------------------------
# low-level session (cookie handshake, NSE blocks bare requests)
# ---------------------------------------------------------------------------

def _session() -> "requests.Session":
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": NSE_HOME,
    })
    return s


def _drop_nse_session() -> None:
    """Forget the warmed session so the next fetch redoes the handshake."""
    global _NSE_SESSION, _NSE_SESSION_AT
    _NSE_SESSION = None
    _NSE_SESSION_AT = None


def _nse_session(ttl_seconds: float) -> Optional["requests.Session"]:
    """A warmed NSE session, reused until ``ttl_seconds`` elapse.

    Returns ``None`` when the cookie handshake fails (typically a blocked IP),
    leaving no half-warmed session behind for the next caller.
    """
    global _NSE_SESSION, _NSE_SESSION_AT
    if (_NSE_SESSION is not None and _NSE_SESSION_AT is not None
            and ttl_seconds > 0
            and time.monotonic() - _NSE_SESSION_AT < ttl_seconds):
        return _NSE_SESSION
    _drop_nse_session()
    session = _session()
    if not _warm_cookies(session):
        return None
    _NSE_SESSION = session
    _NSE_SESSION_AT = time.monotonic()
    return session


def _warm_cookies(s: "requests.Session") -> bool:
    """GET the option-chain page so NSE sets akamai/bm cookies. False = blocked."""
    try:
        r = s.get(NSE_HOME, timeout=TIMEOUT)
        return r.status_code == 200 and bool(s.cookies)
    except Exception as exc:
        logger.warning("NSE cookie warmup failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

def _cache_path(symbol: str) -> Path:
    return CHAIN_CACHE_DIR / f"chain_{symbol.upper()}.json"


def _cache_fresh(symbol: str, ttl_minutes: int) -> Optional[Dict[str, Any]]:
    p = _cache_path(symbol)
    if not p.exists():
        return None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
        fetched = payload.get("fetched_at", "")
        dt = datetime.fromisoformat(str(fetched).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
        if age_min <= ttl_minutes:
            return payload.get("chain")
    except Exception:
        pass
    return None


def _cache_write(symbol: str, chain: Dict[str, Any]) -> None:
    try:
        CHAIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(symbol).write_text(
            json.dumps({"fetched_at": utc_now(), "chain": chain},
                       default=str), encoding="utf-8")
    except Exception as exc:
        logger.warning("Chain cache write failed for %s: %s", symbol, exc)


def _write_chain_diagnostic(symbol: str, diagnostic: Dict[str, Any]) -> None:
    """Persist a normalized, non-secret chain diagnostic for later replay.

    The diagnostic intentionally stores contract counts and validation
    outcomes, not credentials or broker data.  The plan also embeds the same
    structure, so the premarket artifact remains available when cache files
    are ignored by git.
    """
    try:
        CHAIN_DIAGNOSTIC_DIR.mkdir(parents=True, exist_ok=True)
        path = CHAIN_DIAGNOSTIC_DIR / f"chain_{symbol.upper()}_latest.json"
        path.write_text(json.dumps(diagnostic, indent=2, default=str),
                        encoding="utf-8")
    except Exception as exc:
        logger.warning("Chain diagnostic write failed for %s: %s", symbol, exc)


def expiry_status(chain: Dict[str, Any], expiry: str) -> Dict[str, Any]:
    """Return explicit exchange-listing and strike-availability status."""
    expiry = str(expiry or "")
    listed = expiry in set(chain.get("expiries", []) or [])
    rows = strikes_for(chain, expiry)
    ce_records = sum(1 for row in rows if row.get("CE"))
    pe_records = sum(1 for row in rows if row.get("PE"))
    if not listed:
        reason = "invalid_expiry"
    elif not rows:
        reason = "zero_strikes_for_expiry"
    else:
        reason = "usable_strikes"
    return {
        "expiry": expiry,
        "listed_by_exchange": listed,
        "strike_rows": len(rows),
        "ce_records": ce_records,
        "pe_records": pe_records,
        "usable": bool(rows and ce_records and pe_records),
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

def _num(x: Any) -> float:
    try:
        v = float(x)
        return v if v == v else 0.0
    except (TypeError, ValueError):
        return 0.0


def _exchange_timestamp(value: Any) -> Any:
    """Return an exchange-supplied timestamp without replacing it locally."""
    if value in (None, ""):
        return None
    return value


def _parse_strike_row(expiry: str, strike: Any,
                      row: Dict[str, Any],
                      record_timestamp: Any = None) -> Dict[str, Any]:
    """Normalize one NSE strike row to {strike, CE:{...}, PE:{...}}.

    v3 legs use buyPrice1/sellPrice1 for bid/ask (legacy used
    bidprice/askPrice) — accept both.
    """
    row_timestamp = _exchange_timestamp(
        row.get("row_timestamp") or row.get("timestamp")
        or row.get("quote_timestamp") or record_timestamp
    )
    out: Dict[str, Any] = {
        "strike": float(strike),
        "expiry": expiry,
        # These are exchange-record timestamps.  They intentionally do not
        # fall back to the local HTTP retrieval time.
        "row_timestamp": row_timestamp,
    }
    for side in ("CE", "PE"):
        leg = row.get(side) or {}
        quote_timestamp = _exchange_timestamp(
            leg.get("quote_timestamp") or leg.get("timestamp")
            or leg.get("row_timestamp") or row_timestamp
        )
        out[side] = {
            "lastPrice": _num(leg.get("lastPrice")),
            "bidPrice": _num(leg.get("bidPrice", leg.get("buyPrice1"))),
            "askPrice": _num(leg.get("askPrice", leg.get("sellPrice1"))),
            "openInterest": int(leg.get("openInterest") or 0),
            "changeOI": int(leg.get("changeinOpenInterest") or 0),
            "iv": _num(leg.get("impliedVolatility")),
            "volume": int(leg.get("totalTradedVolume") or 0),
            "quote_timestamp": quote_timestamp,
            "row_timestamp": row_timestamp,
        }
        if quote_timestamp is not None:
            out["quote_timestamp"] = quote_timestamp
    return out


def _expiries_for(s: "requests.Session", symbol: str) -> List[str]:
    """Expiry list via contract-info (probed live 2026-10-08)."""
    try:
        r = s.get(NSE_BASE + NSE_EXPIRY_PATH.format(symbol=quote(symbol, safe="")),
                  timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        return list((r.json() or {}).get("expiryDates") or [])
    except Exception:
        return []


def _symbol_identity(value: Any) -> str:
    """Canonical NSE underlying, without inferring an index from a stock."""
    return str(value or "").strip().upper().removesuffix(".NS").removesuffix("IDX")


def _response_identity_failure(data: Dict[str, Any], symbol: str,
                               require_identity: bool) -> Optional[str]:
    """Validate headers, rows and both legs before normalization loses identity.

    NSE commonly supplies the symbol only in CE/PE ``underlying`` fields.
    Strict callers require evidence for every quote, inherited from a header
    or row when available. Legacy cache-enabled callers may accept omitted
    identity, but explicit contradictory identity is never relabelled.
    """
    def identities(item: Dict[str, Any]) -> List[str]:
        return [_symbol_identity(item[key])
                for key in ("symbol", "underlying_symbol", "underlyingSymbol", "underlying")
                if isinstance(item.get(key), str) and item[key].strip()]

    rec = data.get("records") or {}
    filtered = data.get("filtered") or {}
    headers = identities(data) + identities(rec) + identities(filtered)
    for identity in headers:
        if identity != symbol:
            return f"symbol_mismatch:{identity}"
    raw_rows = rec.get("data") or filtered.get("data") or data.get("data") or []
    if not raw_rows and require_identity and not headers:
        return "missing_symbol_identity"
    for row in raw_rows:
        row_ids = identities(row)
        for identity in row_ids:
            if identity != symbol:
                return f"symbol_mismatch:{identity}"
        evidence = bool(headers or row_ids)
        legs = [row[side] for side in ("CE", "PE") if row.get(side)]
        for leg in legs:
            leg_ids = identities(leg)
            for identity in leg_ids:
                if identity != symbol:
                    return f"symbol_mismatch:{identity}"
            if require_identity and not (evidence or leg_ids):
                return "missing_symbol_identity"
        if require_identity and not legs and not evidence:
            return "missing_symbol_identity"
    return None


def fetch_chain(symbol: str = "NIFTY",
                use_cache: bool = True,
                diagnostic_label: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Fetch an NSE index or actual stock-underlying option chain.

    Returns {"underlying": float, "expiries": [...], "by_expiry": {expiry:
    [strike rows...]}} or None when blocked/unreachable/identity-invalid.
    ``use_cache=False`` bypasses all cached data and requires exchange-side
    symbol identity. No simulated quotes or alternate underlyings are used.
    """
    symbol = _symbol_identity(symbol or "NIFTY")
    mode = chain_provider_mode()
    order: List[str] = []
    if mode in ("auto", "zerodha"):
        order.append("zerodha")
    if mode in ("auto", "nse"):
        order.append("nse")

    failures: List[str] = []
    now = datetime.now(timezone.utc)
    for provider in order:
        if provider == "zerodha" and mode == "auto" and _primary_is_blocked(now):
            # Sticky skip: don't spend a request on a provider we know is
            # throttled, and don't prolong its block with retry storms.
            failures.append("zerodha:rate_limited(backoff)")
            continue
        if provider == "zerodha":
            chain = _fetch_chain_zerodha(symbol, failures)
        else:
            chain = _fetch_chain_nse(symbol, use_cache, diagnostic_label)
        if chain:
            _annotate_chain_provider(chain, provider, failures)
            return chain
        if not any(item.startswith(f"{provider}:") for item in failures):
            failures.append(f"{provider}:unavailable")

    logger.error("Option chain unavailable for %s (mode=%s): %s",
                 symbol, mode, "; ".join(dict.fromkeys(failures)))
    return None


def _fetch_chain_zerodha(symbol: str, failures: List[str]) -> Optional[Dict[str, Any]]:
    try:
        from src.options_chain_zerodha import fetch_zerodha_chain
        logger.info("Using Zerodha provider for %s chain", symbol)
        budget = _chain_budget()
        chain = fetch_zerodha_chain(
            symbol,
            raise_errors=True,
            max_expiries=budget["max_expiries"],
            max_strike_distance_pct=budget["strike_band_pct"],
            quote_batch_size=budget["quote_batch_size"],
            quote_spacing_seconds=budget["quote_spacing_seconds"],
        )
    except ChainUnavailable as exc:
        failures.append(f"zerodha:{exc.reason}")
        logger.warning("Zerodha chain unavailable (%s): %s", exc.reason, exc.detail)
        _note_primary_failure(exc.reason)
        return None
    except Exception as exc:  # noqa: BLE001 - failover must never propagate
        failures.append("zerodha:error")
        logger.warning("Zerodha chain failed: %s", exc)
        return None
    if chain:
        _reset_primary_failure()
    return chain


def _annotate_chain_provider(chain: Dict[str, Any], provider: str,
                             failures: List[str]) -> None:
    retrieval = dict(chain.get("retrieval") or {})
    retrieval["requested_mode"] = chain_provider_mode()
    retrieval["provider"] = provider
    if failures:
        retrieval["fallback_reason"] = failures[0]
    blocked = _primary_blocked_until()
    if blocked is not None:
        retrieval["primary_retry_at"] = blocked.isoformat()
    chain["retrieval"] = retrieval
    chain["provider"] = provider
    if not chain.get("source"):
        chain["source"] = provider
    if provider != "zerodha" and failures:
        logger.warning("Chain for %s served by %s (fallback: %s)",
                       chain.get("symbol", ""), provider, failures[0])


def _fetch_chain_nse(symbol: str, use_cache: bool,
                     diagnostic_label: Optional[str] = None
                     ) -> Optional[Dict[str, Any]]:
    """NSE public-API chain fetch (original implementation)."""
    cfg = _options_section()
    ttl = int(cfg.get("chain_cache_minutes", 15))
    max_expiries = _nse_max_expiries()
    session_ttl = _nse_cookie_ttl_seconds()

    if use_cache:
        hit = _cache_fresh(symbol, ttl)
        if hit is not None and _symbol_identity(hit.get("symbol") or symbol) == symbol:
            hit = dict(hit)
            hit.setdefault("symbol", symbol)
            retrieval = {
                **(hit.get("retrieval") or {}),
                "served_from_cache": True,
                "cache_ttl_minutes": ttl,
            }
            hit["retrieval"] = retrieval
            cached_diag = dict(hit.get("diagnostics") or {})
            cached_diag["retrieval"] = retrieval
            cached_diag.setdefault("symbol", symbol)
            _write_chain_diagnostic(symbol, cached_diag)
            return hit

    last: Exception = RuntimeError("unreachable")
    for attempt in range(RETRIES + 1):
        try:
            s = _nse_session(session_ttl)
            if s is None:
                raise RuntimeError("NSE cookie warmup failed (likely blocked)")
            expiries = _expiries_for(s, symbol)
            requested = [e for e in expiries[:max_expiries] if e]
            # Known indices and actual stocks have different NSE endpoints.
            chain_type = "Indices" if symbol in INDEX_SYMBOLS else "Equity"
            legacy_kind = "indices" if symbol in INDEX_SYMBOLS else "equities"
            encoded_symbol = quote(symbol, safe="")
            paths: List[tuple[str, str]] = []
            for exp in (requested or [""]):
                paths.append((
                    "/api/option-chain-v3?type={t}&symbol={s}&expiry={e}"
                    .format(t=chain_type, s=encoded_symbol, e=quote(exp, safe="")) if exp else
                    "/api/option-chain-v3?type={t}&symbol={s}".format(t=chain_type, s=encoded_symbol),
                    exp,
                ))
            paths.append((f"/api/option-chain-{legacy_kind}?symbol={encoded_symbol}", ""))
            responses: List[tuple[Dict[str, Any], str]] = []
            response_diagnostics: List[Dict[str, Any]] = []
            for path, requested_expiry in paths:
                r = s.get(NSE_BASE + path, timeout=TIMEOUT)
                response_diag = {
                    "requested_expiry": requested_expiry or None,
                    "path": path.split("?")[0],
                    "http_status": r.status_code,
                    "success": False,
                    "strike_rows": 0,
                    "ce_records": 0,
                    "pe_records": 0,
                    "validation_failures": [],
                }
                if r.status_code in (401, 403):
                    response_diag["failure_reason"] = "access_denied"
                    response_diagnostics.append(response_diag)
                    continue
                if r.status_code == 404:
                    response_diag["failure_reason"] = "endpoint_not_found"
                    response_diagnostics.append(response_diag)
                    continue
                try:
                    r.raise_for_status()
                except Exception as exc:
                    response_diag["failure_reason"] = f"http_error: {exc}"
                    response_diagnostics.append(response_diag)
                    continue
                try:
                    data = r.json()
                except Exception as exc:
                    response_diag["failure_reason"] = f"invalid_json: {exc}"
                    response_diagnostics.append(response_diag)
                    continue
                if not isinstance(data, dict):
                    response_diag["failure_reason"] = "invalid_response_shape"
                    response_diagnostics.append(response_diag)
                    continue
                identity_failure = _response_identity_failure(
                    data, symbol, require_identity=not use_cache)
                if identity_failure:
                    response_diag["failure_reason"] = identity_failure
                    response_diag["validation_failures"].append(identity_failure)
                    response_diagnostics.append(response_diag)
                    continue
                if (data.get("records") or data.get("filtered")
                        or data.get("data")):
                    responses.append((data, requested_expiry))
                    response_diag["success"] = True
                    response_diag["raw_expiry_dates"] = list(
                        (data.get("records") or {}).get("expiryDates") or [])
                    response_diagnostics.append(response_diag)
                    # A response without an expiry parameter is an aggregate
                    # fallback; do not make more duplicate requests after it.
                    if not requested_expiry:
                        break
                else:
                    response_diag["failure_reason"] = "empty_response"
                    response_diagnostics.append(response_diag)
            if not responses:
                failures = ", ".join(dict.fromkeys(
                    item.get("failure_reason", "unavailable") for item in response_diagnostics))
                raise RuntimeError(f"all NSE chain paths unavailable: {failures}")
            underlying = 0.0
            by_expiry: Dict[str, List[Dict[str, Any]]] = {}
            all_expiries = list(expiries or [])
            for data, requested_expiry in responses:
                rec = data.get("records", {}) or {}
                underlying = float(rec.get("underlyingValue") or underlying or 0.0)
                all_expiries.extend(list(rec.get("expiryDates") or []))
                raw_rows = (rec.get("data")
                            or (data.get("filtered", {}) or {}).get("data")
                            or data.get("data") or [])
                record_timestamp = (
                    rec.get("timestamp") or rec.get("quote_timestamp")
                    or data.get("timestamp") or data.get("quote_timestamp")
                )
                response_diag = next(
                    (item for item in reversed(response_diagnostics)
                     if item.get("requested_expiry") == (requested_expiry or None)
                     and item.get("success")),
                    None,
                )
                for row in raw_rows:
                    explicit_expiry = str(row.get("expiryDate") or "")
                    if (requested_expiry and explicit_expiry
                            and explicit_expiry != requested_expiry):
                        if response_diag is not None:
                            response_diag["validation_failures"].append(
                                f"expiry_mismatch:{explicit_expiry}")
                        continue
                    exp = explicit_expiry
                    if not exp:
                        eds = row.get("expiryDates") or ""
                        if isinstance(eds, str) and eds:
                            exp = eds
                        elif requested_expiry:
                            # A response made with an explicit expiry is
                            # authoritative when NSE omits the row field.  An
                            # aggregate response is not allowed to guess an
                            # expiry from the global list: doing so silently
                            # puts a strike under the wrong expiry key.
                            exp = requested_expiry
                    strike = row.get("strikePrice")
                    if not exp or strike is None:
                        if response_diag is not None:
                            response_diag["validation_failures"].append(
                                "missing_expiry_or_strike")
                        continue
                    try:
                        normalized = _parse_strike_row(
                            exp, strike, row,
                            record_timestamp=record_timestamp,
                        )
                        if normalized["strike"] <= 0:
                            raise ValueError("non-positive strike")
                    except (TypeError, ValueError) as exc:
                        if response_diag is not None:
                            response_diag["validation_failures"].append(str(exc))
                        continue
                    by_expiry.setdefault(exp, []).append(normalized)
                    if response_diag is not None:
                        response_diag["strike_rows"] += 1
                        response_diag["ce_records"] += int(bool(row.get("CE")))
                        response_diag["pe_records"] += int(bool(row.get("PE")))
            # Preserve order while removing duplicate expiry values returned by
            # each per-expiry response.
            expiries = list(dict.fromkeys(all_expiries))
            for rows in by_expiry.values():
                rows.sort(key=lambda x: x["strike"])
            retrieved_at = utc_now()
            diagnostic = {
                "schema_version": 1,
                "retrieved_at": retrieved_at,
                "symbol": symbol,
                "underlying": underlying,
                "requested_expiries": list(requested),
                "responses": response_diagnostics,
                "expiry_status": {},
                "freshness": {"ttl_minutes": ttl, "fresh": True},
            }
            exchange_timestamps = []
            for rows in by_expiry.values():
                for row in rows:
                    if row.get("quote_timestamp") not in (None, ""):
                        exchange_timestamps.append(row["quote_timestamp"])
            quote_timestamp = (
                exchange_timestamps[0] if exchange_timestamps and
                all(item == exchange_timestamps[0] for item in exchange_timestamps)
                else None
            )
            chain = {"underlying": underlying, "symbol": symbol,
                     "expiries": expiries, "by_expiry": by_expiry,
                     "source": "nse", "retrieved_at": retrieved_at,
                     "quote_timestamp": quote_timestamp,
                     "diagnostics": diagnostic}
            diagnostic["expiry_status"] = {
                exp: expiry_status(chain, exp) for exp in expiries[:max_expiries]
            }
            if diagnostic_label:
                diagnostic["label"] = diagnostic_label
            _write_chain_diagnostic(symbol, diagnostic)
            _cache_write(symbol, chain)
            logger.info("NSE chain %s: spot %.1f, %d expiries",
                        symbol, underlying, len(expiries))
            return chain
        except PermissionError as exc:
            last = exc
            logger.warning("NSE chain blocked (attempt %d): %s",
                           attempt + 1, exc)
            # The cookie jar is the thing that gets blocked; drop it so the
            # next attempt performs a fresh handshake.
            _drop_nse_session()
            time.sleep(2 + attempt * 2)
        except Exception as exc:
            last = exc
            logger.warning("NSE chain fetch failed (attempt %d): %s",
                           attempt + 1, exc)
            time.sleep(1 + attempt)
    logger.error("NSE chain unavailable for %s: %s", symbol, last)
    # Fallback to cache if available when live fetch fails.  Strict callers
    # (use_cache=False) must never be answered from disk: that path also skips
    # the response-side identity check, so a cached chain for a different
    # underlying could be traded.
    cached_fallback = _cache_path(symbol)
    if use_cache and cached_fallback.exists():
        try:
            payload = json.loads(cached_fallback.read_text(encoding="utf-8"))
            hit = payload.get("chain")
            if hit is not None:
                hit = dict(hit)
                hit.setdefault("symbol", symbol)
                retrieval = {
                    **(hit.get("retrieval") or {}),
                    "served_from_cache": True,
                    "cache_fallback": True,
                }
                hit["retrieval"] = retrieval
                logger.info("NSE chain unavailable live; successfully fell back to cached chain for %s", symbol)
                return hit
        except Exception as exc:
            logger.warning("Chain cache fallback failed for %s: %s", symbol, exc)
    _write_chain_diagnostic(symbol, {
        "schema_version": 1,
        "retrieved_at": utc_now(),
        "symbol": symbol,
        "retrieval": {"success": False, "served_from_cache": False},
        "failure_reason": str(last),
        "responses": [],
        "expiry_status": {},
        "freshness": {"ttl_minutes": ttl, "fresh": False},
    })
    return None


def nearest_expiry(chain: Dict[str, Any],
                   min_dte: int = 1) -> Optional[str]:
    """First expiry at least min_dte days out (weekly default)."""
    try:
        today = datetime.now(timezone.utc).date()
        cands = []
        for exp in chain.get("expiries", []) or []:
            # NSE format "12-Dec-2026" — also accept ISO
            dt = None
            for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(exp, fmt).date()
                    break
                except ValueError:
                    continue
            if dt is None:
                continue
            if (dt - today).days >= min_dte:
                cands.append((dt, exp))
        if not cands:
            exps = chain.get("expiries", []) or []
            return exps[0] if exps else None
        cands.sort()
        return cands[0][1]
    except Exception:
        exps = (chain or {}).get("expiries", []) or []
        return exps[0] if exps else None


def strikes_for(chain: Dict[str, Any], expiry: str) -> List[Dict[str, Any]]:
    """Strike rows for one expiry, sorted by strike. Empty list when missing."""
    try:
        return list((chain.get("by_expiry", {}) or {}).get(expiry, []) or [])
    except Exception:
        return []


def atm_strike(chain: Dict[str, Any], expiry: str) -> Optional[float]:
    """Strike nearest the underlying."""
    try:
        spot = float(chain.get("underlying") or 0.0)
        rows = strikes_for(chain, expiry)
        if not spot or not rows:
            return None
        return min((r["strike"] for r in rows), key=lambda k: abs(k - spot))
    except Exception:
        return None
