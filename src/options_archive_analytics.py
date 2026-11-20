"""Sidecar analytics for the read-only NFO option archive.

Why a sidecar
-------------
The premium archive (``options_archive.sqlite3``) is deliberately append-only
and can be several GB.  Nothing here writes to it.  Instead we build a small,
rebuildable ``options_analytics.sqlite3`` next to it containing:

* the underlying 5-minute spot series used for moneyness and IV,
* per-quote analytical fields (spot, DTE, moneyness, implied vol, spread,
  execution-quality flags),
* per-candle analytical fields (spot, DTE, moneyness, close-based IV).

The archive's own schema has no spot or IV columns, so this is the additive
layer that Phase 1 promised.  Deleting the sidecar loses nothing: it can be
rebuilt from the archive plus the cached spot pickles.

What is intentionally *not* done
--------------------------------
* No theoretical premium is written back into the archive.
* No quote is marked executable when it was not executable.
* IV is ``None`` when the price cannot be bracketed by Black-Scholes; it is
  never clamped into a plausible-looking number.
* No strategy parameter is tuned here.  This is measurement only.
"""

from __future__ import annotations

import bisect
import json
import shutil
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote as url_quote

from src.options_pricing import implied_vol

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
ROOT = Path(__file__).resolve().parent.parent
SUPPORTED_UNDERLYINGS = ("NIFTY", "BANKNIFTY")
DEFAULT_SPOT_PICKLES = {
    "NIFTY": ROOT / "output" / "orb_retest_5m_NSEI_60d.pkl",
    "BANKNIFTY": ROOT / "output" / "orb_retest_5m_NSEBANK_60d.pkl",
}
EXPIRY_CUTOFF = time(15, 30)
DEFAULT_STALE_SPOT_SECONDS = 600.0
DEFAULT_WIDE_SPREAD_PCT = 0.10


# ---------------------------------------------------------------------------
# Paths and small time helpers
# ---------------------------------------------------------------------------

def default_analytics_path(archive_path: Optional[Any] = None) -> Path:
    if archive_path is not None:
        return Path(archive_path).expanduser().with_name("options_analytics.sqlite3")
    from src.zerodha_option_archive import default_archive_path

    return default_archive_path().expanduser().with_name("options_analytics.sqlite3")


def _ms(value: datetime) -> int:
    return int(round(value.astimezone(UTC).timestamp() * 1000))


def _ist(ms: Optional[int]) -> Optional[datetime]:
    if ms is None:
        return None
    return datetime.fromtimestamp(int(ms) / 1000.0, IST)


def parse_day(value: Any) -> date:
    if isinstance(value, datetime):
        return value.astimezone(IST).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if "T" in text or " " in text:
        text = text.replace("Z", "+00:00")
        return datetime.fromisoformat(text).astimezone(IST).date()
    return date.fromisoformat(text)


def day_bounds_ms(day: Any) -> Tuple[int, int]:
    current = parse_day(day)
    start = datetime.combine(current, time.min, tzinfo=IST)
    end = datetime.combine(current, time(23, 59, 59, 999999), tzinfo=IST)
    return _ms(start), _ms(end)


