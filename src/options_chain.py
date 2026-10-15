"""
TRIO — NSE Options Chain Fetcher
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

Free chain source: NSE public API (option-chain-indices?symbol=NIFTY).
Cookie session required; cloud IPs get blocked often — every failure is
fail-closed to (Kite chain if keys exist, else None) so the caller falls
back to the Black-Scholes sim in options_pricing.py.

Chain row per strike: CE/PE {lastPrice, bidPrice, askPrice, openInterest,
changeinOpenInterest, impliedVolatility, totalTradedVolume}.

Cache: output/options_chain_<SYMBOL>.json, TTL from config
`options.chain_cache_minutes` (default 15 intraday).

DISCLAIMER: Educational purposes only. Not financial advice.
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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

TIMEOUT = 20
RETRIES = 2
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHAIN_CACHE_DIR = PROJECT_ROOT / "output" / "chain_cache"
CHAIN_DIAGNOSTIC_DIR = PROJECT_ROOT / "output" / "chain_diagnostics"


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
        r = s.get(NSE_BASE + NSE_EXPIRY_PATH.format(symbol=symbol),
                  timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        return list((r.json() or {}).get("expiryDates") or [])
    except Exception:
        return []


def fetch_chain(symbol: str = "NIFTY",
                use_cache: bool = True,
                diagnostic_label: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Fetch the full NSE option chain for an index symbol.

    Returns {"underlying": float, "expiries": [...], "by_expiry": {expiry:
    [strike rows...]}} or None when blocked/unreachable (caller falls back
    to BS sim). Never raises.
    """
    symbol = (symbol or "NIFTY").upper()
    cfg = {}
    try:
        cfg = load_config().get("options", {}) or {}
    except Exception:
        pass
    ttl = int(cfg.get("chain_cache_minutes", 15))

    if use_cache:
        hit = _cache_fresh(symbol, ttl)
        if hit is not None:
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
            s = _session()
            if not _warm_cookies(s):
                raise RuntimeError("NSE cookie warmup failed (likely blocked)")
            expiries = _expiries_for(s, symbol)
            # Fetch per-expiry via v3 (the shape NSE serves today), then the
            # legacy indices path as a last resort.
            paths: List[tuple[str, str]] = []
            for exp in (expiries[:4] or [""]):
                paths.append((
                    "/api/option-chain-v3?type=Indices&symbol={s}&expiry={e}"
                    .format(s=symbol, e=exp) if exp else
                    "/api/option-chain-v3?type=Indices&symbol={s}".format(s=symbol),
                    exp,
                ))
            paths.append(("/api/option-chain-indices?symbol={s}".format(s=symbol), ""))
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
                raise RuntimeError("all NSE chain paths 404/blocked")
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
                "requested_expiries": [
                    item for item in expiries[:4] if item
                ],
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
                exp: expiry_status(chain, exp) for exp in expiries[:4]
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
            time.sleep(2 + attempt * 2)
        except Exception as exc:
            last = exc
            logger.warning("NSE chain fetch failed (attempt %d): %s",
                           attempt + 1, exc)
            time.sleep(1 + attempt)
    logger.error("NSE chain unavailable for %s: %s", symbol, last)
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
