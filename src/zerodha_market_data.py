"""Read-only Zerodha Kite market-data adapter.

This module deliberately has no order methods.  It is an optional data source
for candle, instrument, and quote validation while MegaBull remains the TRIO
remote-paper execution broker.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import os
from pathlib import Path
import pickle
import threading
from typing import Any, Dict, Iterable, List, Optional

from src.utils import get_env, get_logger


logger = get_logger("zerodha_market_data")
IST = timezone(timedelta(hours=5, minutes=30))

# Kite instrument tokens for the indices TRIO scans.  Resolving these from the
# token map avoids the `instruments("NSE")` download on every candle request —
# that endpoint is aggressively rate limited and was the single cause of the
# 2026-11-06 "Too many requests" outage.
INDEX_TOKENS: Dict[str, int] = {
    "^NSEI": 256265, "NIFTY": 256265, "NIFTY50": 256265, "NIFTY 50": 256265,
    "^NSEBANK": 260105, "BANKNIFTY": 260105, "NIFTY BANK": 260105,
    "FINNIFTY": 257801, "NIFTY FIN SERVICE": 257801,
    "MIDCPNIFTY": 288009, "NIFTY MID SELECT": 288009,
    "NIFTYNXT50": 260113, "NIFTY NEXT 50": 260113,
}

# Instrument dumps are multi-megabyte and rate limited, so they are cached on
# disk for the whole trading day and shared by every caller in the process.
_INSTRUMENT_CACHE_DIR = (
    Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "TRIO" / "instruments"
)
_SHARED_ADAPTER: Optional["ZerodhaMarketData"] = None
_SHARED_ADAPTER_LOCK = threading.Lock()


def _instrument_cache_path(exchange: str) -> Path:
    stamp = datetime.now(IST).date().isoformat()
    return _INSTRUMENT_CACHE_DIR / f"{exchange.upper()}-{stamp}.pkl"


def _load_cached_instruments(exchange: str) -> Optional[List[Dict[str, Any]]]:
    path = _instrument_cache_path(exchange)
    if not path.exists():
        return None
    try:
        with path.open("rb") as handle:
            payload = pickle.load(handle)
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if not rows:
            return None
        return [dict(row) for row in rows]
    except Exception as exc:  # corrupt/incompatible cache must never be fatal
        logger.warning("Instrument cache read failed for %s: %s", exchange, exc)
        return None


def _save_cached_instruments(exchange: str,
                             rows: List[Dict[str, Any]]) -> None:
    try:
        _INSTRUMENT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _instrument_cache_path(exchange)
        temporary = path.with_suffix(".tmp")
        with temporary.open("wb") as handle:
            pickle.dump(
                {"exchange": exchange, "saved_at": datetime.now(IST).isoformat(),
                 "rows": rows},
                handle, protocol=pickle.HIGHEST_PROTOCOL)
        temporary.replace(path)
        # One dump per exchange per day is enough; drop older days.
        for stale in _INSTRUMENT_CACHE_DIR.glob(f"{exchange.upper()}-*.pkl"):
            if stale != path:
                try:
                    stale.unlink()
                except OSError:
                    pass
        logger.info("Instrument cache written for %s (%d rows)", exchange,
                    len(rows))
    except Exception as exc:
        logger.warning("Instrument cache write failed for %s: %s", exchange, exc)


def get_shared_adapter() -> "ZerodhaMarketData":
    """Return the process-wide adapter so caches survive across callers.

    A fresh adapter per call used to mean an empty instrument cache, so every
    scan redownloaded the full NSE/NFO dumps until Kite started returning
    ``Too many requests``.
    """
    global _SHARED_ADAPTER
    if _SHARED_ADAPTER is not None:
        return _SHARED_ADAPTER
    with _SHARED_ADAPTER_LOCK:
        if _SHARED_ADAPTER is None:
            _SHARED_ADAPTER = ZerodhaMarketData.from_env()
        return _SHARED_ADAPTER


def _period_days(period: Optional[str]) -> int:
    text = str(period or "30d").strip().lower()
    try:
        if text.endswith("d"):
            return max(1, int(float(text[:-1])))
        if text.endswith("mo"):
            return max(1, int(float(text[:-2]) * 30))
        if text.endswith("y"):
            return max(1, int(float(text[:-1]) * 365))
    except (TypeError, ValueError):
        pass
    return 30


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


class ZerodhaMarketData:
    """Small read-only wrapper around Kite Connect."""

    provider_name = "zerodha"

    def __init__(self, api_key: Optional[str] = None,
                 access_token: Optional[str] = None, kite: Any = None):
        if kite is not None:
            self.kite = kite
            self._instrument_cache: Dict[str, List[Dict[str, Any]]] = {}
            # An injected client is a test/DI seam: never let the real
            # on-disk dump leak into it.
            self._use_disk_instrument_cache = False
            return
        try:
            from kiteconnect import KiteConnect
        except ImportError as exc:
            raise RuntimeError(
                "kiteconnect is required for Zerodha read-only data") from exc
        api_key = (api_key or os.environ.get("ZERODHA_API_KEY") or "").strip()
        access_token = (access_token or
                        os.environ.get("ZERODHA_ACCESS_TOKEN") or "").strip()
        if not api_key or not access_token:
            raise RuntimeError(
                "ZERODHA_API_KEY and ZERODHA_ACCESS_TOKEN are required")
        self.kite = KiteConnect(api_key=api_key)
        self.kite.set_access_token(access_token)
        self._instrument_cache = {}
        self._use_disk_instrument_cache = (
            os.environ.get("TRIO_INSTRUMENT_CACHE", "1").strip().lower()
            not in {"0", "false", "no", "off"})

    @classmethod
    def from_env(cls) -> "ZerodhaMarketData":
        return cls(
            api_key=get_env("ZERODHA_API_KEY", required=True),
            access_token=get_env("ZERODHA_ACCESS_TOKEN", required=True),
        )

    def profile(self) -> Dict[str, Any]:
        """Return the authenticated profile; no credential fields are returned."""
        profile = dict(self.kite.profile() or {})
        return {
            "user_id": profile.get("user_id", ""),
            "user_name": profile.get("user_name", ""),
            "broker": profile.get("broker", ""),
            "exchanges": list(profile.get("exchanges", []) or []),
        }

    def instruments(self, exchange: str) -> List[Dict[str, Any]]:
        exchange = str(exchange).strip().upper()
        cached = self._instrument_cache.get(exchange)
        if cached is not None:
            return cached
        rows = (_load_cached_instruments(exchange)
                if getattr(self, "_use_disk_instrument_cache", False) else None)
        if rows is not None:
            logger.info("Instrument cache hit: %s (%d rows, disk)", exchange,
                        len(rows))
            self._instrument_cache[exchange] = rows
            return rows
        rows = [dict(row) for row in (self.kite.instruments(exchange) or [])]
        self._instrument_cache[exchange] = rows
        if getattr(self, "_use_disk_instrument_cache", False):
            _save_cached_instruments(exchange, rows)
        return rows

    @staticmethod
    def _equity_symbol(symbol: str) -> str:
        value = str(symbol or "").strip().upper()
        indices = {
            "^NSEI": "NIFTY 50", "NIFTY": "NIFTY 50", "NIFTY50": "NIFTY 50",
            "^NSEBANK": "NIFTY BANK", "BANKNIFTY": "NIFTY BANK",
            "FINNIFTY": "NIFTY FIN SERVICE", "NIFTY_FIN_SERVICE.NS": "NIFTY FIN SERVICE",
            "^NSMIDCP": "NIFTY NEXT 50", "NIFTYNXT50": "NIFTY NEXT 50",
            "MIDCPNIFTY": "NIFTY MID SELECT", "NIFTY_MID_SELECT.NS": "NIFTY MID SELECT",
        }
        if value in indices:
            return indices[value]
        return value.replace(".NS", "").replace(".BO", "")

    def find_instrument(self, exchange: str, trading_symbol: str,
                        *, instrument_type: Optional[str] = None) -> Dict[str, Any]:
        wanted = str(trading_symbol).strip().upper()
        rows = self.instruments(exchange)
        for row in rows:
            if str(row.get("tradingsymbol", "")).strip().upper() != wanted:
                continue
            if instrument_type and str(row.get("instrument_type", "")).upper() != str(instrument_type).upper():
                continue
            return row
        raise KeyError(f"Zerodha instrument not found: {exchange}:{trading_symbol}")

    def historical(self, exchange: str, trading_symbol: str,
                   interval: str = "5minute", period: str = "30d") -> List[Dict[str, Any]]:
        row = self.find_instrument(exchange, trading_symbol)
        end = datetime.now(IST)
        start = end - timedelta(days=_period_days(period))
        records = self.kite.historical_data(
            row["instrument_token"], start, end, interval)
        return [dict(item) for item in (records or [])]

    def historical_symbol(self, symbol: str, timeframe: str,
                          period: str = "30d") -> List[Dict[str, Any]]:
        intervals = {"1m": "minute", "5m": "5minute", "15m": "15minute",
                     "30m": "30minute", "1h": "60minute", "1d": "day"}
        interval = intervals.get(str(timeframe).lower())
        if interval is None:
            raise ValueError(f"Unsupported Zerodha timeframe: {timeframe}")
        raw = str(symbol or "").strip()
        token = INDEX_TOKENS.get(raw.upper())
        if token is None:
            token = INDEX_TOKENS.get(self._equity_symbol(raw).upper())
        if token is not None:
            end = datetime.now(IST)
            start = end - timedelta(days=_period_days(period))
            records = self.kite.historical_data(token, start, end, interval)
            return [dict(item) for item in (records or [])]
        return self.historical("NSE", self._equity_symbol(raw), interval, period)

    def find_option(self, underlying: str, expiry: Any, strike: float,
                    option_type: str) -> Dict[str, Any]:
        wanted_date = _as_datetime(expiry).date()
        wanted_type = str(option_type).strip().upper()
        rows = self.instruments("NFO")
        matches = []
        for row in rows:
            if str(row.get("name", "")).strip().upper() != str(underlying).upper():
                continue
            if str(row.get("instrument_type", "")).upper() != wanted_type:
                continue
            try:
                if _as_datetime(row.get("expiry")).date() != wanted_date:
                    continue
                if abs(float(row.get("strike")) - float(strike)) > 1e-8:
                    continue
            except (TypeError, ValueError, OverflowError):
                continue
            matches.append(row)
        if len(matches) != 1:
            raise KeyError(
                f"Zerodha option identity unresolved: {underlying} "
                f"{expiry} {strike} {wanted_type} ({len(matches)} matches)")
        return matches[0]

    def quote(self, exchange: str, trading_symbols: Iterable[str]) -> Dict[str, Any]:
        keys = [f"{str(exchange).upper()}:{str(symbol).strip()}"
                for symbol in trading_symbols]
        if not keys:
            return {}
        return dict(self.kite.quote(keys) or {})

    def option_quote(self, instrument: Dict[str, Any]) -> Dict[str, Any]:
        symbol = str(instrument.get("tradingsymbol", "")).strip()
        payload = self.quote("NFO", [symbol]).get(f"NFO:{symbol}", {}) or {}
        depth = payload.get("depth", {}) or {}
        buy = (depth.get("buy", []) or [])
        sell = (depth.get("sell", []) or [])
        return {
            "trading_symbol": symbol,
            "instrument_token": instrument.get("instrument_token"),
            "bid": (buy[0].get("price") if buy else None),
            "ask": (sell[0].get("price") if sell else None),
            "last_price": payload.get("last_price"),
            "open_interest": payload.get("oi"),
            "volume": payload.get("volume"),
            "timestamp": payload.get("timestamp"),
        }


def read_only_smoke() -> Dict[str, Any]:
    """Authenticate and retrieve small metadata samples without placing orders."""
    adapter = ZerodhaMarketData.from_env()
    profile = adapter.profile()
    nse = adapter.instruments("NSE")
    nfo = adapter.instruments("NFO")
    return {
        "provider": adapter.provider_name,
        "profile": profile,
        "nse_instruments": len(nse),
        "nfo_instruments": len(nfo),
        "orders_placed": 0,
    }


__all__ = ["ZerodhaMarketData", "get_shared_adapter", "read_only_smoke"]
