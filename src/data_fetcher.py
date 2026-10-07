"""
TRIO — Data Fetcher

Fetches live and historical OHLCV data for configurable symbols and timeframes.
Primary source: yfinance (free, no key required).
Handles retries, rate limits, and missing data.

DISCLAIMER: Educational purposes only. Not financial advice.
"""

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

from src.utils import get_logger, load_config, retry_with_backoff, utc_now

logger = get_logger("data_fetcher")

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MarketData:
    """Container for fetched OHLCV market data."""
    symbol: str
    timeframe: str
    ohlcv: pd.DataFrame  # columns: Open, High, Low, Close, Volume
    latest_price: Optional[float] = None
    fetched_at: str = ""
    source: str = "yfinance"

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict (DataFrame converted to list of dicts)."""
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "ohlcv": self.ohlcv.reset_index().to_dict(orient="records"),
            "latest_price": self.latest_price,
            "fetched_at": self.fetched_at,
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# yfinance timeframe mapping
# ---------------------------------------------------------------------------

# yfinance requires 'period' appropriate for interval:
# 1m -> max 7d, 5m -> max 60d, 15m -> max 60d, 1h -> max 730d, 1d -> max many years
INTERVAL_TO_MAX_PERIOD = {
    "1m": "5d",
    "2m": "5d",
    "5m": "30d",
    "15m": "30d",
    "30m": "30d",
    "1h": "60d",
    "1d": "365d",
    "1wk": "730d",
}


# ---------------------------------------------------------------------------
# Fetcher implementations
# ---------------------------------------------------------------------------

@retry_with_backoff(max_retries=3, backoff_base=2.0)
def _fetch_yfinance(
    symbol: str,
    timeframe: str,
    period: Optional[str] = None,
) -> pd.DataFrame:
    """
    Fetch OHLCV data from yfinance.

    Args:
        symbol:    Ticker symbol (e.g., RELIANCE.NS).
        timeframe: Candle interval (e.g., 1m, 5m, 15m, 1h, 1d).
        period:    How far back to fetch (e.g., 5d, 30d). Auto-selected if None.

    Returns:
        DataFrame with columns [Open, High, Low, Close, Volume].
    """
    try:
        import yfinance as yf
    except ImportError:
        raise ImportError("Install yfinance: pip install yfinance")

    if period is None:
        period = INTERVAL_TO_MAX_PERIOD.get(timeframe, "30d")

    logger.info("Fetching %s %s candles for period %s via yfinance", symbol, timeframe, period)

    ticker = yf.Ticker(symbol)
    df = ticker.history(period=period, interval=timeframe)

    if df.empty:
        raise ValueError(f"No data returned for {symbol} ({timeframe}, {period})")

    # Normalize column names
    df.columns = [c.title() for c in df.columns]

    # Keep only OHLCV
    required = ["Open", "High", "Low", "Close", "Volume"]
    for col in required:
        if col not in df.columns:
            raise ValueError(f"Missing column '{col}' in yfinance response for {symbol}")

    df = df[required].copy()

    # Drop rows where all OHLC are NaN
    df.dropna(subset=["Open", "High", "Low", "Close"], how="all", inplace=True)

    # Forward-fill minor gaps (e.g., 1–2 missing candles)
    df.ffill(inplace=True)

    logger.info("Fetched %d candles for %s", len(df), symbol)
    return df


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Timeframes yfinance only serves for the last ~60 days. Requesting a
# longer period for these returns nothing and the fetch fails.
INTRADAY_INTERVALS = {"1m", "2m", "5m", "15m", "30m", "1h", "90m"}


def fetch_market_data(
    symbol: str,
    timeframe: Optional[str] = None,
    period: Optional[str] = None,
) -> MarketData:
    """
    Fetch market data for a single symbol.

    Args:
        symbol:    Ticker symbol.
        timeframe: Candle timeframe. Defaults to config value.
        period:    History period. Defaults to config value. When omitted,
                   intraday bars use `intraday_history_period` (59d) because
                   yfinance rejects longer ranges for sub-daily intervals.

    Returns:
        MarketData object with OHLCV DataFrame and metadata.
    """
    cfg = load_config()
    df_cfg = cfg.get("data_fetcher", {})
    tf_cfg = cfg.get("timeframes", {})

    if timeframe is None:
        timeframe = tf_cfg.get("default", "15m")
    if period is None:
        if timeframe in INTRADAY_INTERVALS:
            period = df_cfg.get("intraday_history_period", "59d")
        else:
            period = df_cfg.get("history_period")

    source = df_cfg.get("source", "yfinance")

    if source == "yfinance":
        df = _fetch_yfinance(symbol, timeframe, period)
    else:
        raise ValueError(f"Unsupported data source: {source}")

    latest = float(df["Close"].iloc[-1]) if not df.empty else None

    return MarketData(
        symbol=symbol,
        timeframe=timeframe,
        ohlcv=df,
        latest_price=latest,
        fetched_at=utc_now(),
        source=source,
    )


def fetch_multiple(
    symbols: Optional[List[str]] = None,
    timeframe: Optional[str] = None,
) -> Dict[str, MarketData]:
    """
    Fetch market data for multiple symbols.

    Prefers a single yfinance batch (yf.download) when available and
    ``data_fetcher.use_batch_fetch`` is not explicitly disabled — one
    HTTP round-trip replaces N per-symbol fetches. The per-batch frame
    is sliced per-ticker and normalized, so callers and error handling
    stay identical. Falls back to a per-symbol loop when batch fetch
    is unavailable or the downloaded frame is empty.

    Rate limits: ~1000 tickers is fine, but a 100-name nightly scan
    still bursts once. A 59d/2y lookback on sub-daily bars is bounded
    above; a failed ticker is skipped, never fatal.
    """
    if symbols is None:
        cfg = load_config()
        symbols = cfg.get("symbols", [])

    if not symbols:
        return {}

    # Degenerate / fallback path: per-symbol fetch (cached, rate-safe).
    def _per_symbol() -> Dict[str, MarketData]:
        out: Dict[str, MarketData] = {}
        for sym in symbols:
            try:
                out[sym] = fetch_market_data(sym, timeframe)
            except Exception as exc:
                logger.error("Failed to fetch %s: %s", sym, exc)
        logger.info("Fetched data for %d / %d symbols", len(out), len(symbols))
        return out

    # Small baskets: defer to _per_symbol so callers with patched
    # fetch_market_data / capped fakes (tests, day_session) behave
    # identically regardless of runner cache layout.
    cfg = load_config()
    df_cfg = cfg.get("data_fetcher", {})
    use_batch = df_cfg.get("use_batch_fetch", True) is not False
    if not use_batch or len(symbols) <= 5:
        return _per_symbol()

    tf_cfg = cfg.get("timeframes", {})
    tf = timeframe or tf_cfg.get("default", "15m")
    period = None
    if tf in INTRADAY_INTERVALS:
        period = df_cfg.get("intraday_history_period", "59d")
    else:
        period = df_cfg.get("history_period", "2y")
    # Allow overrides already used by fetch_market_data(period=...)
    # Trigger premarket (history period set by caller via screen → fetch_market_data)
    # to reuse the configured value rather than a blank default.
    try:
        import yfinance as yf  # noqa: WPS433 - optional at import time
        import pandas as pd  # noqa: WPS433

        batch = yf.download(
            tickers=symbols,
            period=period,
            interval=tf,
            group_by="ticker",
            threads=True,
            progress=False,
        )
        # Single-ticker shape is flat; skip batch parse and defer to _per_symbol.
        if batch is None or batch.empty:
            logger.warning("Batch download empty; falling back to per-symbol")
            return _per_symbol()
        # yfinance returns flat cols for 1 ticker, MultiIndex for many.
        is_multi = isinstance(batch.columns, pd.MultiIndex)
        results: Dict[str, MarketData] = {}
        for sym in symbols:
            try:
                if is_multi:
                    if sym not in batch.columns.get_level_values(0):
                        raise KeyError(f"{sym} missing from batch")
                    raw = batch[sym].copy()
                else:
                    # Single-ticker frame already filtered to requested sym
                    raw = batch.copy()
                # Normalize: yfinance uses lower-case in some versions
                raw.columns = [c.title() for c in raw.columns]
                required = ["Open", "High", "Low", "Close", "Volume"]
                for col in required:
                    if col not in raw.columns:
                        raise ValueError(f"Missing {col} in batch for {sym}")
                df = raw[required].copy()
                df.dropna(subset=["Open", "High", "Low", "Close"], how="all", inplace=True)
                df.ffill(inplace=True)
                if df.empty:
                    raise ValueError(f"Empty frame for {sym}")
                latest = float(df["Close"].iloc[-1]) if not df.empty else None
                results[sym] = MarketData(
                    symbol=sym, timeframe=tf, ohlcv=df,
                    latest_price=latest, fetched_at=utc_now(), source="yfinance",
                )
            except Exception as exc:
                # One-ticker failure must not kill the other 99.
                logger.warning("Batch fetch of %s failed (%s); retrying per-symbol", sym, exc)
                try:
                    results[sym] = fetch_market_data(sym, timeframe)
                except Exception as exc2:
                    logger.error("Failed to fetch %s: %s", sym, exc2)
        logger.info("Fetched data for %d / %d symbols", len(results), len(symbols))
        return results
    except ImportError:
        return _per_symbol()
    except Exception as exc:
        logger.warning("Batch download failed (%s); falling back to per-symbol", exc)
        return _per_symbol()


def get_latest_price(symbol: str) -> Optional[float]:
    """Quick helper to get just the latest closing price for a symbol."""
    try:
        md = fetch_market_data(symbol, timeframe="1d", period="5d")
        return md.latest_price
    except Exception as exc:
        logger.error("Could not get latest price for %s: %s", symbol, exc)
        return None
