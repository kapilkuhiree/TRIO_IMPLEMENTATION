"""Incremental, read-only archive for Zerodha NFO option data.

The archive is deliberately separate from the execution runtime.  Zerodha's
historical endpoint can return candles for a known active option token, but
expired option tokens are not discoverable later unless they were archived
while active.  This module stores the daily NFO instrument snapshot, genuine
historical candles, and sampled quote snapshots in SQLite without inventing
bid/ask values or theoretical premiums.

No order API is imported or called here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import uuid
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
SUPPORTED_UNDERLYINGS = frozenset({"NIFTY", "BANKNIFTY"})
SUPPORTED_OPTION_TYPES = frozenset({"CE", "PE"})
FIVE_MINUTE_START = time(9, 15)
FIVE_MINUTE_LAST_START = time(15, 10)


def _now_ist(value: Optional[datetime] = None) -> datetime:
    current = value or datetime.now(IST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=IST)
    return current.astimezone(IST)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _epoch_ms(value: datetime) -> int:
    return int(round(value.astimezone(UTC).timestamp() * 1000))


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _timestamp(value: Any) -> Optional[datetime]:
    """Normalize provider timestamps to aware IST/UTC-safe datetimes.

    Zerodha normally returns timezone-aware timestamps.  A naïve provider
    timestamp is interpreted as IST because that is the exchange convention;
    the raw value is retained in SQLite for auditability.
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    else:
        text = str(value).strip().replace("Z", "+00:00")
        parsed = None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            for fmt in (
                "%Y-%m-%d %H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S",
                "%d-%b-%Y %H:%M:%S",
                "%d-%m-%Y %H:%M:%S",
            ):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def _expiry(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _field(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in row:
            return row[name]
        lower = name.lower()
        for actual, value in row.items():
            if str(actual).lower() == lower:
                return value
    return None


def _contract_identity(row: Mapping[str, Any]) -> Dict[str, Any]:
    expiry = _expiry(_field(row, "expiry", "expiry_date"))
    return {
        "provider": "zerodha",
        "exchange": str(_field(row, "exchange") or "NFO").strip().upper(),
        "instrument_token": str(_field(row, "instrument_token", "instrumentToken") or "").strip(),
        "trading_symbol": str(_field(row, "tradingsymbol", "trading_symbol") or "").strip(),
        "underlying": str(_field(row, "name", "underlying", "underlying_symbol") or "").strip().upper(),
        "option_type": str(_field(row, "instrument_type", "option_type") or "").strip().upper(),
        "expiry_date": expiry.isoformat() if expiry else "",
        "strike": _number(_field(row, "strike")),
        "lot_size": _number(_field(row, "lot_size", "lotSize")),
        "tick_size": _number(_field(row, "tick_size", "tickSize")),
        "segment": str(_field(row, "segment") or "").strip(),
        "exchange_token": str(_field(row, "exchange_token", "exchangeToken") or "").strip(),
    }


def _valid_candle(row: Any, interval: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Normalize a Kite candle list/dict, returning an explicit reject reason."""
    if isinstance(row, Mapping):
        raw = dict(row)
        timestamp = _field(raw, "timestamp", "date", "datetime")
        values = raw
        get = lambda *names: _field(values, *names)
    elif isinstance(row, (list, tuple)):
        if len(row) < 6:
            return None, "incomplete_candle"
        timestamp = row[0]
        values = row
        get = lambda *names: {
            "open": row[1], "high": row[2], "low": row[3],
            "close": row[4], "volume": row[5],
            "open_interest": row[6] if len(row) > 6 else None,
        }.get(names[0])
    else:
        return None, "candle_not_mapping_or_sequence"

    parsed = _timestamp(timestamp)
    if parsed is None:
        return None, "invalid_timestamp"
    interval_text = str(interval).strip().lower()
    minutes = {"minute": 1, "1minute": 1, "3minute": 3,
               "5minute": 5, "10minute": 10, "15minute": 15,
               "30minute": 30, "60minute": 60}.get(interval_text)
    if minutes and interval_text != "day":
        local_time = parsed.time().replace(second=0, microsecond=0)
        if not (FIVE_MINUTE_START <= local_time <= FIVE_MINUTE_LAST_START):
            return None, "outside_session"
        if parsed.minute % minutes:
            return None, "misaligned_bar"

    open_price = _number(get("open", "Open"))
    high = _number(get("high", "High"))
    low = _number(get("low", "Low"))
    close = _number(get("close", "Close"))
    volume = _number(get("volume", "Volume"))
    oi = _number(get("open_interest", "oi", "openInterest"))
    if any(value is None or value <= 0 for value in (open_price, high, low, close)):
        return None, "invalid_ohlc"
    if high < max(open_price, low, close) or low > min(open_price, high, close):
        return None, "inconsistent_ohlc"
    if volume is None or volume < 0:
        return None, "invalid_volume"
    if oi is not None and oi < 0:
        return None, "invalid_open_interest"
    return {
        "timestamp": parsed,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "open_interest": oi,
        "raw": raw if isinstance(row, Mapping) else list(row),
    }, ""


@dataclass
class IngestStats:
    seen: int = 0
    inserted: int = 0
    duplicates: int = 0
    conflicts: int = 0
    rejected: int = 0
    errors: List[str] = field(default_factory=list)

    def update(self, other: "IngestStats") -> None:
        for key in ("seen", "inserted", "duplicates", "conflicts", "rejected"):
            setattr(self, key, getattr(self, key) + getattr(other, key))
        self.errors.extend(other.errors)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seen": self.seen, "inserted": self.inserted,
            "duplicates": self.duplicates, "conflicts": self.conflicts,
            "rejected": self.rejected, "errors": list(self.errors),
        }


class SQLiteOptionArchive:
    """Durable SQLite store with validation and idempotent writes."""

    def __init__(self, path: str | Path, *, timeout: float = 30.0):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path), timeout=timeout)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "SQLiteOptionArchive":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def start_run(self, underlyings: Iterable[str], interval: str,
                  period: str) -> str:
        run_id = uuid.uuid4().hex
        now = _now_ist()
        self.connection.execute(
            """INSERT INTO ingest_runs
               (run_id, started_at_ms, requested_underlyings, interval, period, status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, _epoch_ms(now), _json(list(underlyings)), interval,
             period, "running"),
        )
        self.connection.commit()
        return run_id

    def finish_run(self, run_id: str, *, status: str = "complete",
                   errors: Optional[Sequence[str]] = None) -> None:
        now = _now_ist()
        self.connection.execute(
            "UPDATE ingest_runs SET finished_at_ms=?, status=?, errors_json=? WHERE run_id=?",
            (_epoch_ms(now), status, _json(list(errors or [])), run_id),
        )
        self.connection.commit()

    def _reject(self, run_id: str, entity_type: str, reason: str,
                *, contract_id: Optional[int] = None,
                source_timestamp: Any = None, detail: Any = None,
                raw: Any = None) -> None:
        self.connection.execute(
            """INSERT INTO rejections
               (run_id, entity_type, contract_id, source_timestamp,
                reason_code, detail, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, entity_type, contract_id,
             None if source_timestamp is None else str(source_timestamp),
             reason, None if detail is None else str(detail),
             None if raw is None else _json(raw)),
        )

    @staticmethod
    def _validated_contract(row: Mapping[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
        normalized = _contract_identity(row)
        required = (normalized["exchange"], normalized["instrument_token"],
                    normalized["trading_symbol"], normalized["underlying"],
                    normalized["option_type"], normalized["expiry_date"],
                    normalized["strike"])
        if any(value in (None, "") for value in required):
            return None, "invalid_contract_identity"
        if normalized["exchange"] != "NFO":
            return None, "invalid_contract_exchange"
        if normalized["underlying"] not in SUPPORTED_UNDERLYINGS:
            return None, "unsupported_underlying"
        if normalized["option_type"] not in SUPPORTED_OPTION_TYPES:
            return None, "invalid_option_type"
        if normalized["strike"] <= 0:
            return None, "invalid_strike"
        if normalized["lot_size"] is not None and normalized["lot_size"] <= 0:
            return None, "invalid_lot_size"
        return normalized, ""

    def upsert_contracts(self, instruments: Iterable[Mapping[str, Any]], *,
                         seen_at: datetime, run_id: str) -> IngestStats:
        stats = IngestStats()
        seen_ms = _epoch_ms(_now_ist(seen_at))
        for raw in instruments:
            stats.seen += 1
            normalized, reason = self._validated_contract(raw)
            if normalized is None:
                stats.rejected += 1
                self._reject(run_id, "contract", reason, raw=raw)
                continue
            token = normalized["instrument_token"]
            symbol = normalized["trading_symbol"]
            existing_token = self.connection.execute(
                "SELECT * FROM contracts WHERE provider=? AND exchange=? AND instrument_token=?",
                ("zerodha", "NFO", token),
            ).fetchone()
            existing_symbol = self.connection.execute(
                "SELECT * FROM contracts WHERE provider=? AND exchange=? AND trading_symbol=?",
                ("zerodha", "NFO", symbol),
            ).fetchone()
            if existing_token is not None and existing_token["trading_symbol"] != symbol:
                stats.rejected += 1
                self._reject(run_id, "contract", "contract_identity_conflict",
                             detail=f"token maps to {existing_token['trading_symbol']}", raw=raw)
                continue
            if existing_symbol is not None and existing_symbol["instrument_token"] != token:
                stats.rejected += 1
                self._reject(run_id, "contract", "contract_identity_conflict",
                             detail=f"symbol maps to {existing_symbol['instrument_token']}", raw=raw)
                continue
            if existing_token is None:
                self.connection.execute(
                    """INSERT INTO contracts
                       (provider, exchange, instrument_token, trading_symbol,
                        underlying, option_type, expiry_date, strike, lot_size,
                        tick_size, segment, exchange_token, first_seen_at_ms,
                        last_seen_at_ms, raw_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    ("zerodha", "NFO", token, symbol, normalized["underlying"],
                     normalized["option_type"], normalized["expiry_date"],
                     normalized["strike"],
                     None if normalized["lot_size"] is None else int(normalized["lot_size"]),
                     normalized["tick_size"], normalized["segment"],
                     normalized["exchange_token"], seen_ms, seen_ms, _json(raw)),
                )
                stats.inserted += 1
            else:
                self.connection.execute(
                    """UPDATE contracts SET last_seen_at_ms=?, lot_size=?,
                       tick_size=?, segment=?, exchange_token=?, raw_json=?
                       WHERE contract_id=?""",
                    (seen_ms,
                     None if normalized["lot_size"] is None else int(normalized["lot_size"]),
                     normalized["tick_size"], normalized["segment"],
                     normalized["exchange_token"], _json(raw), existing_token["contract_id"]),
                )
                stats.duplicates += 1
        self.connection.commit()
        self._increment_run(run_id, contracts_seen=stats.seen,
                            contracts_written=stats.inserted,
                            rejected_rows=stats.rejected)
        return stats

    def _contract_id(self, contract: Mapping[str, Any]) -> Optional[int]:
        identity, _ = self._validated_contract(contract)
        if identity is None:
            return None
        row = self.connection.execute(
            """SELECT contract_id FROM contracts
               WHERE provider='zerodha' AND exchange='NFO' AND instrument_token=?""",
            (identity["instrument_token"],),
        ).fetchone()
        return int(row["contract_id"]) if row else None

    def _increment_run(self, run_id: str, **fields: int) -> None:
        allowed = {
            "contracts_seen", "contracts_written", "candle_rows_seen",
            "candle_rows_inserted", "candle_duplicates", "quote_rows_seen",
            "quote_rows_inserted", "quote_duplicates", "rejected_rows",
        }
        fields = {key: int(value) for key, value in fields.items() if key in allowed}
        if not fields:
            return
        assignments = ", ".join(f"{key}={key}+?" for key in fields)
        self.connection.execute(
            f"UPDATE ingest_runs SET {assignments} WHERE run_id=?",
            (*fields.values(), run_id),
        )
        self.connection.commit()

    def append_candles(self, contract: Mapping[str, Any], rows: Iterable[Any], *,
                       interval: str, fetched_at: datetime,
                       run_id: str) -> IngestStats:
        stats = IngestStats()
        contract_id = self._contract_id(contract)
        if contract_id is None:
            stats.rejected += 1
            self._reject(run_id, "candle", "invalid_contract_identity", raw=contract)
            self.connection.commit()
            return stats
        fetched_ms = _epoch_ms(_now_ist(fetched_at))
        for raw in rows or []:
            stats.seen += 1
            candle, reason = _valid_candle(raw, interval)
            if candle is None:
                stats.rejected += 1
                self._reject(run_id, "candle", reason,
                             contract_id=contract_id,
                             source_timestamp=_field(raw, "timestamp", "date")
                             if isinstance(raw, Mapping) else (raw[0] if raw else None),
                             raw=raw)
                continue
            timestamp_ms = _epoch_ms(candle["timestamp"])
            existing = self.connection.execute(
                """SELECT open, high, low, close, volume, open_interest
                   FROM candles WHERE contract_id=? AND interval=? AND timestamp_ms=?""",
                (contract_id, interval, timestamp_ms),
            ).fetchone()
            if existing is not None:
                same = all(abs(float(existing[key]) - float(candle[key])) <= 1e-12
                           for key in ("open", "high", "low", "close", "volume"))
                old_oi, new_oi = existing["open_interest"], candle["open_interest"]
                same = same and (old_oi is None or new_oi is None or
                                 abs(float(old_oi) - float(new_oi)) <= 1e-12)
                stats.duplicates += 1
                if not same:
                    stats.conflicts += 1
                    self._reject(run_id, "candle", "duplicate_conflict",
                                 contract_id=contract_id,
                                 source_timestamp=candle["timestamp"].isoformat(), raw=raw)
                continue
            self.connection.execute(
                """INSERT INTO candles
                   (contract_id, interval, timestamp_ms, timestamp_utc,
                    open, high, low, close, volume, open_interest,
                    fetched_at_ms, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (contract_id, interval, timestamp_ms, _iso(candle["timestamp"]),
                 candle["open"], candle["high"], candle["low"], candle["close"],
                 candle["volume"], candle["open_interest"], fetched_ms, _json(candle["raw"])),
            )
            stats.inserted += 1
        self.connection.commit()
        self._increment_run(run_id, candle_rows_seen=stats.seen,
                            candle_rows_inserted=stats.inserted,
                            candle_duplicates=stats.duplicates,
                            rejected_rows=stats.rejected)
        return stats

    def append_quote(self, contract: Mapping[str, Any], quote: Mapping[str, Any], *,
                     observed_at: datetime, run_id: str) -> IngestStats:
        stats = IngestStats(seen=1)
        contract_id = self._contract_id(contract)
        if contract_id is None:
            stats.rejected = 1
            self._reject(run_id, "quote", "invalid_contract_identity", raw=contract)
            self.connection.commit()
            return stats
        observed = _now_ist(observed_at)
        observed_ms = _epoch_ms(observed)
        source_raw = quote.get("timestamp")
        source_time = _timestamp(source_raw)
        bid = _number(quote.get("bid"))
        ask = _number(quote.get("ask"))
        last_price = _number(quote.get("last_price", quote.get("lastPrice")))
        volume = _number(quote.get("volume"))
        oi = _number(quote.get("open_interest", quote.get("oi")))
        if volume is not None and volume < 0:
            stats.rejected = 1
            self._reject(run_id, "quote", "invalid_volume", contract_id=contract_id, raw=quote)
            self.connection.commit()
            return stats
        if oi is not None and oi < 0:
            stats.rejected = 1
            self._reject(run_id, "quote", "invalid_open_interest", contract_id=contract_id, raw=quote)
            self.connection.commit()
            return stats
        if (bid is not None and bid <= 0) or (ask is not None and ask <= 0) or (
                bid is not None and ask is not None and bid >= ask):
            # Preserve the snapshot for liquidity diagnostics, but mark it
            # non-executable rather than inventing or repairing either side.
            executable = 0
        else:
            executable = int(bid is not None and ask is not None and bid > 0 and ask > bid)
        existing = self.connection.execute(
            "SELECT quote_id FROM quotes WHERE contract_id=? AND observed_at_ms=?",
            (contract_id, observed_ms),
        ).fetchone()
        if existing is not None:
            stats.duplicates = 1
            self.connection.commit()
            self._increment_run(run_id, quote_rows_seen=1, quote_duplicates=1)
            return stats
        self.connection.execute(
            """INSERT INTO quotes
               (contract_id, observed_at_ms, observed_at_utc,
                source_timestamp_raw, source_timestamp_ms, bid, ask,
                last_price, volume, open_interest, executable, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (contract_id, observed_ms, _iso(observed),
             None if source_raw in (None, "") else str(source_raw),
             _epoch_ms(source_time) if source_time else None,
             bid, ask, last_price, volume, oi, executable, _json(quote)),
        )
        stats.inserted = 1
        self.connection.commit()
        self._increment_run(run_id, quote_rows_seen=1, quote_rows_inserted=1)
        return stats

    def append_quotes(self, contract: Mapping[str, Any], quotes: Iterable[Mapping[str, Any]], *,
                      observed_at: datetime, run_id: str) -> IngestStats:
        result = IngestStats()
        for quote in quotes or []:
            result.update(self.append_quote(contract, quote, observed_at=observed_at,
                                            run_id=run_id))
        return result

    def coverage(self, *, underlyings: Iterable[str] = ("NIFTY", "BANKNIFTY"),
                 interval: str = "5minute", start: Optional[datetime] = None,
                 end: Optional[datetime] = None,
                 trading_dates: Optional[Iterable[date]] = None,
                 partial_selection: bool = False) -> Dict[str, Any]:
        wanted = [str(item).strip().upper() for item in underlyings]
        rows = self.connection.execute(
            f"""SELECT c.*, ca.interval, ca.timestamp_ms, ca.timestamp_utc
                FROM contracts c LEFT JOIN candles ca
                ON ca.contract_id=c.contract_id AND ca.interval=?
                WHERE c.underlying IN ({','.join('?' for _ in wanted)})
                ORDER BY c.underlying, c.expiry_date, c.strike, c.option_type,
                         ca.timestamp_ms""",
            [interval, *wanted],
        ).fetchall()
        # Apply timestamp filters to the materialized rows so the optional
        # parameters do not accidentally alter the contract universe.
        start_ms = _epoch_ms(_now_ist(start)) if start else None
        end_ms = _epoch_ms(_now_ist(end)) if end else None
        by_contract: Dict[int, Dict[str, Any]] = {}
        for row in rows:
            record = by_contract.setdefault(int(row["contract_id"]), {
                "underlying": row["underlying"], "expiry": row["expiry_date"],
                "strike": row["strike"], "option_type": row["option_type"],
                "trading_symbol": row["trading_symbol"], "instrument_token": row["instrument_token"],
                "candle_count": 0, "candle_first": None, "candle_last": None,
                "quote_count": 0, "executable_quote_count": 0,
                "missing_5m_slots": [],
            })
            ts = row["timestamp_ms"]
            if ts is not None and (start_ms is None or ts >= start_ms) and (end_ms is None or ts <= end_ms):
                record["candle_count"] += 1
                record["candle_first"] = record["candle_first"] or row["timestamp_utc"]
                record["candle_last"] = row["timestamp_utc"]

        contract_ids = list(by_contract)
        if contract_ids:
            marks = ",".join("?" for _ in contract_ids)
            quote_rows = self.connection.execute(
                f"""SELECT contract_id, COUNT(*) AS n,
                          SUM(CASE WHEN executable=1 THEN 1 ELSE 0 END) AS executable_n
                   FROM quotes WHERE contract_id IN ({marks})
                   GROUP BY contract_id""", contract_ids).fetchall()
            for row in quote_rows:
                if int(row["contract_id"]) in by_contract:
                    by_contract[int(row["contract_id"])] ["quote_count"] = int(row["n"])
                    by_contract[int(row["contract_id"])] ["executable_quote_count"] = int(row["executable_n"] or 0)

        contract_rows = self.connection.execute(
            f"""SELECT * FROM contracts
                WHERE underlying IN ({','.join('?' for _ in wanted)})
                ORDER BY underlying, expiry_date, strike, option_type""",
            wanted).fetchall()
        contract_count = len(contract_rows)
        by_expiry: Dict[str, int] = {}
        for row in contract_rows:
            by_expiry[str(row["expiry_date"])] = by_expiry.get(
                str(row["expiry_date"]), 0) + 1
        candles_total = sum(item["candle_count"] for item in by_contract.values())
        quote_total = sum(item["quote_count"] for item in by_contract.values())
        executable_total = sum(item["executable_quote_count"] for item in by_contract.values())
        candle_times = self.connection.execute(
            f"""SELECT MIN(ca.timestamp_utc) AS first_ts, MAX(ca.timestamp_utc) AS last_ts
               FROM candles ca JOIN contracts c ON c.contract_id=ca.contract_id
               WHERE ca.interval=? AND c.underlying IN ({','.join('?' for _ in wanted)})""",
            [interval, *wanted],
        ).fetchone()
        quote_times = self.connection.execute(
            f"""SELECT MIN(q.observed_at_utc) AS first_ts, MAX(q.observed_at_utc) AS last_ts
               FROM quotes q JOIN contracts c ON c.contract_id=q.contract_id
               WHERE c.underlying IN ({','.join('?' for _ in wanted)})""",
            wanted,
        ).fetchone()
        expected_slots = None
        missing_slots = None
        if trading_dates is not None and interval.lower() in {"5minute", "5m"}:
            dates = list(trading_dates)
            expected_times = {
                _epoch_ms(datetime.combine(item, time(9, 15), tzinfo=IST)
                          + timedelta(minutes=5 * offset))
                for item in dates for offset in range(72)
            }
            expected_slots = len(expected_times)
            missing_slots = 0
            for contract_id, item in by_contract.items():
                actual_rows = self.connection.execute(
                    """SELECT timestamp_ms FROM candles
                       WHERE contract_id=? AND interval=?""",
                    (contract_id, interval),
                ).fetchall()
                actual = {int(row["timestamp_ms"]) for row in actual_rows}
                missing = sorted(expected_times - actual)
                item["missing_5m_slots"] = [
                    datetime.fromtimestamp(mark / 1000, UTC).astimezone(IST).isoformat()
                    for mark in missing
                ]
                missing_slots += len(missing)
        rejected = self.connection.execute(
            """SELECT reason_code, COUNT(*) AS n FROM rejections
               GROUP BY reason_code ORDER BY reason_code"""
        ).fetchall()
        return {
            "filters": {"underlyings": wanted, "interval": interval,
                        "start": _iso(_now_ist(start)) if start else None,
                        "end": _iso(_now_ist(end)) if end else None},
            "selection": {
                "contracts": contract_count,
                "by_underlying": {
                    item: sum(1 for row in contract_rows if row["underlying"] == item)
                    for item in wanted},
                "by_expiry": by_expiry,
                "by_option_type": {
                    option: sum(1 for row in contract_rows if row["option_type"] == option)
                    for option in sorted(SUPPORTED_OPTION_TYPES)},
                "partial_selection": bool(partial_selection),
            },
            "candles": {
                "rows": candles_total,
                "contracts_with_candles": sum(1 for item in by_contract.values()
                                               if item["candle_count"]),
                "first_timestamp": candle_times["first_ts"],
                "last_timestamp": candle_times["last_ts"],
                "observed_session_days": self._observed_days(wanted, interval),
                "expected_slots": expected_slots,
                "missing_slots": missing_slots,
            },
            "quotes": {
                "rows": quote_total,
                "contracts_with_quotes": sum(1 for item in by_contract.values()
                                              if item["quote_count"]),
                "first_observed_at": quote_times["first_ts"],
                "last_observed_at": quote_times["last_ts"],
                "executable_rows": executable_total,
                "non_executable_rows": max(0, quote_total - executable_total),
            },
            "by_contract": list(by_contract.values()),
            "rejections": {row["reason_code"]: int(row["n"]) for row in rejected},
            "historical_bid_ask_alignment": "unavailable",
            "readiness": "NOT_READY_FOR_OPTIONS_PNL",
        }

    def _observed_days(self, underlyings: Sequence[str], interval: str) -> int:
        marks = ",".join("?" for _ in underlyings)
        rows = self.connection.execute(
            f"""SELECT DISTINCT substr(ca.timestamp_utc, 1, 10) AS d
                FROM candles ca JOIN contracts c ON c.contract_id=ca.contract_id
                WHERE ca.interval=? AND c.underlying IN ({marks})""",
            [interval, *underlyings],
        ).fetchall()
        return len(rows)


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 30000;
CREATE TABLE IF NOT EXISTS contracts (
    contract_id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    exchange TEXT NOT NULL,
    instrument_token TEXT NOT NULL,
    trading_symbol TEXT NOT NULL,
    underlying TEXT NOT NULL,
    option_type TEXT NOT NULL,
    expiry_date TEXT NOT NULL,
    strike REAL NOT NULL,
    lot_size INTEGER,
    tick_size REAL,
    segment TEXT,
    exchange_token TEXT,
    first_seen_at_ms INTEGER NOT NULL,
    last_seen_at_ms INTEGER NOT NULL,
    raw_json TEXT NOT NULL,
    UNIQUE(provider, exchange, instrument_token),
    UNIQUE(provider, exchange, trading_symbol)
);
CREATE TABLE IF NOT EXISTS candles (
    contract_id INTEGER NOT NULL REFERENCES contracts(contract_id),
    interval TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    timestamp_utc TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    open_interest REAL,
    fetched_at_ms INTEGER NOT NULL,
    raw_json TEXT NOT NULL,
    PRIMARY KEY(contract_id, interval, timestamp_ms)
);
CREATE TABLE IF NOT EXISTS quotes (
    quote_id INTEGER PRIMARY KEY,
    contract_id INTEGER NOT NULL REFERENCES contracts(contract_id),
    observed_at_ms INTEGER NOT NULL,
    observed_at_utc TEXT NOT NULL,
    source_timestamp_raw TEXT,
    source_timestamp_ms INTEGER,
    bid REAL,
    ask REAL,
    last_price REAL,
    volume REAL,
    open_interest REAL,
    executable INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL,
    UNIQUE(contract_id, observed_at_ms)
);
CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id TEXT PRIMARY KEY,
    started_at_ms INTEGER NOT NULL,
    finished_at_ms INTEGER,
    requested_underlyings TEXT NOT NULL,
    interval TEXT,
    period TEXT,
    contracts_seen INTEGER NOT NULL DEFAULT 0,
    contracts_written INTEGER NOT NULL DEFAULT 0,
    candle_rows_seen INTEGER NOT NULL DEFAULT 0,
    candle_rows_inserted INTEGER NOT NULL DEFAULT 0,
    candle_duplicates INTEGER NOT NULL DEFAULT 0,
    quote_rows_seen INTEGER NOT NULL DEFAULT 0,
    quote_rows_inserted INTEGER NOT NULL DEFAULT 0,
    quote_duplicates INTEGER NOT NULL DEFAULT 0,
    rejected_rows INTEGER NOT NULL DEFAULT 0,
    errors_json TEXT,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rejections (
    rejection_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES ingest_runs(run_id),
    entity_type TEXT NOT NULL,
    contract_id INTEGER,
    source_timestamp TEXT,
    reason_code TEXT NOT NULL,
    detail TEXT,
    raw_json TEXT
);
"""


def discover_active_option_instruments(
    adapter: Any,
    *,
    underlyings: Iterable[str] = ("NIFTY", "BANKNIFTY"),
    as_of: Optional[datetime] = None,
    expiry_from: Optional[date] = None,
    expiry_to: Optional[date] = None,
    option_types: Iterable[str] = ("CE", "PE"),
    strike_min: Optional[float] = None,
    strike_max: Optional[float] = None,
    rejections: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Return active, identity-complete NFO rows from one daily snapshot."""
    wanted = {str(item).strip().upper() for item in underlyings}
    wanted_types = {str(item).strip().upper() for item in option_types}
    current = _now_ist(as_of).date()
    output: List[Dict[str, Any]] = []
    rejected = rejections if rejections is not None else []
    rows = adapter.instruments("NFO")
    for raw in rows or []:
        identity = _contract_identity(raw)
        reason = ""
        expiry = _expiry(_field(raw, "expiry", "expiry_date"))
        if identity["underlying"] not in wanted:
            reason = "unsupported_underlying"
        elif identity["option_type"] not in wanted_types:
            reason = "invalid_option_type"
        elif not identity["instrument_token"] or not identity["trading_symbol"]:
            reason = "invalid_contract_identity"
        elif expiry is None:
            reason = "invalid_expiry"
        elif expiry < current:
            reason = "expired_contract"
        elif expiry_from is not None and expiry < expiry_from:
            reason = "before_expiry_filter"
        elif expiry_to is not None and expiry > expiry_to:
            reason = "after_expiry_filter"
        elif identity["strike"] is None or identity["strike"] <= 0:
            reason = "invalid_strike"
        elif strike_min is not None and identity["strike"] < strike_min:
            reason = "below_strike_filter"
        elif strike_max is not None and identity["strike"] > strike_max:
            reason = "above_strike_filter"
        elif identity["exchange"] != "NFO":
            reason = "invalid_contract_exchange"
        if reason:
            rejected.append({"reason": reason, "raw": dict(raw) if isinstance(raw, Mapping) else raw})
            continue
        output.append(dict(raw))
    output.sort(key=lambda row: (
        str(_field(row, "name") or ""),
        str(_field(row, "expiry") or ""),
        float(_number(_field(row, "strike")) or 0),
        str(_field(row, "instrument_type") or ""),
    ))
    return output


def _chunks(values: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    width = max(1, int(size))
    for index in range(0, len(values), width):
        yield values[index:index + width]


def archive_active_options(
    adapter: Any,
    archive: SQLiteOptionArchive,
    *,
    underlyings: Iterable[str] = ("NIFTY", "BANKNIFTY"),
    interval: str = "5minute",
    period: str = "30d",
    collect_candles: bool = True,
    collect_quotes: bool = True,
    quote_batch_size: int = 200,
    max_contracts: Optional[int] = None,
    as_of: Optional[datetime] = None,
    strike_min: Optional[float] = None,
    strike_max: Optional[float] = None,
) -> Dict[str, Any]:
    """Capture one restart-safe active-contract snapshot.

    ``max_contracts`` is an explicit sampling limit.  The returned report
    marks the selection partial so it cannot be mistaken for full-universe
    coverage.
    """
    current = _now_ist(as_of)
    wanted = [str(item).strip().upper() for item in underlyings]
    run_id = archive.start_run(wanted, interval, period)
    discovery_rejections: List[Dict[str, Any]] = []
    instruments = discover_active_option_instruments(
        adapter, underlyings=wanted, as_of=current,
        strike_min=strike_min, strike_max=strike_max,
        rejections=discovery_rejections)
    partial = max_contracts is not None and max_contracts > 0 and len(instruments) > max_contracts
    if max_contracts is not None and max_contracts > 0:
        instruments = instruments[:max_contracts]
    # Discovery rejections are kept with the run; no malformed/expired rows
    # disappear silently just because they are outside the active scope.
    for item in discovery_rejections:
        archive._reject(run_id, "contract", item["reason"], raw=item.get("raw"))
    archive.connection.commit()
    contract_stats = archive.upsert_contracts(instruments, seen_at=current, run_id=run_id)
    historical_success = 0
    historical_errors: List[Dict[str, Any]] = []
    candle_stats = IngestStats()
    if collect_candles:
        for instrument in instruments:
            try:
                rows = adapter.historical("NFO", str(instrument["tradingsymbol"]),
                                          interval=interval, period=period)
                candle_stats.update(archive.append_candles(
                    instrument, rows, interval=interval,
                    fetched_at=current, run_id=run_id))
                historical_success += 1
            except Exception as exc:
                error = {"trading_symbol": instrument.get("tradingsymbol"),
                         "error_type": type(exc).__name__, "error": str(exc)}
                historical_errors.append(error)
                archive._reject(run_id, "candle", "historical_fetch_error",
                                raw=instrument, detail=str(exc))
        archive.connection.commit()

    quote_stats = IngestStats()
    quote_errors: List[Dict[str, Any]] = []
    if collect_quotes and instruments:
        for batch in _chunks(instruments, quote_batch_size):
            observed = _now_ist()
            symbols = [str(item["tradingsymbol"]) for item in batch]
            try:
                payloads = adapter.quote("NFO", symbols) or {}
            except Exception as exc:
                quote_errors.append({"symbols": symbols, "error_type": type(exc).__name__,
                                     "error": str(exc)})
                for item in batch:
                    archive._reject(run_id, "quote", "quote_fetch_error", raw=item,
                                    detail=str(exc))
                archive.connection.commit()
                continue
            for instrument in batch:
                key = f"NFO:{instrument['tradingsymbol']}"
                raw_quote = payloads.get(key)
                if not isinstance(raw_quote, Mapping):
                    quote_errors.append({"trading_symbol": instrument["tradingsymbol"],
                                         "error": "quote_missing"})
                    archive._reject(run_id, "quote", "missing_quote_payload", raw=instrument)
                    continue
                depth = raw_quote.get("depth", {}) or {}
                buys = depth.get("buy", []) or []
                sells = depth.get("sell", []) or []
                quote = {
                    "trading_symbol": instrument["tradingsymbol"],
                    "instrument_token": instrument.get("instrument_token"),
                    "bid": buys[0].get("price") if buys else None,
                    "ask": sells[0].get("price") if sells else None,
                    "last_price": raw_quote.get("last_price"),
                    "open_interest": raw_quote.get("oi"),
                    "volume": raw_quote.get("volume"),
                    "timestamp": raw_quote.get("timestamp"),
                }
                quote_stats.update(archive.append_quote(
                    instrument, quote, observed_at=observed, run_id=run_id))
            archive.connection.commit()
    all_errors = historical_errors + quote_errors
    archive.finish_run(run_id, status="complete" if not all_errors else "partial",
                       errors=[str(item) for item in all_errors])
    report = archive.coverage(underlyings=wanted, interval=interval,
                              partial_selection=partial)
    report["run"] = {
        "run_id": run_id, "status": "complete" if not all_errors else "partial",
        "requested_underlyings": wanted, "interval": interval, "period": period,
        "selected_contracts": len(instruments),
        "discovery_rejections": len(discovery_rejections),
        "contract_stats": contract_stats.to_dict(),
        "candle_stats": candle_stats.to_dict(),
        "quote_stats": quote_stats.to_dict(),
        "historical_contracts_queried": historical_success,
        "historical_fetch_errors": historical_errors,
        "quote_errors": quote_errors,
        "orders_placed": 0,
    }
    return report


def default_archive_path() -> Path:
    root = os.environ.get("TRIO_OPTIONS_ARCHIVE_PATH")
    if root:
        return Path(root).expanduser()
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    if local:
        return Path(local) / "TRIO" / "options" / "options_archive.sqlite3"
    return Path.home() / ".trio" / "options" / "options_archive.sqlite3"


__all__ = [
    "IngestStats", "SQLiteOptionArchive", "archive_active_options",
    "default_archive_path", "discover_active_option_instruments",
]
