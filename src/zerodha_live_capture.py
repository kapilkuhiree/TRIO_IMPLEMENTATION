"""Read-only, restart-safe capture of active Zerodha NFO option data.

This module is intentionally a data collector, not a trading integration.  It
uses :class:`ZerodhaMarketData` for the provider boundary and the existing
``SQLiteOptionArchive`` write/read API for durable storage.  The only writes
performed are local SQLite archive writes; no broker/order module is imported.

The provider timestamp on a quote is kept separate from the local
``observed_at`` timestamp.  Candle timestamps are never used as a fallback for
either value.  This distinction matters when checking whether a live quote can
be compared with a historical candle at the same exact provider timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
import os
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.zerodha_market_data import ZerodhaMarketData
from src.zerodha_option_archive import (
    SQLiteOptionArchive,
    _contract_identity,
    _expiry,
    _number,
    _now_ist,
    discover_active_option_instruments,
    default_archive_path,
)


IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
DEFAULT_UNDERLYINGS = ("NIFTY", "BANKNIFTY")
DEFAULT_INTERVAL = "5minute"


def _as_ist(value: Optional[datetime]) -> datetime:
    current = value or datetime.now(IST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=IST)
    return current.astimezone(IST)


def _finite(value: Any) -> Optional[float]:
    number = _number(value)
    return number if number is not None and math.isfinite(number) else None


def _normalise_underlyings(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    for value in values:
        for item in str(value or "").split(","):
            name = item.strip().upper()
            if name and name not in result:
                result.append(name)
    return result


def _safe_error(scope: str, exc: BaseException, *, symbol: Any = None,
                detail: Optional[str] = None) -> Dict[str, Any]:
    """Return bounded diagnostics without exposing credentials or responses."""
    message = str(detail if detail is not None else exc)
    for name in ("ZERODHA_API_KEY", "ZERODHA_ACCESS_TOKEN"):
        secret = os.environ.get(name)
        if secret:
            message = message.replace(secret, "[redacted]")
    return {
        "scope": scope,
        **({"trading_symbol": str(symbol)} if symbol else {}),
        "error_type": type(exc).__name__,
        "error": message[:240],
    }


@dataclass(frozen=True)
class CaptureSettings:
    """One capture cycle's provider and selection policy."""

    underlyings: Tuple[str, ...] = DEFAULT_UNDERLYINGS
    interval: str = DEFAULT_INTERVAL
    period: str = "30d"
    max_contracts: Optional[int] = None
    strike_min: Optional[float] = None
    strike_max: Optional[float] = None
    quote_batch_size: int = 200
    collect_candles: bool = True
    collect_quotes: bool = True
    max_retries: int = 3
    backoff_seconds: float = 1.0
    rate_limit_seconds: float = 0.25

    def __post_init__(self) -> None:
        names = tuple(_normalise_underlyings(self.underlyings))
        if not names:
            raise ValueError("at least one underlying is required")
        object.__setattr__(self, "underlyings", names)
        if self.max_contracts is not None and self.max_contracts < 1:
            raise ValueError("max_contracts must be positive")
        if self.strike_min is not None and self.strike_max is not None and self.strike_min > self.strike_max:
            raise ValueError("strike_min cannot exceed strike_max")
        if self.quote_batch_size < 1:
            raise ValueError("quote_batch_size must be positive")
        if self.max_retries < 1:
            raise ValueError("max_retries must be positive")
        if self.backoff_seconds < 0 or self.rate_limit_seconds < 0:
            raise ValueError("backoff and rate-limit seconds cannot be negative")


