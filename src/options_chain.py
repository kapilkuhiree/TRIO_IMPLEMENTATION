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


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

def _num(x: Any) -> float:
    try:
        v = float(x)
        return v if v == v else 0.0
    except (TypeError, ValueError):
        return 0.0


def _parse_strike_row(expiry: str, strike: Any,
                      row: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize one NSE strike row to {strike, CE:{...}, PE:{...}}.

    v3 legs use buyPrice1/sellPrice1 for bid/ask (legacy used
    bidprice/askPrice) — accept both.
    """
    out: Dict[str, Any] = {"strike": float(strike), "expiry": expiry}
    for side in ("CE", "PE"):
        leg = row.get(side) or {}
        out[side] = {
            "lastPrice": _num(leg.get("lastPrice")),
            "bidPrice": _num(leg.get("bidPrice", leg.get("buyPrice1"))),
            "askPrice": _num(leg.get("askPrice", leg.get("sellPrice1"))),
            "openInterest": int(leg.get("openInterest") or 0),
            "changeOI": int(leg.get("changeinOpenInterest") or 0),
            "iv": _num(leg.get("impliedVolatility")),
            "volume": int(leg.get("totalTradedVolume") or 0),
        }
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
                use_cache: bool = True) -> Optional[Dict[str, Any]]:
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
            paths: List[str] = []
            for exp in (expiries[:4] or [""]):
                paths.append(
                    "/api/option-chain-v3?type=Indices&symbol={s}&expiry={e}"
                    .format(s=symbol, e=exp) if exp else
                    "/api/option-chain-v3?type=Indices&symbol={s}".format(s=symbol))
            paths.append("/api/option-chain-indices?symbol={s}".format(s=symbol))
            data = None
            for path in paths:
                r = s.get(NSE_BASE + path, timeout=TIMEOUT)
                if r.status_code in (401, 403):
                    continue
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                try:
                    data = r.json()
                except Exception:
                    continue
                if (data.get("records") or data.get("filtered")
                        or data.get("data")):
                    break
                data = None
            if data is None:
                raise RuntimeError("all NSE chain paths 404/blocked")
            rec = data.get("records", {}) or {}
            underlying = float(rec.get("underlyingValue") or 0.0)
            expiries = list(rec.get("expiryDates") or expiries or [])
            # v3 nests rows under records.data; per-strike rows carry
            # expiryDates (plural) but not expiryDate (singular) — the
            # requested expiry is the grouping key (probed live 2026-10-08).
            raw_rows = (rec.get("data")
                        or (data.get("filtered", {}) or {}).get("data")
                        or data.get("data") or [])
            by_expiry: Dict[str, List[Dict[str, Any]]] = {}
            for row in raw_rows:
                exp = str(row.get("expiryDate") or "")
                if not exp:
                    eds = row.get("expiryDates") or ""
                    exp = str(eds) if isinstance(eds, str) else ""
                strike = row.get("strikePrice")
                if not exp or strike is None:
                    continue
                by_expiry.setdefault(exp, []).append(
                    _parse_strike_row(exp, strike, row))
            # Per-expiry v3 calls return rows without an expiry tag at all
            # (only expiryDates on the row). Attribute them to the expiry
            # we asked for (first path's expiry).
            if not by_expiry and raw_rows:
                try:
                    asked = (expiries or [""])[0]
                except Exception:
                    asked = ""
                if asked:
                    for row in raw_rows:
                        strike = row.get("strikePrice")
                        if strike is None:
                            continue
                        by_expiry.setdefault(asked, []).append(
                            _parse_strike_row(asked, strike, row))
            for rows in by_expiry.values():
                rows.sort(key=lambda x: x["strike"])
            chain = {"underlying": underlying, "expiries": expiries,
                     "by_expiry": by_expiry, "source": "nse"}
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