def _parse_expiry(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except (TypeError, ValueError):
        return None


def time_to_expiry_years(timestamp_ms: int, expiry: Any) -> Optional[float]:
    """Calendar time from ``timestamp_ms`` to the expiry-day 15:30 IST close."""
    expiry_date = _parse_expiry(expiry)
    if expiry_date is None:
        return None
    expiry_dt = datetime.combine(expiry_date, EXPIRY_CUTOFF, tzinfo=IST)
    seconds = (expiry_dt.timestamp() * 1000.0 - float(timestamp_ms)) / 1000.0
    if seconds <= 0:
        return 0.0
    return seconds / (365.25 * 24.0 * 3600.0)


def dte_days(timestamp_ms: int, expiry: Any) -> Optional[float]:
    expiry_date = _parse_expiry(expiry)
    when = _ist(timestamp_ms)
    if expiry_date is None or when is None:
        return None
    return float((expiry_date - when.date()).days)


# ---------------------------------------------------------------------------
# Spot series
# ---------------------------------------------------------------------------

def load_spot_bars_from_pickle(path: Any, underlying: str) -> List[Dict[str, Any]]:
    """Load a cached yfinance-style pickle as IST 5-minute spot bars."""
    import pandas as pd  # local import keeps the pure-stdlib helpers light

    frame = pd.read_pickle(Path(path).expanduser())
    if frame is None or len(frame) == 0:
        return []
    bars: List[Dict[str, Any]] = []
    for index, row in frame.iterrows():
        stamp = pd.Timestamp(index)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("Asia/Kolkata")
        stamp = stamp.tz_convert("UTC")
        bars.append({
            "underlying": str(underlying).strip().upper(),
            "timestamp_ms": int(stamp.timestamp() * 1000),
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
            "source": str(path),
        })
    bars.sort(key=lambda item: item["timestamp_ms"])
    return bars


def _spot_series(connection: sqlite3.Connection,
                 underlyings: Sequence[str]) -> Dict[str, Tuple[List[int], List[float]]]:
    series: Dict[str, Tuple[List[int], List[float]]] = {}
    marks = ",".join("?" for _ in underlyings)
    rows = connection.execute(
        f"""SELECT underlying, timestamp_ms, close FROM spot_bars
             WHERE UPPER(underlying) IN ({marks})
             ORDER BY underlying, timestamp_ms""",
        [item.upper() for item in underlyings],
    ).fetchall()
    by_underlying: Dict[str, List[Tuple[int, float]]] = {}
    for row in rows:
        by_underlying.setdefault(str(row["underlying"]).upper(), []).append(
            (int(row["timestamp_ms"]), float(row["close"]))
        )
    for name, values in by_underlying.items():
        values.sort(key=lambda item: item[0])
        series[name] = ([item[0] for item in values], [item[1] for item in values])
    return series


def asof_spot(series: Mapping[str, Tuple[List[int], List[float]]],
              underlying: str,
              timestamp_ms: int,
              *,
              max_gap_seconds: float = 1800.0) -> Tuple[Optional[float], Optional[int], Optional[float]]:
    """Latest spot at or before ``timestamp_ms``.

    Returns ``(spot, spot_timestamp_ms, gap_seconds)``.  When the nearest bar
    is older than ``max_gap_seconds`` the spot is returned as ``None`` so the
    caller can flag the row rather than silently using a stale price.
    """
    key = str(underlying).strip().upper()
    series_values = series.get(key)
    if not series_values:
        return None, None, None
    stamps, closes = series_values
    index = bisect.bisect_right(stamps, int(timestamp_ms)) - 1
    if index < 0:
        return None, None, None
    spot_stamp = stamps[index]
    gap = (int(timestamp_ms) - spot_stamp) / 1000.0
    if gap > max_gap_seconds:
        return None, spot_stamp, gap
    return closes[index], spot_stamp, gap


# ---------------------------------------------------------------------------
# Feature derivation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FeatureConfig:
    stale_spot_seconds: float = DEFAULT_STALE_SPOT_SECONDS
    wide_spread_pct: float = DEFAULT_WIDE_SPREAD_PCT
    spot_max_gap_seconds: float = 1800.0


def derive_features(
    *,
    underlying: str,
    expiry: Any,
    strike: float,
    timestamp_ms: int,
    spot: Optional[float],
    spot_gap_seconds: Optional[float],
    bid: Optional[float] = None,
    ask: Optional[float] = None,
    last_price: Optional[float] = None,
    close: Optional[float] = None,
    executable: Optional[bool] = None,
    option_type: str = "CE",
    config: FeatureConfig = FeatureConfig(),
) -> Dict[str, Any]:
    """Compute one row of analytical fields and quality flags."""
    flags: List[str] = []
    price: Optional[float] = None
    spread_abs: Optional[float] = None
    spread_pct: Optional[float] = None

    if bid is not None or ask is not None:
        has_bid = bid is not None and float(bid) > 0
        has_ask = ask is not None and float(ask) > 0
        if has_bid and has_ask:
            bid_value, ask_value = float(bid), float(ask)
            spread_abs = ask_value - bid_value
            midpoint = (ask_value + bid_value) / 2.0
            price = midpoint if midpoint > 0 else None
            if midpoint > 0:
                spread_pct = spread_abs / midpoint
            if ask_value < bid_value:
                flags.append("crossed")
            if spread_pct is not None and spread_pct > config.wide_spread_pct:
                flags.append("wide_spread")
        else:
            if not has_bid:
                flags.append("zero_bid")
            if float(last_price or 0.0) > 0:
                price = float(last_price)
    elif close is not None and float(close) > 0:
        price = float(close)

    if executable is False:
        flags.append("non_executable")

    if spot is None or spot <= 0:
        flags.append("no_spot")
        spot_value = None
    else:
        spot_value = float(spot)
        if spot_gap_seconds is not None and spot_gap_seconds > config.stale_spot_seconds:
            flags.append("stale_spot")

    years = time_to_expiry_years(timestamp_ms, expiry)
    if years is None:
        flags.append("bad_expiry")
        years = None
    elif years <= 0:
        flags.append("expired")

    moneyness = None
    if spot_value is not None and strike:
        moneyness = (float(strike) - spot_value) / spot_value

    vol = None
    if (
        price is not None
        and price > 0
        and spot_value is not None
        and strike
        and years is not None
        and years > 0
        and str(option_type).upper() in {"CE", "PE"}
    ):
        vol = implied_vol(price, spot_value, float(strike), years,
                          str(option_type).upper())
        if vol is None:
            flags.append("iv_unavailable")

    return {
        "spot": spot_value,
        "spot_gap_seconds": spot_gap_seconds,
        "dte_days": dte_days(timestamp_ms, expiry),
        "dte_years": years,
        "moneyness": moneyness,
        "price": price,
        "spread_abs": spread_abs,
        "spread_pct": spread_pct,
        "implied_vol": vol,
        "flags": ",".join(sorted(set(flags))) if flags else "ok",
    }


# ---------------------------------------------------------------------------
# Sidecar schema and build
# ---------------------------------------------------------------------------

ANALYTICS_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 30000;
CREATE TABLE IF NOT EXISTS spot_bars (
    underlying TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    source TEXT,
    PRIMARY KEY (underlying, timestamp_ms)
);
CREATE TABLE IF NOT EXISTS quote_analytics (
    quote_id INTEGER PRIMARY KEY,
    contract_id INTEGER NOT NULL,
    underlying TEXT NOT NULL,
    trading_symbol TEXT NOT NULL,
    option_type TEXT NOT NULL,
    expiry_date TEXT NOT NULL,
    strike REAL NOT NULL,
    lot_size INTEGER,
    observed_at_ms INTEGER NOT NULL,
    source_timestamp_ms INTEGER,
    spot REAL,
    spot_timestamp_ms INTEGER,
    spot_gap_seconds REAL,
    dte_days REAL,
    dte_years REAL,
    moneyness REAL,
    price REAL,
    spread_abs REAL,
    spread_pct REAL,
    implied_vol REAL,
    executable INTEGER,
    flags TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quote_analytics_contract
    ON quote_analytics(contract_id, observed_at_ms);
CREATE INDEX IF NOT EXISTS idx_quote_analytics_underlying
    ON quote_analytics(underlying, observed_at_ms);
CREATE TABLE IF NOT EXISTS candle_analytics (
    contract_id INTEGER NOT NULL,
    interval TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    underlying TEXT NOT NULL,
    trading_symbol TEXT NOT NULL,
    option_type TEXT NOT NULL,
    expiry_date TEXT NOT NULL,
    strike REAL NOT NULL,
    spot REAL,
    spot_timestamp_ms INTEGER,
    spot_gap_seconds REAL,
    dte_days REAL,
    dte_years REAL,
    moneyness REAL,
    price REAL,
    implied_vol REAL,
    flags TEXT NOT NULL,
    PRIMARY KEY (contract_id, interval, timestamp_ms)
);
CREATE INDEX IF NOT EXISTS idx_candle_analytics_underlying
    ON candle_analytics(underlying, timestamp_ms);
CREATE TABLE IF NOT EXISTS build_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _readonly_connection(path: Any) -> sqlite3.Connection:
    archive = Path(path).expanduser().resolve()
    if not archive.exists():
        raise FileNotFoundError(f"archive does not exist: {archive}")
    uri_path = url_quote(str(archive), safe="/:\\")
    connection = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def backup_archive(archive_path: Any) -> Path:
    """Copy the archive before a build.  Never mutates the original."""
    source = Path(archive_path).expanduser()
    if not source.exists():
        raise FileNotFoundError(f"archive does not exist: {source}")
    stamp = datetime.now(IST).strftime("%Y%m%d-%H%M%S")
    target = source.with_name(f"{source.name}.backup-{stamp}")
    shutil.copy2(source, target)
    return target


def _contract_rows(connection: sqlite3.Connection,
                   underlyings: Sequence[str]) -> List[Dict[str, Any]]:
    marks = ",".join("?" for _ in underlyings)
    return [dict(row) for row in connection.execute(
        f"""SELECT contract_id, underlying, trading_symbol, expiry_date,
                   strike, option_type, lot_size
              FROM contracts
             WHERE UPPER(underlying) IN ({marks})
             ORDER BY underlying, expiry_date, strike, option_type""",
        [item.upper() for item in underlyings],
    ).fetchall()]


def build_analytics(
    archive_path: Any,
    analytics_path: Any,
    *,
    spot_pickles: Optional[Mapping[str, Any]] = None,
    underlyings: Sequence[str] = SUPPORTED_UNDERLYINGS,
    start: Any = None,
    end: Any = None,
    interval: str = "5minute",
    feature_config: FeatureConfig = FeatureConfig(),
    backup: bool = False,
) -> Dict[str, Any]:
    """Build (or rebuild) the sidecar analytics database.

    ``start``/``end`` are optional IST dates that restrict the archive rows
    analysed.  The spot series is always loaded for the full cached window so
    as-of joins near the boundaries still work.
    """
    underlyings = tuple(dict.fromkeys(str(item).strip().upper()
                                      for item in underlyings if str(item).strip()))
    if not underlyings:
        raise ValueError("at least one underlying is required")
    archive_path = Path(archive_path).expanduser()
    analytics_path = Path(analytics_path).expanduser()
    backup_path = backup_archive(archive_path) if backup else None

    spots = dict(DEFAULT_SPOT_PICKLES)
    if spot_pickles:
        spots.update({str(key).upper(): value for key, value in spot_pickles.items()})

    analytics_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar = sqlite3.connect(str(analytics_path))
    sidecar.row_factory = sqlite3.Row
    sidecar.executescript(ANALYTICS_SCHEMA)

    archive = _readonly_connection(archive_path)
    archive.row_factory = sqlite3.Row
    try:
        # --- spot bars -------------------------------------------------
        spot_counts: Dict[str, int] = {}
        sidecar.execute("DELETE FROM spot_bars")
        for underlying in underlyings:
            pickle_path = spots.get(underlying)
            if not pickle_path or not Path(pickle_path).exists():
                spot_counts[underlying] = 0
                continue
            bars = load_spot_bars_from_pickle(pickle_path, underlying)
            sidecar.executemany(
                """INSERT OR REPLACE INTO spot_bars
                   (underlying, timestamp_ms, open, high, low, close, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (bar["underlying"], bar["timestamp_ms"], bar["open"],
                     bar["high"], bar["low"], bar["close"], bar["source"])
                    for bar in bars
                ],
            )
            spot_counts[underlying] = len(bars)
        sidecar.commit()

        start_ms = day_bounds_ms(start)[0] if start is not None else None
        end_ms = day_bounds_ms(end)[1] if end is not None else None

        contracts = _contract_rows(archive, underlyings)
        contract_map = {int(row["contract_id"]): row for row in contracts}
        if not contract_map:
            sidecar.execute("DELETE FROM quote_analytics")
            sidecar.execute("DELETE FROM candle_analytics")
            sidecar.commit()
            return {
                "archive": str(archive_path), "sidecar": str(analytics_path),
                "backup": str(backup_path) if backup_path else None,
                "spot_bars": spot_counts, "contracts": 0, "quotes": 0,
                "candles": 0, "iv_quotes": 0, "iv_candles": 0,
            }

        series = _spot_series(sidecar, underlyings)
        contract_ids = list(contract_map)

        def _chunks(values: Sequence[int], size: int = 500) -> Iterable[Sequence[int]]:
            for index in range(0, len(values), size):
                yield values[index:index + size]

        quote_total = 0
        quote_iv = 0
        candle_total = 0
        candle_iv = 0
        flag_counts: Dict[str, int] = {}
        sidecar.execute("DELETE FROM quote_analytics")
        sidecar.execute("DELETE FROM candle_analytics")

        for chunk in _chunks(contract_ids):
            marks = ",".join("?" for _ in chunk)
            quote_rows = archive.execute(
                f"""SELECT quote_id, contract_id, observed_at_ms,
                           source_timestamp_ms, bid, ask, last_price, executable
                      FROM quotes
                     WHERE contract_id IN ({marks})
                       AND (? IS NULL OR observed_at_ms >= ?)
                       AND (? IS NULL OR observed_at_ms <= ?)
                     ORDER BY contract_id, observed_at_ms""",
                [*chunk, start_ms, start_ms, end_ms, end_ms],
            ).fetchall()
            payload = []
            for row in quote_rows:
                contract = contract_map[int(row["contract_id"])]
                when_ms = int(row["observed_at_ms"])
                spot, spot_ms, gap = asof_spot(
                    series, contract["underlying"], when_ms,
                    max_gap_seconds=feature_config.spot_max_gap_seconds,
                )
                features = derive_features(
                    underlying=contract["underlying"],
                    expiry=contract["expiry_date"],
                    strike=float(contract["strike"]),
                    timestamp_ms=when_ms, spot=spot, spot_gap_seconds=gap,
                    bid=row["bid"], ask=row["ask"], last_price=row["last_price"],
                    executable=bool(row["executable"]),
                    option_type=contract["option_type"],
                    config=feature_config,
                )
                if features["implied_vol"] is not None:
                    quote_iv += 1
                for flag in features["flags"].split(","):
                    flag_counts[flag] = flag_counts.get(flag, 0) + 1
                payload.append((
                    int(row["quote_id"]), int(row["contract_id"]),
                    contract["underlying"], contract["trading_symbol"],
                    contract["option_type"], contract["expiry_date"],
                    float(contract["strike"]), contract["lot_size"],
                    when_ms, row["source_timestamp_ms"],
                    features["spot"], spot_ms, features["spot_gap_seconds"],
                    features["dte_days"], features["dte_years"],
                    features["moneyness"], features["price"],
                    features["spread_abs"], features["spread_pct"],
                    features["implied_vol"], int(bool(row["executable"])),
                    features["flags"],
                ))
            if payload:
                sidecar.executemany(
                    """INSERT OR REPLACE INTO quote_analytics
                       (quote_id, contract_id, underlying, trading_symbol,
                        option_type, expiry_date, strike, lot_size,
                        observed_at_ms, source_timestamp_ms, spot,
                        spot_timestamp_ms, spot_gap_seconds, dte_days,
                        dte_years, moneyness, price, spread_abs, spread_pct,
                        implied_vol, executable, flags)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    payload,
                )
            quote_total += len(payload)

            candle_rows = archive.execute(
                f"""SELECT contract_id, timestamp_ms, close
                      FROM candles
                     WHERE contract_id IN ({marks}) AND interval=?
                       AND (? IS NULL OR timestamp_ms >= ?)
                       AND (? IS NULL OR timestamp_ms <= ?)
                     ORDER BY contract_id, timestamp_ms""",
                [*chunk, interval, start_ms, start_ms, end_ms, end_ms],
            ).fetchall()
            candle_payload = []
            for row in candle_rows:
                contract = contract_map[int(row["contract_id"])]
                when_ms = int(row["timestamp_ms"])
                spot, spot_ms, gap = asof_spot(
                    series, contract["underlying"], when_ms,
                    max_gap_seconds=feature_config.spot_max_gap_seconds,
                )
                features = derive_features(
                    underlying=contract["underlying"],
                    expiry=contract["expiry_date"],
                    strike=float(contract["strike"]),
                    timestamp_ms=when_ms, spot=spot, spot_gap_seconds=gap,
                    close=row["close"],
                    option_type=contract["option_type"],
                    config=feature_config,
                )
                if features["implied_vol"] is not None:
                    candle_iv += 1
                candle_payload.append((
                    int(row["contract_id"]), interval, when_ms,
                    contract["underlying"], contract["trading_symbol"],
                    contract["option_type"], contract["expiry_date"],
                    float(contract["strike"]), features["spot"], spot_ms,
                    features["spot_gap_seconds"], features["dte_days"],
                    features["dte_years"], features["moneyness"],
                    features["price"], features["implied_vol"],
                    features["flags"],
                ))
            if candle_payload:
                sidecar.executemany(
                    """INSERT OR REPLACE INTO candle_analytics
                       (contract_id, interval, timestamp_ms, underlying,
                        trading_symbol, option_type, expiry_date, strike,
                        spot, spot_timestamp_ms, spot_gap_seconds, dte_days,
                        dte_years, moneyness, price, implied_vol, flags)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    candle_payload,
                )
            candle_total += len(candle_payload)

        meta = {
            "built_at": datetime.now(IST).isoformat(),
            "archive": str(archive_path),
            "archive_size_bytes": str(archive_path.stat().st_size),
            "backup": str(backup_path) if backup_path else "",
            "underlyings": json.dumps(list(underlyings)),
            "interval": interval,
            "start": str(start) if start is not None else "",
            "end": str(end) if end is not None else "",
            "spot_bars": json.dumps(spot_counts),
            "contracts": str(len(contracts)),
            "quotes": str(quote_total),
            "candles": str(candle_total),
        }
        sidecar.executemany(
            "INSERT OR REPLACE INTO build_meta (key, value) VALUES (?, ?)",
            list(meta.items()),
        )
        sidecar.commit()
        return {
            "archive": str(archive_path),
            "sidecar": str(analytics_path),
            "backup": str(backup_path) if backup_path else None,
            "spot_bars": spot_counts,
            "contracts": len(contracts),
            "quotes": quote_total,
            "candles": candle_total,
            "iv_quotes": quote_iv,
            "iv_candles": candle_iv,
            "flags": dict(sorted(flag_counts.items())),
            "window": {"start": meta["start"], "end": meta["end"]},
        }
    finally:
        archive.close()
        sidecar.close()


# ---------------------------------------------------------------------------
# Coverage / quality summary
# ---------------------------------------------------------------------------

def _flag_counts(connection: sqlite3.Connection,
                 table: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in connection.execute(f"SELECT flags FROM {table}"):
        for flag in str(row["flags"] or "").split(","):
            if flag:
                counts[flag] = counts.get(flag, 0) + 1
    return dict(sorted(counts.items()))


def coverage_summary(analytics_path: Any) -> Dict[str, Any]:
    path = Path(analytics_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"analytics sidecar does not exist: {path}")
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        quotes = connection.execute(
            """SELECT COUNT(*) n,
                      SUM(CASE WHEN implied_vol IS NOT NULL THEN 1 ELSE 0 END) iv,
                      SUM(CASE WHEN executable=1 THEN 1 ELSE 0 END) ex,
                      MIN(observed_at_ms) first_ms, MAX(observed_at_ms) last_ms
                 FROM quote_analytics"""
        ).fetchone()
        candles = connection.execute(
            """SELECT COUNT(*) n,
                      SUM(CASE WHEN implied_vol IS NOT NULL THEN 1 ELSE 0 END) iv,
                      MIN(timestamp_ms) first_ms, MAX(timestamp_ms) last_ms
                 FROM candle_analytics"""
        ).fetchone()
        by_underlying = {
            str(row["underlying"]): {
                "quotes": int(row["n"]),
                "iv": int(row["iv"] or 0),
                "executable": int(row["ex"] or 0),
            }
            for row in connection.execute(
                """SELECT underlying, COUNT(*) n,
                          SUM(CASE WHEN implied_vol IS NOT NULL THEN 1 ELSE 0 END) iv,
                          SUM(CASE WHEN executable=1 THEN 1 ELSE 0 END) ex
                     FROM quote_analytics GROUP BY underlying ORDER BY underlying"""
            )
        }
        iv_values = [float(row["implied_vol"]) for row in connection.execute(
            "SELECT implied_vol FROM quote_analytics WHERE implied_vol IS NOT NULL"
        )]
        gaps = [float(row["spot_gap_seconds"]) for row in connection.execute(
            "SELECT spot_gap_seconds FROM quote_analytics WHERE spot_gap_seconds IS NOT NULL"
        )]
        meta = {str(row["key"]): str(row["value"])
                for row in connection.execute("SELECT key, value FROM build_meta")}
        return {
            "sidecar": str(path),
            "meta": meta,
            "quotes": {
                "rows": int(quotes["n"]),
                "iv": int(quotes["iv"] or 0),
                "executable": int(quotes["ex"] or 0),
                "first": _ist(quotes["first_ms"]).isoformat() if quotes["first_ms"] else None,
                "last": _ist(quotes["last_ms"]).isoformat() if quotes["last_ms"] else None,
                "flags": _flag_counts(connection, "quote_analytics"),
            },
            "candles": {
                "rows": int(candles["n"]),
                "iv": int(candles["iv"] or 0),
                "first": _ist(candles["first_ms"]).isoformat() if candles["first_ms"] else None,
                "last": _ist(candles["last_ms"]).isoformat() if candles["last_ms"] else None,
                "flags": _flag_counts(connection, "candle_analytics"),
            },
            "by_underlying": by_underlying,
            "iv": {
                "quotes_n": len(iv_values),
                "median": statistics.median(iv_values) if iv_values else None,
                "p25": statistics.quantiles(iv_values, n=4)[0] if len(iv_values) >= 4 else None,
                "p75": statistics.quantiles(iv_values, n=4)[2] if len(iv_values) >= 4 else None,
            },
            "spot_gap_seconds": {
                "n": len(gaps),
                "median": statistics.median(gaps) if gaps else None,
                "p90": statistics.quantiles(gaps, n=10)[8] if len(gaps) >= 10 else None,
                "max": max(gaps) if gaps else None,
            },
        }
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Contract selection for replay
# ---------------------------------------------------------------------------

def select_contract(
    connection: sqlite3.Connection,
    *,
    underlying: str,
    option_type: str,
    target_strike: float,
    trade_date: Any,
    expiry: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """Pick the nearest tradeable archived contract for one signal.

    Nearest **expiry strictly after the trade date** first, then nearest
    strike.  The returned dict includes ``strike_drift_pct`` so the caller can
    accept or reject the substitution rather than hiding it.
    """
    when = parse_day(trade_date)
    option_type = str(option_type).strip().upper()
    underlying = str(underlying).strip().upper()
    if option_type not in {"CE", "PE"}:
        return None
    if expiry is not None:
        rows = connection.execute(
            """SELECT contract_id, underlying, trading_symbol, expiry_date,
                      strike, option_type, lot_size
                 FROM contracts
                WHERE UPPER(underlying)=? AND UPPER(option_type)=?
                  AND expiry_date=? ORDER BY strike""",
            (underlying, option_type, parse_day(expiry).isoformat()),
        ).fetchall()
    else:
        rows = connection.execute(
            """SELECT contract_id, underlying, trading_symbol, expiry_date,
                      strike, option_type, lot_size
                 FROM contracts
                WHERE UPPER(underlying)=? AND UPPER(option_type)=?
                  AND expiry_date>? ORDER BY expiry_date, strike""",
            (underlying, option_type, when.isoformat()),
        ).fetchall()
    if not rows:
        return None
    chosen_expiry = min(str(row["expiry_date"]) for row in rows)
    same_expiry = [row for row in rows if str(row["expiry_date"]) == chosen_expiry]
    chosen = min(same_expiry, key=lambda row: abs(float(row["strike"]) - float(target_strike)))
    strike = float(chosen["strike"])
    drift = abs(strike - float(target_strike)) / float(target_strike) if target_strike else None
    expiry_date = _parse_expiry(chosen_expiry)
    return {
        "contract_id": int(chosen["contract_id"]),
        "underlying": str(chosen["underlying"]),
        "trading_symbol": str(chosen["trading_symbol"]),
        "expiry_date": chosen_expiry,
        "strike": strike,
        "option_type": str(chosen["option_type"]),
        "lot_size": chosen["lot_size"],
        "strike_drift_pct": drift,
        "dte_days": float((expiry_date - when).days) if expiry_date else None,
    }


def latest_candle_timestamp(connection: sqlite3.Connection,
                            contract_id: int,
                            day: Any,
                            *,
                            interval: str = "5minute") -> Optional[int]:
    start_ms, end_ms = day_bounds_ms(day)
    row = connection.execute(
        """SELECT MAX(timestamp_ms) value FROM candles
            WHERE contract_id=? AND interval=?
              AND timestamp_ms>=? AND timestamp_ms<=?""",
        (int(contract_id), interval, start_ms, end_ms),
    ).fetchone()
    return None if row is None or row["value"] is None else int(row["value"])


__all__ = [
    "DEFAULT_SPOT_PICKLES",
    "FeatureConfig",
    "asof_spot",
    "backup_archive",
    "build_analytics",
    "coverage_summary",
    "day_bounds_ms",
    "default_analytics_path",
    "derive_features",
    "dte_days",
    "latest_candle_timestamp",
    "load_spot_bars_from_pickle",
    "parse_day",
    "select_contract",
    "time_to_expiry_years",
]