class CaptureSQLiteOptionArchive(SQLiteOptionArchive):
    """The existing archive with validation extended to configured stocks.

    ``SQLiteOptionArchive`` deliberately protects its original two-index
    collector with a small allow-list.  The new collector needs the same
    schema and idempotent methods for stock options too, so this subclass only
    broadens contract validation; all storage, validation, and coverage methods
    remain the existing archive implementation.
    """

    @staticmethod
    def _validated_contract(row: Mapping[str, Any]):
        normalized = _contract_identity(row)
        required = (
            normalized["exchange"], normalized["instrument_token"],
            normalized["trading_symbol"], normalized["underlying"],
            normalized["option_type"], normalized["expiry_date"],
            normalized["strike"],
        )
        if any(value in (None, "") for value in required):
            return None, "invalid_contract_identity"
        if normalized["exchange"] != "NFO":
            return None, "invalid_contract_exchange"
        if normalized["option_type"] not in {"CE", "PE"}:
            return None, "invalid_option_type"
        if normalized["strike"] is None or normalized["strike"] <= 0:
            return None, "invalid_strike"
        if normalized["lot_size"] is not None and normalized["lot_size"] <= 0:
            return None, "invalid_lot_size"
        return normalized, ""


class _RateLimiter:
    def __init__(self, minimum_seconds: float, sleep: Callable[[float], Any],
                 monotonic: Callable[[], float] = time.monotonic):
        self.minimum_seconds = max(0.0, float(minimum_seconds))
        self.sleep = sleep
        self.monotonic = monotonic
        self.last_call: Optional[float] = None

    def before_provider_call(self) -> None:
        now = self.monotonic()
        if self.last_call is not None:
            wait = self.minimum_seconds - (now - self.last_call)
            if wait > 0:
                self.sleep(wait)
        self.last_call = self.monotonic()


