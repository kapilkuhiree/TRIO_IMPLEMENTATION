"""Read-only Zerodha Kite market-data adapter.

This module deliberately has no order methods.  It is an optional data source
for candle, instrument, and quote validation while MegaBull remains the TRIO
remote-paper execution broker.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import os
from typing import Any, Dict, Iterable, List, Optional

from src.utils import get_env, get_logger


logger = get_logger("zerodha_market_data")
IST = timezone(timedelta(hours=5, minutes=30))


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
        if exchange not in self._instrument_cache:
            rows = self.kite.instruments(exchange) or []
            self._instrument_cache[exchange] = [dict(row) for row in rows]
        return list(self._instrument_cache[exchange])

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
        exchange = "NSE"
        symbol = self._equity_symbol(symbol)
        return self.historical(exchange, symbol, interval, period)

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


__all__ = ["ZerodhaMarketData", "read_only_smoke"]