class ZerodhaLiveCapture:
    """Capture active exact contracts, 5-minute history, and quote depth."""

    provider_name = "zerodha"
    read_only = True

    def __init__(
        self,
        adapter: ZerodhaMarketData,
        archive: SQLiteOptionArchive,
        settings: Optional[CaptureSettings] = None,
        *,
        sleep: Callable[[float], Any] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(IST),
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.adapter = adapter
        self.archive = archive
        self.settings = settings or CaptureSettings()
        # Callers may pass the original archive class directly.  Keep its
        # schema and public methods, while allowing this collector's explicit
        # stock-underlying policy to validate through the same methods.
        if (not isinstance(archive, CaptureSQLiteOptionArchive)
                and any(name not in {"NIFTY", "BANKNIFTY"}
                        for name in self.settings.underlyings)):
            archive._validated_contract = CaptureSQLiteOptionArchive._validated_contract
        self.sleep = sleep
        self.now = now
        self.rate_limiter = _RateLimiter(
            self.settings.rate_limit_seconds, sleep, monotonic)

    def _provider_call(self, scope: str, operation: Callable[[], Any],
                       errors: List[Dict[str, Any]], *, symbol: Any = None,
                       record_error: bool = True) -> Any:
        last_error: Optional[BaseException] = None
        for attempt in range(self.settings.max_retries):
            self.rate_limiter.before_provider_call()
            try:
                return operation()
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # Provider errors are isolated per operation.
                last_error = exc
                if attempt + 1 >= self.settings.max_retries:
                    if record_error:
                        errors.append(_safe_error(scope, exc, symbol=symbol))
                    raise
                delay = self.settings.backoff_seconds * (2 ** attempt)
                if delay > 0:
                    self.sleep(delay)
        assert last_error is not None
        raise last_error

    @staticmethod
    def _exact_contracts(rows: Sequence[Mapping[str, Any]], *, now: datetime,
                         underlyings: Sequence[str], strike_min: Optional[float],
                         strike_max: Optional[float],
                         rejected: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Keep only identity-complete active CE/PE NFO rows.

        The shared discovery API applies expiry, exchange, strike, and option
        type filters.  This second check makes the capture contract explicit:
        the token, symbol, underlying, expiry, strike, side, and lot metadata
        must all be present before a row can be archived or queried.
        """
        discovered = discover_active_option_instruments(
            type("_RowsAdapter", (), {"instruments": lambda self, exchange: list(rows)})(),
            underlyings=underlyings,
            as_of=now,
            option_types=("CE", "PE"),
            strike_min=strike_min,
            strike_max=strike_max,
            rejections=rejected,
        )
        output: List[Dict[str, Any]] = []
        for raw in discovered:
            identity = _contract_identity(raw)
            raw_exchange = str(raw.get("exchange", "")).strip().upper()
            required = (
                raw_exchange, identity["instrument_token"],
                identity["trading_symbol"], identity["underlying"],
                identity["option_type"], identity["expiry_date"],
                identity["strike"], identity["lot_size"],
            )
            if any(value in (None, "") for value in required):
                rejected.append({
                    "reason": "incomplete_contract_metadata",
                    "raw": dict(raw),
                })
                continue
            output.append(dict(raw))
        output.sort(key=lambda row: (
            str(row.get("name", "")), str(row.get("expiry", "")),
            float(_finite(row.get("strike")) or 0),
            str(row.get("instrument_type", "")),
        ))
        return output

    def _record_discovery_rejections(self, run_id: str,
                                     rejected: Sequence[Mapping[str, Any]]) -> None:
        for item in rejected:
            self.archive._reject(
                run_id, "contract", str(item.get("reason", "discovery_rejection")),
                raw=item.get("raw"),
            )
        if rejected:
            self.archive.connection.commit()

    @staticmethod
    def _quote_from_payload(instrument: Mapping[str, Any],
                            payload: Mapping[str, Any]) -> Dict[str, Any]:
        depth = payload.get("depth") or {}
        buys = depth.get("buy", []) or []
        sells = depth.get("sell", []) or []
        first_buy = buys[0] if buys and isinstance(buys[0], Mapping) else {}
        first_sell = sells[0] if sells and isinstance(sells[0], Mapping) else {}
        return {
            "trading_symbol": instrument.get("tradingsymbol"),
            "instrument_token": instrument.get("instrument_token"),
            "bid": payload.get("bid", first_buy.get("price")),
            "ask": payload.get("ask", first_sell.get("price")),
            "last_price": payload.get("last_price", payload.get("lastPrice")),
            "open_interest": payload.get("open_interest", payload.get("oi")),
            "volume": payload.get("volume"),
            # This is the provider value, never a candle value.
            "timestamp": payload.get("timestamp"),
            # Keep the complete depth in raw_json for auditability.
            "depth": depth,
        }

    @staticmethod
    def _quote_payload(payloads: Any, symbol: str) -> Optional[Mapping[str, Any]]:
        if not isinstance(payloads, Mapping):
            return None
        for key in (f"NFO:{symbol}", symbol):
            value = payloads.get(key)
            if isinstance(value, Mapping):
                return value
        return None

    def _capture_quotes(self, instruments: Sequence[Mapping[str, Any]], run_id: str,
                        observed_at: datetime, errors: List[Dict[str, Any]]) -> Dict[str, int]:
        counts = {"batches": 0, "batch_errors": 0, "contracts": 0,
                  "inserted": 0, "duplicates": 0, "rejected": 0}
        if not self.settings.collect_quotes:
            return counts
        width = self.settings.quote_batch_size
        for start in range(0, len(instruments), width):
            batch = list(instruments[start:start + width])
            symbols = [str(row.get("tradingsymbol")) for row in batch]
            counts["batches"] += 1
            payloads: Mapping[str, Any] = {}
            try:
                payloads = self._provider_call(
                    "quote_batch",
                    lambda symbols=symbols: self.adapter.quote("NFO", symbols),
                    errors,
                ) or {}
            except KeyboardInterrupt:
                raise
            except Exception:
                counts["batch_errors"] += 1
                # A failed batch must not discard contracts that can still be
                # sampled individually.  Each fallback has its own retries.
                for instrument in batch:
                    symbol = str(instrument.get("tradingsymbol"))
                    try:
                        single = self._provider_call(
                            "quote_contract",
                            lambda symbol=symbol: self.adapter.quote("NFO", [symbol]),
                            errors, symbol=symbol,
                        ) or {}
                        item = self._quote_payload(single, symbol)
                        if item is not None:
                            payloads = dict(payloads)
                            payloads[f"NFO:{symbol}"] = item
                    except KeyboardInterrupt:
                        raise
                    except Exception:
                        # The operation already has a bounded error record.
                        continue
            for instrument in batch:
                symbol = str(instrument.get("tradingsymbol"))
                payload = self._quote_payload(payloads, symbol)
                if payload is None:
                    counts["rejected"] += 1
                    errors.append({
                        "scope": "quote_contract", "trading_symbol": symbol,
                        "error_type": "MissingQuote", "error": "quote_missing",
                    })
                    self.archive._reject(
                        run_id, "quote", "missing_quote_payload", raw=instrument)
                    continue
                try:
                    quote = self._quote_from_payload(instrument, payload)
                    if quote.get("timestamp") in (None, ""):
                        errors.append({
                            "scope": "quote_contract", "trading_symbol": symbol,
                            "error_type": "MissingProviderTimestamp",
                            "error": "provider quote timestamp missing; preserved as null",
                        })
                    counts["contracts"] += 1
                    stats = self.archive.append_quote(
                        instrument, quote, observed_at=observed_at, run_id=run_id)
                except Exception as exc:
                    counts["rejected"] += 1
                    errors.append(_safe_error(
                        "quote_archive", exc, symbol=symbol))
                    self.archive._reject(
                        run_id, "quote", "quote_archive_error", raw=instrument,
                        detail=_safe_error("quote_archive", exc, symbol=symbol)["error"])
                    continue
                counts["inserted"] += stats.inserted
                counts["duplicates"] += stats.duplicates
                counts["rejected"] += stats.rejected
        self.archive.connection.commit()
        return counts

    @staticmethod
    def _alignment(archive: SQLiteOptionArchive,
                   underlyings: Sequence[str], interval: str) -> Dict[str, Any]:
        """Measure exact provider-timestamp matches without changing timestamps."""
        connection = getattr(archive, "connection", None)
        if connection is None:
            return {
                "status": "unavailable", "quote_rows": 0,
                "quotes_with_provider_timestamp": 0,
                "exact_timestamp_matches": 0, "coverage_percent": None,
            }
        marks = ",".join("?" for _ in underlyings)
        row = connection.execute(
            f"""SELECT COUNT(*) AS quote_rows,
                       SUM(CASE WHEN q.source_timestamp_ms IS NOT NULL THEN 1 ELSE 0 END)
                           AS timestamped,
                       SUM(CASE WHEN q.source_timestamp_ms IS NOT NULL
                                  AND c.timestamp_ms IS NOT NULL THEN 1 ELSE 0 END)
                           AS matched
                FROM quotes q
                JOIN contracts co ON co.contract_id=q.contract_id
                LEFT JOIN candles c
                  ON c.contract_id=q.contract_id
                 AND c.interval=?
                 AND c.timestamp_ms=q.source_timestamp_ms
                WHERE co.underlying IN ({marks})""",
            [interval, *underlyings],
        ).fetchone()
        quote_rows = int(row["quote_rows"] or 0)
        timestamped = int(row["timestamped"] or 0)
        matched = int(row["matched"] or 0)
        return {
            "status": "measured",
            "method": "exact provider source_timestamp_ms == candle timestamp_ms",
            "quote_rows": quote_rows,
            "quotes_with_provider_timestamp": timestamped,
            "exact_timestamp_matches": matched,
            "coverage_percent": (round(100.0 * matched / timestamped, 2)
                                  if timestamped else None),
            "observed_at_field": "quotes.observed_at_utc",
            "provider_timestamp_field": "quotes.source_timestamp_raw",
            "candle_timestamp_field": "candles.timestamp_utc",
            "timestamps_fabricated": False,
        }

    def capture_once(self, *, as_of: Optional[datetime] = None) -> Dict[str, Any]:
        current = _as_ist(as_of or self.now())
        settings = self.settings
        run_id = self.archive.start_run(settings.underlyings, settings.interval, settings.period)
        errors: List[Dict[str, Any]] = []
        discovery_rejections: List[Dict[str, Any]] = []
        instruments: List[Dict[str, Any]] = []
        historical_success = 0
        candle_counts = {"seen": 0, "inserted": 0, "duplicates": 0,
                         "rejected": 0, "conflicts": 0}
        quote_counts: Dict[str, int] = {}
        interrupted = False
        status = "complete"
        partial_selection = False
        contract_stats = {
            "seen": 0, "inserted": 0, "duplicates": 0, "conflicts": 0,
            "rejected": 0, "errors": [],
        }
        try:
            try:
                rows = self._provider_call(
                    "instrument_snapshot",
                    lambda: self.adapter.instruments("NFO"),
                    errors,
                    record_error=False,
                ) or []
                instruments = self._exact_contracts(
                    list(rows), now=current, underlyings=settings.underlyings,
                    strike_min=settings.strike_min, strike_max=settings.strike_max,
                    rejected=discovery_rejections,
                )
                partial_selection = bool(
                    settings.max_contracts is not None
                    and len(instruments) > settings.max_contracts
                )
                if settings.max_contracts is not None:
                    instruments = instruments[:settings.max_contracts]
                self._record_discovery_rejections(run_id, discovery_rejections)
                contract_stats = self.archive.upsert_contracts(
                    instruments, seen_at=current, run_id=run_id).to_dict()
            except KeyboardInterrupt:
                interrupted = True
                status = "stopped"
            except Exception as exc:
                status = "partial"
                errors.append(_safe_error("contract_discovery", exc))

            if not interrupted:
                for instrument in instruments:
                    symbol = str(instrument.get("tradingsymbol"))
                    try:
                        rows = self._provider_call(
                            "historical", lambda symbol=symbol: self.adapter.historical(
                                "NFO", symbol, interval=settings.interval,
                                period=settings.period), errors, symbol=symbol,
                            record_error=False)
                        stats = self.archive.append_candles(
                            instrument, rows, interval=settings.interval,
                            fetched_at=current, run_id=run_id)
                        historical_success += 1
                        for key in candle_counts:
                            candle_counts[key] += getattr(stats, key)
                    except KeyboardInterrupt:
                        interrupted = True
                        status = "stopped"
                        break
                    except Exception as exc:
                        status = "partial"
                        errors.append(_safe_error("historical", exc, symbol=symbol))
                        self.archive._reject(
                            run_id, "candle", "historical_fetch_error",
                            raw=instrument,
                            detail=_safe_error("historical", exc, symbol=symbol)["error"])
                self.archive.connection.commit()

            if not interrupted:
                try:
                    quote_counts = self._capture_quotes(
                        instruments, run_id, current, errors)
                except KeyboardInterrupt:
                    interrupted = True
                    status = "stopped"
                except Exception as exc:
                    status = "partial"
                    errors.append(_safe_error("quote_capture", exc))
            if errors and status == "complete":
                status = "partial"
        finally:
            try:
                self.archive.finish_run(
                    run_id, status=status,
                    errors=[item.get("error", "capture error") for item in errors],
                )
            except Exception as exc:
                errors.append(_safe_error("finish_run", exc))
                status = "partial"

        try:
            coverage = self.archive.coverage(
                underlyings=settings.underlyings, interval=settings.interval,
                partial_selection=bool(
                    partial_selection),
            )
        except Exception as exc:
            coverage = {"error": _safe_error("coverage", exc)}
            errors.append(coverage["error"])
        alignment = self._alignment(self.archive, settings.underlyings, settings.interval)
        report = {
            "ok": status == "complete",
            "provider": self.provider_name,
            "read_only": True,
            "status": status,
            "requested_underlyings": list(settings.underlyings),
            "selected_contracts": len(instruments),
            "contracts": [
                {
                    "instrument_token": row.get("instrument_token"),
                    "trading_symbol": row.get("tradingsymbol"),
                    "underlying": row.get("name"),
                    "expiry": _expiry(row.get("expiry")).isoformat()
                    if _expiry(row.get("expiry")) else None,
                    "strike": _finite(row.get("strike")),
                    "option_type": row.get("instrument_type"),
                    "lot_size": row.get("lot_size"),
                    "exchange": row.get("exchange", "NFO"),
                }
                for row in instruments
            ],
            "discovery_rejections": len(discovery_rejections),
            "partial_selection": partial_selection,
            "contract_stats": contract_stats,
            "historical_contracts_queried": historical_success,
            "historical_fetch_errors": sum(
                1 for item in errors if item.get("scope") == "historical"
            ),
            "quote": quote_counts,
            "candle": candle_counts,
            "coverage": coverage,
            "quote_candle_alignment": alignment,
            "errors": errors,
            "run_id": run_id,
            "orders_placed": 0,
        }
        report["run"] = {
            "run_id": run_id,
            "status": status,
            "requested_underlyings": list(settings.underlyings),
            "interval": settings.interval,
            "period": settings.period,
            "selected_contracts": len(instruments),
            "discovery_rejections": len(discovery_rejections),
            "partial_selection": partial_selection,
            "contract_stats": contract_stats,
            "candle_stats": candle_counts,
            "quote_stats": quote_counts,
            "historical_contracts_queried": historical_success,
            "orders_placed": 0,
        }
        return report

    def run(self, *, cycles: Optional[int] = 1, interval_seconds: float = 300.0,
            once: bool = False) -> Dict[str, Any]:
        """Run bounded capture cycles and stop cleanly on KeyboardInterrupt."""
        if cycles is not None and cycles < 1:
            raise ValueError("cycles must be positive or None")
        if interval_seconds < 0:
            raise ValueError("interval_seconds cannot be negative")
        target = 1 if once else cycles
        reports: List[Dict[str, Any]] = []
        stopped_reason = "completed"
        try:
            while target is None or len(reports) < target:
                report = self.capture_once()
                reports.append(report)
                if report.get("status") == "stopped":
                    stopped_reason = "keyboard_interrupt"
                    break
                if target is None or len(reports) < target:
                    if interval_seconds > 0:
                        self.sleep(interval_seconds)
        except KeyboardInterrupt:
            stopped_reason = "keyboard_interrupt"

        all_errors = [error for report in reports for error in report.get("errors", [])]
        counts = {
            "selected_contracts": sum(report.get("selected_contracts", 0) for report in reports),
            "historical_contracts_queried": sum(
                report.get("historical_contracts_queried", 0) for report in reports),
            "historical_fetch_errors": sum(
                report.get("historical_fetch_errors", 0) for report in reports),
            "candle_rows_inserted": sum(
                report.get("candle", {}).get("inserted", 0) for report in reports),
            "quote_rows_inserted": sum(
                report.get("quote", {}).get("inserted", 0) for report in reports),
        }
        return {
            "ok": bool(reports) and all(report.get("ok", False) for report in reports),
            "provider": self.provider_name,
            "read_only": True,
            "cycles": len(reports),
            "stopped_reason": stopped_reason,
            "counts": counts,
            "errors": all_errors,
            "cycle_reports": reports,
            "last_report": reports[-1] if reports else None,
            "orders_placed": 0,
        }


def capture_active_options(
    adapter: ZerodhaMarketData,
    archive: SQLiteOptionArchive,
    *,
    underlyings: Iterable[str] = DEFAULT_UNDERLYINGS,
    period: str = "30d",
    interval: str = DEFAULT_INTERVAL,
    max_contracts: Optional[int] = None,
    strike_min: Optional[float] = None,
    strike_max: Optional[float] = None,
    quote_batch_size: int = 200,
    collect_candles: bool = True,
    collect_quotes: bool = True,
    max_retries: int = 3,
    backoff_seconds: float = 1.0,
    rate_limit_seconds: float = 0.25,
    sleep: Callable[[float], Any] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(IST),
    as_of: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Convenience wrapper for one read-only capture cycle."""
    settings = CaptureSettings(
        underlyings=tuple(underlyings), interval=interval, period=period,
        max_contracts=max_contracts, strike_min=strike_min, strike_max=strike_max,
        quote_batch_size=quote_batch_size, collect_candles=collect_candles,
        collect_quotes=collect_quotes, max_retries=max_retries,
        backoff_seconds=backoff_seconds, rate_limit_seconds=rate_limit_seconds,
    )
    return ZerodhaLiveCapture(adapter, archive, settings, sleep=sleep, now=now).capture_once(
        as_of=as_of)


__all__ = [
    "CaptureSettings", "CaptureSQLiteOptionArchive", "ZerodhaLiveCapture",
    "capture_active_options", "default_archive_path",
]
