"""Historical option-premium backtesting from ``SQLiteOptionArchive``.

This module is intentionally read-only and deliberately small.  It consumes
only the archive's ``contracts``, ``candles`` and ``quotes`` tables.  Signals
are supplied by the caller (rows, a JSON-compatible object, or a deterministic
callback); the engine never derives a signal from option prices or from an
underlying replacement series.

The executable instrument is one fully identified option contract.  A signal
must identify ``underlying``, ``trading_symbol``, ``expiry``, ``strike``,
``option_type`` (``CE``/``PE``), and ``lot_size``.  The identity is matched
 exactly against archive metadata.  A BUY entry uses the ask from the next
 option candle strictly after the completed signal timestamp.  Exits use the
  bid from the same timestamp as the exit option candle.  The default staged
  policy starts with a -20% stop, ratchets at +10% and +20%, and closes at
  +30%; an open position is hard-flat on the last option candle of its entry
  day.  Exact quote matching remains the compatibility default;
 interval-aligned matching uses the actual source timestamp inside the candle
 interval and rejects observations made after that interval completes.

No broker, order, network, credential, Black-Scholes, or underlying-price
code is used here.  A path is opened with SQLite's read-only URI mode.  A
connection or an existing ``SQLiteOptionArchive`` object may also be supplied.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import quote as url_quote


IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
NOT_READY_FOR_OPTIONS_PNL = "NOT_READY_FOR_OPTIONS_PNL"
OPTIONS_PNL_READY = "OPTIONS_PNL_READY"
QUOTE_POLICY_EXACT = "exact"
QUOTE_POLICY_INTERVAL_ALIGNED = "interval_aligned"
EXIT_POLICY_STAGED_BID_LADDER = "staged_bid_ladder"
EXIT_POLICY_SINGLE_TARGET = "single_target"


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _positive_number(value: Any) -> Optional[float]:
    number = _finite_number(value)
    return number if number is not None and number > 0 else None


def _integer(value: Any) -> Optional[int]:
    number = _finite_number(value)
    if number is None or number <= 0 or not number.is_integer():
        return None
    return int(number)


def _field(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in row:
            return row[name]
        wanted = name.lower()
        for actual, value in row.items():
            if str(actual).lower() == wanted:
                return value
    return None


def _parse_datetime(value: Any, *, end_of_day: bool = False) -> Optional[datetime]:
    """Parse a signal/filter timestamp and normalize it to aware IST."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.max if end_of_day else time.min)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if not math.isfinite(number):
            return None
        # Accept either Unix seconds or archive-style Unix milliseconds.
        seconds = number / 1000.0 if abs(number) >= 100_000_000_000 else number
        parsed = datetime.fromtimestamp(seconds, UTC)
    else:
        text = str(value).strip().replace("Z", "+00:00")
        parsed = None
        try:
            # ``datetime.fromisoformat`` accepts a date-only string as
            # midnight; retain an inclusive end-of-day filter for CLI dates.
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                parsed = datetime.strptime(text, "%Y-%m-%d")
                if end_of_day:
                    parsed = parsed.replace(hour=23, minute=59, second=59,
                                            microsecond=999999)
            else:
                parsed = datetime.fromisoformat(text)
        except ValueError:
            for fmt in (
                "%Y-%m-%d %H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d",
                "%d-%b-%Y %H:%M:%S",
                "%d-%m-%Y %H:%M:%S",
            ):
                try:
                    parsed = datetime.strptime(text, fmt)
                    if fmt == "%Y-%m-%d" and end_of_day:
                        parsed = parsed.replace(hour=23, minute=59, second=59,
                                                microsecond=999999)
                    break
                except ValueError:
                    continue
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def _parse_ms(value: Any, *, end_of_day: bool = False) -> Optional[int]:
    parsed = _parse_datetime(value, end_of_day=end_of_day)
    return None if parsed is None else int(round(parsed.astimezone(UTC).timestamp() * 1000))


def _parse_expiry(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _iso_ms(timestamp_ms: Optional[int]) -> Optional[str]:
    if timestamp_ms is None:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000, UTC).astimezone(IST).isoformat()


def _interval_milliseconds(interval: str) -> Optional[int]:
    """Return the duration represented by an archive candle interval."""
    normalized = str(interval).strip().lower()
    minutes = {
        "minute": 1,
        "1minute": 1,
        "1m": 1,
        "3minute": 3,
        "3m": 3,
        "5minute": 5,
        "5m": 5,
        "10minute": 10,
        "10m": 10,
        "15minute": 15,
        "15m": 15,
        "30minute": 30,
        "30m": 30,
        "60minute": 60,
        "60m": 60,
        "hour": 60,
        "1hour": 60,
        "1h": 60,
    }
    if normalized in minutes:
        return minutes[normalized] * 60_000
    if normalized in {"day", "1day", "1d"}:
        return 24 * 60 * 60_000
    return None


def _normalize_quote_policy(value: Any) -> str:
    normalized = str(value or QUOTE_POLICY_EXACT).strip().lower().replace("-", "_")
    aliases = {
        "exact": QUOTE_POLICY_EXACT,
        "exact_timestamp": QUOTE_POLICY_EXACT,
        "candle_start": QUOTE_POLICY_EXACT,
        "interval": QUOTE_POLICY_INTERVAL_ALIGNED,
        "interval_aligned": QUOTE_POLICY_INTERVAL_ALIGNED,
        "interval_alignment": QUOTE_POLICY_INTERVAL_ALIGNED,
        "source_timestamp_interval": QUOTE_POLICY_INTERVAL_ALIGNED,
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ValueError(
            "quote_policy must be 'exact' or 'interval_aligned'"
        ) from exc


def _normalize_exit_policy(value: Any) -> str:
    normalized = str(value or EXIT_POLICY_STAGED_BID_LADDER).strip().lower().replace("-", "_")
    aliases = {
        "staged": EXIT_POLICY_STAGED_BID_LADDER,
        "staged_bid": EXIT_POLICY_STAGED_BID_LADDER,
        "staged_bid_ladder": EXIT_POLICY_STAGED_BID_LADDER,
        "ladder": EXIT_POLICY_STAGED_BID_LADDER,
        "single": EXIT_POLICY_SINGLE_TARGET,
        "single_target": EXIT_POLICY_SINGLE_TARGET,
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ValueError(
            "exit_policy must be 'staged_bid_ladder' or 'single_target'"
        ) from exc


def _trading_day(timestamp_ms: int) -> date:
    return datetime.fromtimestamp(timestamp_ms / 1000, UTC).astimezone(IST).date()


def _json_number(value: Any) -> Any:
    """Keep report numbers JSON-safe without leaking NaN/Infinity."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


@dataclass(frozen=True)
class TransactionCosts:
    """Explicit per-trade transaction assumptions.

    ``brokerage_per_order`` is a flat amount per entry or exit order.
    ``slippage_bps`` is applied adversely to each side of the spread.
    ``tax_rate`` is applied to executed entry plus exit turnover.
    """

    brokerage_per_order: float = 0.0
    slippage_bps: float = 0.0
    tax_rate: float = 0.0

    def validate(self) -> None:
        for name, value in (
            ("brokerage_per_order", self.brokerage_per_order),
            ("slippage_bps", self.slippage_bps),
            ("tax_rate", self.tax_rate),
        ):
            if _finite_number(value) is None or float(value) < 0:
                raise ValueError(f"{name} must be a finite non-negative number")


@dataclass(frozen=True)
class BacktestConfig:
    """Backtest controls independent from the archive and signal source."""

    underlyings: Sequence[str] = field(default_factory=tuple)
    start: Any = None
    end: Any = None
    interval: str = "5minute"
    stop_fraction: float = 0.20
    target_fraction: float = 0.30
    profit_step_fraction: float = 0.10
    exit_policy: str = EXIT_POLICY_STAGED_BID_LADDER
    costs: TransactionCosts = field(default_factory=TransactionCosts)
    capital: Optional[float] = None
    risk_fraction: float = 0.01
    max_lots: int = 1
    quote_policy: str = QUOTE_POLICY_EXACT


@dataclass
class _ContractData:
    contract: dict[str, Any]
    candles: list[dict[str, Any]] = field(default_factory=list)
    quotes_by_timestamp: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    candle_timestamps: set[int] = field(default_factory=set)
    invalid_candles: int = 0
    quote_rows: int = 0
    aligned_quote_rows: int = 0
    exact_quote_rows: int = 0
    interval_aligned_quote_rows: int = 0
    misaligned_quote_rows: int = 0
    invalid_aligned_quotes: int = 0
    quotes_observed_after_candle_completion: int = 0


class _ArchiveError(RuntimeError):
    pass


def _rows(connection: sqlite3.Connection, statement: str,
          parameters: Sequence[Any] = ()) -> list[dict[str, Any]]:
    cursor = connection.execute(statement, tuple(parameters))
    names = [item[0] for item in cursor.description or ()]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _source_connection(source: Any) -> tuple[sqlite3.Connection, bool, Optional[str]]:
    """Return a connection without creating a database for path inputs."""
    if isinstance(source, sqlite3.Connection):
        return source, False, None
    archive_connection = getattr(source, "connection", None)
    if isinstance(archive_connection, sqlite3.Connection):
        path = getattr(source, "path", None)
        return archive_connection, False, str(path) if path is not None else None
    if source is None:
        raise _ArchiveError("SQLite archive path or connection is required")
    path = Path(source).expanduser()
    if not path.exists() or not path.is_file():
        raise _ArchiveError(f"SQLite archive does not exist: {path}")
    # URI mode=ro is important: a typo must not create a new empty archive.
    uri_path = url_quote(str(path.resolve()), safe="/:\\")
    connection = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)
    return connection, True, str(path)


def _required_schema(connection: sqlite3.Connection) -> None:
    expected = {
        "contracts": {
            "contract_id", "underlying", "trading_symbol", "expiry_date",
            "strike", "option_type", "lot_size",
        },
        "candles": {
            "contract_id", "interval", "timestamp_ms", "open", "high",
            "low", "close",
        },
        "quotes": {
            "contract_id", "observed_at_ms", "source_timestamp_ms", "bid",
            "ask",
        },
    }
    actual_tables = {
        row["name"] for row in _rows(
            connection,
            "SELECT name FROM sqlite_master WHERE type='table'",
        )
    }
    missing_tables = sorted(set(expected) - actual_tables)
    if missing_tables:
        raise _ArchiveError("archive schema missing table(s): " + ", ".join(missing_tables))
    missing_columns: list[str] = []
    for table, required in expected.items():
        columns = {
            row["name"] for row in _rows(connection, f"PRAGMA table_info({table})")
        }
        missing_columns.extend(
            f"{table}.{column}" for column in sorted(required - columns)
        )
    if missing_columns:
        raise _ArchiveError(
            "archive schema missing column(s): " + ", ".join(missing_columns)
        )


def _normalize_costs(value: Any) -> TransactionCosts:
    if value is None:
        return TransactionCosts()
    if isinstance(value, TransactionCosts):
        value.validate()
        return value
    if isinstance(value, Mapping):
        costs = TransactionCosts(
            brokerage_per_order=float(value.get("brokerage_per_order", 0.0)),
            slippage_bps=float(value.get("slippage_bps", 0.0)),
            tax_rate=float(value.get("tax_rate", 0.0)),
        )
        costs.validate()
        return costs
    raise ValueError("costs must be TransactionCosts or a mapping")


def _normalize_config(
    config: Optional[BacktestConfig | Mapping[str, Any]],
    *,
    underlyings: Optional[Iterable[str]],
    start: Any,
    end: Any,
    interval: Optional[str],
    stop_fraction: Optional[float],
    target_fraction: Optional[float],
    profit_step_fraction: Optional[float],
    exit_policy: Optional[str],
    costs: Any,
    capital: Optional[float],
    risk_fraction: Optional[float],
    max_lots: Optional[int],
    quote_policy: Optional[str],
    quote_alignment: Optional[str],
) -> BacktestConfig:
    if config is None:
        base: dict[str, Any] = {}
    elif isinstance(config, BacktestConfig):
        base = {
            "underlyings": config.underlyings,
            "start": config.start,
            "end": config.end,
            "interval": config.interval,
            "stop_fraction": config.stop_fraction,
            "target_fraction": config.target_fraction,
            "profit_step_fraction": config.profit_step_fraction,
            "exit_policy": config.exit_policy,
            "costs": config.costs,
            "capital": config.capital,
            "risk_fraction": config.risk_fraction,
            "max_lots": config.max_lots,
            "quote_policy": config.quote_policy,
        }
    elif isinstance(config, Mapping):
        base = dict(config)
    else:
        raise ValueError("config must be BacktestConfig or a mapping")

    if underlyings is not None:
        base["underlyings"] = tuple(underlyings)
    if start is not None:
        base["start"] = start
    if end is not None:
        base["end"] = end
    if interval is not None:
        base["interval"] = interval
    if stop_fraction is not None:
        base["stop_fraction"] = stop_fraction
    if target_fraction is not None:
        base["target_fraction"] = target_fraction
    if profit_step_fraction is not None:
        base["profit_step_fraction"] = profit_step_fraction
    if exit_policy is not None:
        base["exit_policy"] = exit_policy
    if costs is not None:
        base["costs"] = costs
    if capital is not None:
        base["capital"] = capital
    if risk_fraction is not None:
        base["risk_fraction"] = risk_fraction
    if max_lots is not None:
        base["max_lots"] = max_lots
    if quote_policy is not None:
        base["quote_policy"] = quote_policy
    elif quote_alignment is not None:
        base["quote_policy"] = quote_alignment

    normalized_underlyings = tuple(
        sorted({str(item).strip().upper() for item in base.get("underlyings", ()) if str(item).strip()})
    )
    normalized_quote_policy = _normalize_quote_policy(
        base.get("quote_policy", base.get("quote_alignment", QUOTE_POLICY_EXACT))
    )
    result = BacktestConfig(
        underlyings=normalized_underlyings,
        start=base.get("start"),
        end=base.get("end"),
        interval=str(base.get("interval", "5minute")).strip() or "5minute",
        stop_fraction=float(base.get("stop_fraction", 0.20)),
        target_fraction=float(base.get("target_fraction", 0.30)),
        profit_step_fraction=float(base.get("profit_step_fraction", 0.10)),
        exit_policy=_normalize_exit_policy(base.get("exit_policy", EXIT_POLICY_STAGED_BID_LADDER)),
        costs=_normalize_costs(base.get("costs")),
        capital=(None if base.get("capital") is None else float(base["capital"])),
        risk_fraction=float(base.get("risk_fraction", 0.01)),
        max_lots=int(base.get("max_lots", 1)),
        quote_policy=normalized_quote_policy,
    )
    if not result.underlyings:
        raise ValueError("at least one underlying is required")
    if result.stop_fraction <= 0 or result.target_fraction <= 0:
        raise ValueError("stop_fraction and target_fraction must be positive")
    if result.profit_step_fraction <= 0:
        raise ValueError("profit_step_fraction must be positive")
    if result.exit_policy == EXIT_POLICY_STAGED_BID_LADDER and result.target_fraction < result.profit_step_fraction:
        raise ValueError("target_fraction must be at least profit_step_fraction for staged exits")
    if result.capital is not None and (not math.isfinite(result.capital) or result.capital <= 0):
        raise ValueError("capital must be positive when supplied")
    if not math.isfinite(result.risk_fraction) or result.risk_fraction <= 0:
        raise ValueError("risk_fraction must be positive")
    if result.max_lots <= 0:
        raise ValueError("max_lots must be positive")
    if result.quote_policy == QUOTE_POLICY_INTERVAL_ALIGNED and _interval_milliseconds(result.interval) is None:
        raise ValueError("interval_aligned quote_policy requires a supported timed interval")
    result.costs.validate()
    start_ms = _parse_ms(result.start)
    end_ms = _parse_ms(result.end, end_of_day=True)
    if result.start is not None and start_ms is None:
        raise ValueError("invalid start timestamp")
    if result.end is not None and end_ms is None:
        raise ValueError("invalid end timestamp")
    if start_ms is not None and end_ms is not None and start_ms > end_ms:
        raise ValueError("start must not be after end")
    return result


def _empty_report(
    *,
    status: str = NOT_READY_FOR_OPTIONS_PNL,
    reason: Optional[str] = None,
    underlyings: Sequence[str] = (),
    start: Any = None,
    end: Any = None,
    interval: str = "5minute",
    archive_path: Optional[str] = None,
    signal_source: str = "unavailable",
    quote_policy: str = QUOTE_POLICY_EXACT,
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {"counts": {}, "rejected_data": []}
    if reason:
        diagnostics["counts"][reason] = 1
        diagnostics["rejected_data"].append({"reason": reason})
    zero_pnl = {
        "gross_pnl": 0.0,
        "net_pnl": 0.0,
        "brokerage": 0.0,
        "taxes": 0.0,
        "slippage": 0.0,
        "total_costs": 0.0,
        "closed_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "breakeven_trades": 0,
    }
    return {
        "status": status,
        "provenance": {
            "source": "SQLiteOptionArchive",
            "archive_path": archive_path,
            "signal_source": signal_source,
            "tables_read": ["contracts", "candles", "quotes"],
            "pricing": "actual_option_ohlc_and_aligned_historical_bid_ask",
            "quote_policy": quote_policy,
            "quote_timestamp_policy": quote_policy,
            "quote_alignment": quote_policy,
            "quote_timestamp_basis": (
                "exact_candle_start_effective_timestamp"
                if quote_policy == QUOTE_POLICY_EXACT
                else "source_timestamp_within_candle_interval"
            ),
            "black_scholes": False,
            "underlying_substitution": False,
            "network_used": False,
            "order_calls": 0,
        },
        "coverage": {
            "underlyings": list(underlyings),
            "interval": interval,
            "quote_policy": quote_policy,
            "quote_timestamp_policy": quote_policy,
            "quote_alignment": quote_policy,
            "start": start,
            "end": end,
            "contracts": 0,
            "candles": 0,
            "quotes": 0,
            "aligned_quotes": 0,
            "exact_quotes": 0,
            "interval_aligned_quotes": 0,
            "misaligned_quotes": 0,
            "quotes_observed_after_candle_completion": 0,
            "ambiguous_quotes": 0,
        },
        "diagnostics": diagnostics,
        "trade_ledger": [],
        "trades": [],
        "orders_placed": 0,
        "pnl": zero_pnl,
        "win_rate": 0.0,
        "profit_factor": None,
    }


def _normalize_signals(
    supplied: Any,
    callback: Optional[Callable[[], Iterable[Mapping[str, Any]]]],
    *,
    signal_source: str,
    start_ms: Optional[int],
    end_ms: Optional[int],
) -> tuple[list[dict[str, Any]], Counter[str], list[dict[str, Any]]]:
    counts: Counter[str] = Counter()
    rejected: list[dict[str, Any]] = []
    if supplied is None and callback is not None:
        try:
            supplied = callback()
        except Exception as exc:  # deterministic callback errors are data errors
            counts["signal_callback_error"] += 1
            rejected.append({"reason": "signal_callback_error", "detail": str(exc)})
            return [], counts, rejected
    if supplied is None:
        counts["missing_signals"] += 1
        rejected.append({"reason": "missing_signals"})
        return [], counts, rejected
    if isinstance(supplied, Mapping):
        for key in ("signals", "rows", "data"):
            if key in supplied:
                supplied = supplied[key]
                break
        else:
            supplied = [supplied]
    try:
        rows = list(supplied)
    except TypeError:
        counts["invalid_signals"] += 1
        rejected.append({"reason": "invalid_signals", "detail": "not iterable"})
        return [], counts, rejected

    normalized: list[dict[str, Any]] = []
    previous_timestamp: Optional[int] = None
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            counts["invalid_signal_row"] += 1
            rejected.append({"index": index, "reason": "invalid_signal_row"})
            continue
        row = dict(raw)
        nested = _field(row, "contract", "option")
        if isinstance(nested, Mapping):
            merged = dict(nested)
            merged.update(row)
            row = merged
        timestamp_ms = _parse_ms(_field(row, "timestamp", "signal_timestamp", "completed_at", "time", "date"))
        if timestamp_ms is None:
            counts["invalid_signal_timestamp"] += 1
            rejected.append({"index": index, "reason": "invalid_signal_timestamp"})
            continue
        if previous_timestamp is not None and timestamp_ms < previous_timestamp:
            counts["non_chronological_signal"] += 1
            rejected.append({"index": index, "reason": "non_chronological_signal",
                             "timestamp": _iso_ms(timestamp_ms)})
            continue
        previous_timestamp = timestamp_ms
        if start_ms is not None and timestamp_ms < start_ms:
            counts["signal_before_start"] += 1
            rejected.append({"index": index, "reason": "signal_before_start"})
            continue
        if end_ms is not None and timestamp_ms > end_ms:
            counts["signal_after_end"] += 1
            rejected.append({"index": index, "reason": "signal_after_end"})
            continue
        completed = _field(row, "completed", "bar_closed", "is_completed", "complete")
        if not _completed_signal(completed):
            counts["incomplete_underlying_signal"] += 1
            rejected.append({"index": index, "reason": "incomplete_underlying_signal"})
            continue

        underlying = str(_field(row, "underlying", "underlying_symbol") or "").strip().upper()
        symbol = str(_field(row, "trading_symbol", "tradingsymbol") or "").strip().upper()
        expiry = _parse_expiry(_field(row, "expiry", "expiry_date"))
        strike = _positive_number(_field(row, "strike"))
        lot_size = _integer(_field(row, "lot_size", "lotSize"))
        option_type = str(_field(row, "option_type", "type", "instrument_type") or "").strip().upper()
        action = str(_field(row, "action", "side", "order_side", "direction", "signal") or "").strip().upper()
        action_tokens = set(re.findall(r"[A-Z]+", action.replace("/", " ").replace("_", " ")))
        if not option_type:
            if "CE" in action_tokens:
                option_type = "CE"
            elif "PE" in action_tokens:
                option_type = "PE"
            elif action in {"BULLISH", "UP", "LONG"}:
                option_type = "CE"
            elif action in {"BEARISH", "DOWN", "SHORT"}:
                option_type = "PE"
        if not action:
            action = "BUY"
            action_tokens = {"BUY"}
        is_buy = "BUY" in action_tokens or action in {"LONG", "BULLISH", "BEARISH", "UP", "DOWN"}
        if not is_buy or "SELL" in action_tokens or "SHORT" in action_tokens:
            counts["non_buy_signal"] += 1
            rejected.append({"index": index, "reason": "non_buy_signal"})
            continue
        if not underlying or not symbol or expiry is None or strike is None or lot_size is None:
            counts["missing_signal_identity"] += 1
            rejected.append({"index": index, "reason": "missing_signal_identity"})
            continue
        if option_type not in {"CE", "PE"}:
            counts["invalid_option_type"] += 1
            rejected.append({"index": index, "reason": "invalid_option_type"})
            continue
        declared_entry = _field(row, "entry_timestamp", "entry_time")
        declared_entry_ms = _parse_ms(declared_entry) if declared_entry is not None else None
        if declared_entry is not None and (declared_entry_ms is None or declared_entry_ms <= timestamp_ms):
            counts["lookahead_signal"] += 1
            rejected.append({"index": index, "reason": "lookahead_signal"})
            continue
        normalized.append({
            "index": index,
            "timestamp_ms": timestamp_ms,
            "underlying": underlying,
            "trading_symbol": symbol,
            "expiry": expiry,
            "strike": strike,
            "option_type": option_type,
            "lot_size": lot_size,
            "action": "BUY",
            "signal_id": _field(row, "signal_id", "id") or f"signal-{index}",
        })
    return normalized, counts, rejected


def _completed_signal(value: Any) -> bool:
    """Accept only an explicit completed-bar marker.

    Missing, false, numeric zero, and forming/incomplete text are all rejected.
    A signal without this marker is not eligible for historical entry.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "completed", "complete", "closed"}
    return False


def _valid_ohlc(row: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    timestamp_ms = _integer(row.get("timestamp_ms"))
    # timestamp_ms can be zero for a legitimate epoch row; _integer rejects it,
    # while exchange data is always positive.  Keep the check explicit here.
    raw_timestamp = _finite_number(row.get("timestamp_ms"))
    if raw_timestamp is None or not raw_timestamp.is_integer() or raw_timestamp < 0:
        return None
    timestamp_ms = int(raw_timestamp)
    values = {name: _positive_number(row.get(name)) for name in ("open", "high", "low", "close")}
    if any(value is None for value in values.values()):
        return None
    if values["high"] < max(values["open"], values["low"], values["close"]):
        return None
    if values["low"] > min(values["open"], values["high"], values["close"]):
        return None
    return {"timestamp_ms": timestamp_ms, **values}


def _valid_quote(row: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    bid = _positive_number(row.get("bid"))
    ask = _positive_number(row.get("ask"))
    if bid is None or ask is None or bid >= ask:
        return None
    if row.get("executable") is not None and int(row.get("executable") or 0) != 1:
        return None
    observed_value = _finite_number(row.get("observed_at_ms"))
    source_value = _finite_number(row.get("source_timestamp_ms"))
    if (
        observed_value is None
        or not observed_value.is_integer()
        or observed_value < 0
        or (source_value is not None and (not source_value.is_integer() or source_value < 0))
    ):
        return None
    return {
        "bid": bid,
        "ask": ask,
        "quote_id": row.get("quote_id"),
        "observed_at_ms": int(observed_value),
        "source_timestamp_ms": None if source_value is None else int(source_value),
    }


def _reject(counts: Counter[str], rejected: list[dict[str, Any]], reason: str, **details: Any) -> None:
    counts[reason] += 1
    item = {"reason": reason}
    item.update({key: value for key, value in details.items() if value is not None})
    rejected.append(item)


def _contract_identity(row: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    lot_size = _integer(row.get("lot_size"))
    strike = _positive_number(row.get("strike"))
    expiry = _parse_expiry(row.get("expiry_date"))
    underlying = str(row.get("underlying") or "").strip().upper()
    symbol = str(row.get("trading_symbol") or "").strip().upper()
    option_type = str(row.get("option_type") or "").strip().upper()
    if not underlying or not symbol or expiry is None or strike is None or lot_size is None:
        return None
    if option_type not in {"CE", "PE"}:
        return None
    return {
        "contract_id": int(row["contract_id"]),
        "underlying": underlying,
        "trading_symbol": symbol,
        "expiry": expiry,
        "strike": strike,
        "option_type": option_type,
        "lot_size": lot_size,
    }


def _same_identity(contract: Mapping[str, Any], signal: Mapping[str, Any]) -> bool:
    return (
        contract["underlying"] == signal["underlying"]
        and contract["trading_symbol"] == signal["trading_symbol"]
        and contract["expiry"] == signal["expiry"]
        and math.isclose(float(contract["strike"]), float(signal["strike"]), rel_tol=0, abs_tol=1e-9)
        and contract["option_type"] == signal["option_type"]
        and contract["lot_size"] == signal["lot_size"]
    )


def _load_contract_data(
    connection: sqlite3.Connection,
    config: BacktestConfig,
    *,
    start_ms: Optional[int],
    end_ms: Optional[int],
    counts: Counter[str],
    rejected: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[int, _ContractData]]:
    marks = ",".join("?" for _ in config.underlyings)
    raw_contracts = _rows(
        connection,
        f"""SELECT contract_id, underlying, trading_symbol, expiry_date,
                   strike, option_type, lot_size
              FROM contracts WHERE UPPER(underlying) IN ({marks})
             ORDER BY underlying, expiry_date, strike, option_type, trading_symbol""",
        list(config.underlyings),
    )
    contracts: list[dict[str, Any]] = []
    data: dict[int, _ContractData] = {}
    for raw in raw_contracts:
        identity = _contract_identity(raw)
        if identity is None:
            _reject(counts, rejected, "invalid_contract_identity",
                    contract_id=raw.get("contract_id"))
            continue
        contracts.append(identity)
        contract_id = identity["contract_id"]
        candle_rows = _rows(
            connection,
            f"""SELECT timestamp_ms, open, high, low, close
                  FROM candles
                 WHERE contract_id=? AND interval=?
                   AND (? IS NULL OR timestamp_ms>=?)
                   AND (? IS NULL OR timestamp_ms<=?)
                 ORDER BY timestamp_ms""",
            (contract_id, config.interval, start_ms, start_ms, end_ms, end_ms),
        )
        item = _ContractData(contract=identity)
        for candle_row in candle_rows:
            candle = _valid_ohlc(candle_row)
            if candle is None:
                item.invalid_candles += 1
                _reject(counts, rejected, "invalid_option_ohlc", contract_id=contract_id,
                        timestamp=_iso_ms(int(candle_row["timestamp_ms"]))
                        if _finite_number(candle_row.get("timestamp_ms")) is not None else None)
                continue
            if item.candles and candle["timestamp_ms"] <= item.candles[-1]["timestamp_ms"]:
                _reject(counts, rejected, "non_chronological_option_candle",
                        contract_id=contract_id, timestamp=_iso_ms(candle["timestamp_ms"]))
                continue
            item.candles.append(candle)
            item.candle_timestamps.add(candle["timestamp_ms"])
        quote_rows = _rows(
            connection,
            f"""SELECT quote_id, observed_at_ms, source_timestamp_ms, bid, ask,
                          executable
                     FROM quotes
                    WHERE contract_id=?
                      AND (? IS NULL OR COALESCE(source_timestamp_ms, observed_at_ms)>=?)
                      AND (? IS NULL OR COALESCE(source_timestamp_ms, observed_at_ms)<=?)
                    ORDER BY COALESCE(source_timestamp_ms, observed_at_ms), quote_id""",
            (contract_id, start_ms, start_ms, end_ms, end_ms),
        )
        item.quote_rows = len(quote_rows)
        interval_ms = _interval_milliseconds(config.interval)
        candles_by_timestamp = {candle["timestamp_ms"]: candle for candle in item.candles}
        for quote_row in quote_rows:
            source_value = _finite_number(quote_row.get("source_timestamp_ms"))
            observed_value = _finite_number(quote_row.get("observed_at_ms"))
            source_timestamp: Optional[int] = None
            if source_value is not None and source_value.is_integer() and source_value >= 0:
                source_timestamp = int(source_value)
            observed_timestamp: Optional[int] = None
            if observed_value is not None and observed_value.is_integer() and observed_value >= 0:
                observed_timestamp = int(observed_value)
            if observed_timestamp is None or (
                quote_row.get("source_timestamp_ms") is not None and source_timestamp is None
            ):
                item.misaligned_quote_rows += 1
                _reject(counts, rejected, "invalid_quote_timestamp", contract_id=contract_id)
                continue
            if source_timestamp is not None and observed_timestamp < source_timestamp:
                item.misaligned_quote_rows += 1
                _reject(
                    counts,
                    rejected,
                    "quote_observed_before_source_timestamp",
                    contract_id=contract_id,
                    timestamp=_iso_ms(observed_timestamp),
                    source_timestamp=_iso_ms(source_timestamp),
                )
                continue

            if config.quote_policy == QUOTE_POLICY_INTERVAL_ALIGNED:
                if source_timestamp is None:
                    item.misaligned_quote_rows += 1
                    _reject(counts, rejected, "missing_quote_source_timestamp",
                            contract_id=contract_id,
                            timestamp=_iso_ms(observed_timestamp))
                    continue
                candle = next(
                    (
                        candidate for candidate in item.candles
                        if candidate["timestamp_ms"] <= source_timestamp
                        and interval_ms is not None
                        and source_timestamp < candidate["timestamp_ms"] + interval_ms
                    ),
                    None,
                )
                effective_timestamp = source_timestamp
                aligned = candle is not None
            else:
                effective_timestamp = (
                    source_timestamp if source_timestamp is not None else observed_timestamp
                )
                candle = candles_by_timestamp.get(effective_timestamp)
                aligned = candle is not None

            if not aligned or candle is None:
                item.misaligned_quote_rows += 1
                _reject(counts, rejected, "misaligned_quote", contract_id=contract_id,
                        timestamp=_iso_ms(effective_timestamp))
                continue

            item.aligned_quote_rows += 1
            if config.quote_policy == QUOTE_POLICY_EXACT:
                item.exact_quote_rows += 1
            else:
                item.interval_aligned_quote_rows += 1

            candle_completion = candle["timestamp_ms"] + (interval_ms or 0)
            if interval_ms is not None and observed_timestamp > candle_completion:
                item.quotes_observed_after_candle_completion += 1
                _reject(
                    counts,
                    rejected,
                    "quote_observed_after_candle_completion",
                    contract_id=contract_id,
                    timestamp=_iso_ms(observed_timestamp),
                    candle_timestamp=_iso_ms(candle["timestamp_ms"]),
                )
                continue

            valid_quote = _valid_quote({
                **quote_row,
                "observed_at_ms": observed_timestamp,
                "source_timestamp_ms": source_timestamp,
            })
            if valid_quote is None:
                item.invalid_aligned_quotes += 1
                _reject(counts, rejected, "invalid_aligned_quote", contract_id=contract_id,
                        timestamp=_iso_ms(effective_timestamp))
                continue
            item.quotes_by_timestamp.setdefault(candle["timestamp_ms"], []).append(valid_quote)
        data[contract_id] = item
    return contracts, data


def _size_position(
    entry_price: float,
    contract: Mapping[str, Any],
    config: BacktestConfig,
) -> Optional[dict[str, Any]]:
    lot_size = int(contract["lot_size"])
    risk_per_unit = entry_price * config.stop_fraction
    risk_per_lot = risk_per_unit * lot_size
    if config.capital is None:
        lots = min(1, config.max_lots)
        risk_budget = None
    else:
        risk_budget = config.capital * config.risk_fraction
        lots = min(config.max_lots, int(risk_budget // risk_per_lot)) if risk_per_lot > 0 else 0
    if lots <= 0:
        return None
    return {
        "lots": lots,
        "quantity": lots * lot_size,
        "lot_size": lot_size,
        "risk_per_unit": risk_per_unit,
        "risk_per_lot": risk_per_lot,
        "risk_budget": risk_budget,
    }


def _one_quote(
    item: _ContractData,
    timestamp_ms: int,
    counts: Counter[str],
    rejected: list[dict[str, Any]],
    *,
    quote_policy: str = QUOTE_POLICY_EXACT,
) -> Optional[dict[str, Any]]:
    rows = item.quotes_by_timestamp.get(timestamp_ms, [])
    if not rows:
        _reject(counts, rejected, "missing_aligned_quote",
                contract_id=item.contract["contract_id"], timestamp=_iso_ms(timestamp_ms))
        return None
    if len(rows) != 1:
        reason = (
            "ambiguous_interval_quote"
            if quote_policy == QUOTE_POLICY_INTERVAL_ALIGNED
            else "ambiguous_aligned_quote"
        )
        _reject(counts, rejected, reason,
                contract_id=item.contract["contract_id"], timestamp=_iso_ms(timestamp_ms),
                candidate_count=len(rows))
        if reason != "ambiguous_aligned_quote":
            counts["ambiguous_aligned_quote"] += 1
        counts["ambiguous_quote_candidates"] += 1
        return None
    return rows[0]


def _quote_ledger_fields(prefix: str, quote: Mapping[str, Any]) -> dict[str, Any]:
    """Keep execution prices separate from the quote's actual provenance."""
    observed_at_ms = quote.get("observed_at_ms")
    source_timestamp_ms = quote.get("source_timestamp_ms")
    return {
        f"{prefix}_quote_id": quote.get("quote_id"),
        f"{prefix}_quote_bid": quote.get("bid"),
        f"{prefix}_quote_ask": quote.get("ask"),
        f"{prefix}_quote_observed_at": _iso_ms(observed_at_ms),
        f"{prefix}_quote_source_timestamp": _iso_ms(source_timestamp_ms),
        f"{prefix}_quote_observed_at_ms": observed_at_ms,
        f"{prefix}_quote_source_timestamp_ms": source_timestamp_ms,
        # Direct aliases keep the ledger convenient for tabular consumers.
        f"{prefix}_bid": quote.get("bid"),
        f"{prefix}_ask": quote.get("ask"),
        f"{prefix}_observed_at": _iso_ms(observed_at_ms),
        f"{prefix}_source_timestamp": _iso_ms(source_timestamp_ms),
    }


def _staged_bid_exit(
    same_day: Sequence[dict[str, Any]],
    item: _ContractData,
    entry_price: float,
    config: BacktestConfig,
    counts: Counter[str],
    rejected: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Apply the BUY option staged bid ladder without using candle OHLC.

    The returned state keeps the complete ladder auditable.  +10% and +20%
    are ratchet milestones, not partial exits; the full quantity closes at a
    ratcheted stop, the final target, or same-day hard-flat.
    """
    final_target_fraction = float(config.target_fraction)
    step = float(config.profit_step_fraction)
    target_fractions: list[float] = []
    fraction = step
    while fraction < final_target_fraction and not math.isclose(
        fraction, final_target_fraction, rel_tol=0.0, abs_tol=1e-12
    ):
        target_fractions.append(fraction)
        fraction += step
    if not target_fractions or not math.isclose(
        target_fractions[-1], final_target_fraction, rel_tol=0.0, abs_tol=1e-12
    ):
        target_fractions.append(final_target_fraction)

    reached_stage = 0
    current_stop = entry_price * (1.0 - config.stop_fraction)
    active_target = entry_price * (1.0 + target_fractions[0])
    final_target = entry_price * (1.0 + final_target_fraction)

    for candle_index, candle in enumerate(same_day):
        quote = _one_quote(
            item,
            candle["timestamp_ms"],
            counts,
            rejected,
            quote_policy=config.quote_policy,
        )
        if quote is None:
            _reject(
                counts,
                rejected,
                "missing_intermediate_exit_bid",
                contract_id=item.contract["contract_id"],
                timestamp=_iso_ms(candle["timestamp_ms"]),
            )
            return None

        bid = float(quote["bid"])
        if bid < current_stop or math.isclose(
            bid, current_stop, rel_tol=0.0, abs_tol=1e-9
        ):
            return {
                "exit_candle": candle,
                "exit_quote": quote,
                "exit_reason": "stop",
                "exit_trigger": (
                    "initial_stop"
                    if reached_stage == 0
                    else f"profit_lock_stop_{int(round(target_fractions[reached_stage - 1] * 100))}"
                ),
                "stop_price": current_stop,
                "target_price": active_target,
                "initial_stop_price": entry_price * (1.0 - config.stop_fraction),
                "final_target_price": final_target,
                "highest_profit_stage": reached_stage,
                "reached_profit_targets": target_fractions[:reached_stage],
            }

        if bid > final_target or math.isclose(
            bid, final_target, rel_tol=0.0, abs_tol=1e-9
        ):
            reached_stage = len(target_fractions)
            return {
                "exit_candle": candle,
                "exit_quote": quote,
                "exit_reason": "target",
                "exit_trigger": f"final_target_{int(round(final_target_fraction * 100))}",
                "stop_price": current_stop,
                "target_price": final_target,
                "initial_stop_price": entry_price * (1.0 - config.stop_fraction),
                "final_target_price": final_target,
                "highest_profit_stage": reached_stage,
                "reached_profit_targets": target_fractions[:reached_stage],
            }

        while reached_stage < len(target_fractions) and (
            bid > entry_price * (1.0 + target_fractions[reached_stage])
            or math.isclose(
                bid,
                entry_price * (1.0 + target_fractions[reached_stage]),
                rel_tol=0.0,
                abs_tol=1e-9,
            )
        ):
            reached_stage += 1
        if reached_stage:
            current_stop = entry_price * (1.0 + target_fractions[reached_stage - 1])
            active_target = (
                entry_price * (1.0 + target_fractions[reached_stage])
                if reached_stage < len(target_fractions)
                else final_target
            )

        if candle_index == len(same_day) - 1:
            return {
                "exit_candle": candle,
                "exit_quote": quote,
                "exit_reason": "same_day_hard_flat",
                "exit_trigger": "same_day_hard_flat",
                "stop_price": current_stop,
                "target_price": active_target,
                "initial_stop_price": entry_price * (1.0 - config.stop_fraction),
                "final_target_price": final_target,
                "highest_profit_stage": reached_stage,
                "reached_profit_targets": target_fractions[:reached_stage],
            }
    return None


def _trade_for_signal(
    signal: Mapping[str, Any],
    item: _ContractData,
    config: BacktestConfig,
    counts: Counter[str],
    rejected: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    signal_timestamp = int(signal["timestamp_ms"])
    entry: Optional[dict[str, Any]] = None
    entry_quote: Optional[dict[str, Any]] = None
    for candle in item.candles:
        if candle["timestamp_ms"] <= signal_timestamp:
            continue
        quote = _one_quote(
            item,
            candle["timestamp_ms"],
            counts,
            rejected,
            quote_policy=config.quote_policy,
        )
        if quote is None:
            _reject(
                counts,
                rejected,
                "unfillable_first_entry_candle",
                contract_id=item.contract["contract_id"],
                timestamp=_iso_ms(candle["timestamp_ms"]),
            )
            return None
        entry_observed = int(quote["observed_at_ms"])
        if entry_observed <= signal_timestamp:
            _reject(
                counts,
                rejected,
                "entry_quote_not_after_signal",
                contract_id=item.contract["contract_id"],
                timestamp=_iso_ms(entry_observed),
            )
            return None
        entry = candle
        entry_quote = quote
        break
    if entry is None or entry_quote is None:
        _reject(counts, rejected, "no_next_eligible_option_candle",
                contract_id=item.contract["contract_id"], timestamp=_iso_ms(signal_timestamp))
        return None
    # This is intentionally a strict check even though the candidate loop
    # already enforces it; it documents the no-lookahead invariant in the
    # ledger-producing path.
    if entry["timestamp_ms"] <= signal_timestamp:
        _reject(counts, rejected, "lookahead_entry", contract_id=item.contract["contract_id"])
        return None
    entry_price_raw = float(entry_quote["ask"])
    slip_factor = config.costs.slippage_bps / 10_000.0
    entry_price = entry_price_raw * (1.0 + slip_factor)
    sizing = _size_position(entry_price, item.contract, config)
    if sizing is None:
        _reject(counts, rejected, "risk_budget_below_one_lot",
                contract_id=item.contract["contract_id"])
        return None
    entry_day = _trading_day(entry["timestamp_ms"])
    same_day = [
        candle for candle in item.candles
        if candle["timestamp_ms"] > entry["timestamp_ms"]
        and _trading_day(candle["timestamp_ms"]) == entry_day
    ]
    if not same_day:
        _reject(counts, rejected, "no_same_day_exit_candle",
                contract_id=item.contract["contract_id"], timestamp=_iso_ms(entry["timestamp_ms"]))
        return None

    if config.exit_policy == EXIT_POLICY_STAGED_BID_LADDER:
        exit_state = _staged_bid_exit(
            same_day, item, entry_price, config, counts, rejected,
        )
    else:
        stop_price = entry_price * (1.0 - config.stop_fraction)
        target_price = entry_price * (1.0 + config.target_fraction)
        exit_state = None
        for candle in same_day:
            hit_stop = candle["low"] <= stop_price
            hit_target = candle["high"] >= target_price
            if not hit_stop and not hit_target and candle is not same_day[-1]:
                continue
            if hit_stop:
                exit_reason = "stop"
            elif hit_target:
                exit_reason = "target"
            else:
                exit_reason = "same_day_hard_flat"
            quote = _one_quote(
                item,
                candle["timestamp_ms"],
                counts,
                rejected,
                quote_policy=config.quote_policy,
            )
            if quote is None:
                _reject(
                    counts,
                    rejected,
                    "rejected_exit_without_aligned_bid",
                    contract_id=item.contract["contract_id"],
                    timestamp=_iso_ms(candle["timestamp_ms"]),
                )
                return None
            exit_state = {
                "exit_candle": candle,
                "exit_quote": quote,
                "exit_reason": exit_reason,
                "exit_trigger": exit_reason,
                "stop_price": stop_price,
                "target_price": target_price,
                "initial_stop_price": stop_price,
                "final_target_price": target_price,
                "highest_profit_stage": 0,
                "reached_profit_targets": [],
            }
            break

    if exit_state is None:
        _reject(counts, rejected, "unclosed_trade", contract_id=item.contract["contract_id"])
        return None
    exit_candle = exit_state["exit_candle"]
    exit_quote = exit_state["exit_quote"]
    exit_reason = exit_state["exit_reason"]
    if exit_candle["timestamp_ms"] <= entry["timestamp_ms"]:
        _reject(counts, rejected, "lookahead_exit", contract_id=item.contract["contract_id"])
        return None
    if int(exit_quote["observed_at_ms"]) <= int(entry_quote["observed_at_ms"]):
        _reject(
            counts,
            rejected,
            "exit_quote_not_after_entry",
            contract_id=item.contract["contract_id"],
        )
        return None
    exit_price_raw = float(exit_quote["bid"])
    exit_price = exit_price_raw * (1.0 - slip_factor)
    quantity = int(sizing["quantity"])
    # ``gross_pnl`` is the raw historical bid/ask result.  Slippage is shown
    # separately so the accounting identity remains
    # net_pnl = gross_pnl - total_costs.
    gross_pnl = (exit_price_raw - entry_price_raw) * quantity
    execution_pnl_after_slippage = (exit_price - entry_price) * quantity
    turnover = (entry_price + exit_price) * quantity
    brokerage = config.costs.brokerage_per_order * 2.0
    taxes = turnover * config.costs.tax_rate
    slippage = ((entry_price - entry_price_raw) + (exit_price_raw - exit_price)) * quantity
    total_costs = brokerage + taxes + slippage
    net_pnl = gross_pnl - total_costs
    trade_id = (
        f"{item.contract['trading_symbol']}:{signal['timestamp_ms']}:{entry['timestamp_ms']}"
    )
    return {
        "trade_id": trade_id,
        "status": "closed",
        "side": "BUY",
        "option_type": item.contract["option_type"],
        "underlying": item.contract["underlying"],
        "trading_symbol": item.contract["trading_symbol"],
        "expiry": item.contract["expiry"],
        "strike": item.contract["strike"],
        "lot_size": item.contract["lot_size"],
        "lots": sizing["lots"],
        "quantity": quantity,
        "signal_timestamp": _iso_ms(signal_timestamp),
        "entry_timestamp": _iso_ms(entry["timestamp_ms"]),
        "exit_timestamp": _iso_ms(exit_candle["timestamp_ms"]),
        "entry_price_raw_ask": entry_price_raw,
        "entry_price": entry_price,
        "exit_price_raw_bid": exit_price_raw,
        "exit_price": exit_price,
        "execution_pnl_after_slippage": execution_pnl_after_slippage,
        "stop_price": exit_state["stop_price"],
        "target_price": exit_state["target_price"],
        "initial_stop_price": exit_state["initial_stop_price"],
        "final_target_price": exit_state["final_target_price"],
        "exit_policy": config.exit_policy,
        "exit_trigger": exit_state["exit_trigger"],
        "highest_profit_stage": exit_state["highest_profit_stage"],
        "reached_profit_targets": exit_state["reached_profit_targets"],
        "exit_reason": exit_reason,
        "risk_per_lot": sizing["risk_per_lot"],
        "risk_budget": sizing["risk_budget"],
        "gross_pnl": gross_pnl,
        "brokerage": brokerage,
        "taxes": taxes,
        "slippage": slippage,
        "total_costs": total_costs,
        "net_pnl": net_pnl,
        **_quote_ledger_fields("entry", entry_quote),
        **_quote_ledger_fields("exit", exit_quote),
    }


def _finalize_report(
    report: dict[str, Any],
    trades: list[dict[str, Any]],
    counts: Counter[str],
    rejected: list[dict[str, Any]],
) -> dict[str, Any]:
    gross = sum(float(item["gross_pnl"]) for item in trades)
    brokerage = sum(float(item["brokerage"]) for item in trades)
    taxes = sum(float(item["taxes"]) for item in trades)
    slippage = sum(float(item["slippage"]) for item in trades)
    total_costs = sum(float(item["total_costs"]) for item in trades)
    net = sum(float(item["net_pnl"]) for item in trades)
    wins = sum(1 for item in trades if item["net_pnl"] > 0)
    losses = sum(1 for item in trades if item["net_pnl"] < 0)
    breakeven = len(trades) - wins - losses
    gross_profit = sum(float(item["net_pnl"]) for item in trades if item["net_pnl"] > 0)
    gross_loss = sum(float(item["net_pnl"]) for item in trades if item["net_pnl"] < 0)
    report["trade_ledger"] = trades
    report["trades"] = trades
    report["coverage"]["ambiguous_quotes"] = counts.get(
        "ambiguous_quote_candidates", 0
    )
    report["pnl"] = {
        "gross_pnl": gross,
        "net_pnl": net,
        "brokerage": brokerage,
        "taxes": taxes,
        "slippage": slippage,
        "total_costs": total_costs,
        "closed_trades": len(trades),
        "winning_trades": wins,
        "losing_trades": losses,
        "breakeven_trades": breakeven,
    }
    report["win_rate"] = wins / len(trades) if trades else 0.0
    report["profit_factor"] = (
        gross_profit / abs(gross_loss) if gross_loss < 0 else None
    )
    report["diagnostics"] = {"counts": dict(sorted(counts.items())),
                             "rejected_data": rejected}
    eligible_failures = sum(
        counts.get(reason, 0)
        for reason in (
            "unfillable_first_entry_candle",
            "missing_intermediate_exit_bid",
            "rejected_exit_without_aligned_bid",
            "quote_observed_before_source_timestamp",
            "incomplete_underlying_signal",
            "missing_contract_identity",
            "ambiguous_contract_identity",
            "quote_candle_alignment_failed",
            "unclosed_trade",
            "entry_quote_not_after_signal",
            "exit_quote_not_after_entry",
        )
    )
    coverage_complete = bool(trades) and eligible_failures == 0 and not rejected
    report["coverage"]["coverage_complete"] = coverage_complete
    report["coverage"]["partial_data"] = bool(trades) and not coverage_complete
    report["coverage"]["profitability_claim_allowed"] = False
    report["coverage"]["eligible_data_failures"] = eligible_failures
    report["status"] = OPTIONS_PNL_READY if trades else NOT_READY_FOR_OPTIONS_PNL
    if not trades:
        # A not-ready report must not accidentally retain a partial calculation.
        report["pnl"] = {
            "gross_pnl": 0.0, "net_pnl": 0.0, "brokerage": 0.0,
            "taxes": 0.0, "slippage": 0.0, "total_costs": 0.0,
            "closed_trades": 0, "winning_trades": 0, "losing_trades": 0,
            "breakeven_trades": 0,
        }
        report["win_rate"] = 0.0
        report["profit_factor"] = None
    return report


def run_options_premium_backtest(
    source: Any,
    signals: Any = None,
    *,
    underlyings: Optional[Iterable[str]] = None,
    start: Any = None,
    end: Any = None,
    signal_callback: Optional[Callable[[], Iterable[Mapping[str, Any]]]] = None,
    config: Optional[BacktestConfig | Mapping[str, Any]] = None,
    interval: Optional[str] = None,
    stop_fraction: Optional[float] = None,
    target_fraction: Optional[float] = None,
    profit_step_fraction: Optional[float] = None,
    exit_policy: Optional[str] = None,
    costs: Any = None,
    brokerage_per_order: Optional[float] = None,
    slippage_bps: Optional[float] = None,
    tax_rate: Optional[float] = None,
    capital: Optional[float] = None,
    risk_fraction: Optional[float] = None,
    max_lots: Optional[int] = None,
    quote_policy: Optional[str] = None,
    quote_alignment: Optional[str] = None,
) -> dict[str, Any]:
    """Run a read-only historical option-premium backtest.

    ``source`` is a ``sqlite3.Connection``, an existing archive object with a
    ``connection`` attribute, or a filesystem path.  For convenience,
    individual cost fields can be supplied directly; they are explicit model
    inputs and default to zero rather than being silently guessed.
    """
    requested_underlyings = tuple(
        sorted({str(item).strip().upper() for item in (underlyings or ()) if str(item).strip()})
    )
    archive_path = str(getattr(source, "path", "")) or (str(source) if isinstance(source, (str, Path)) else None)
    signal_source = "callback" if signal_callback is not None and signals is None else "rows"
    try:
        direct_costs = costs
        if any(value is not None for value in (brokerage_per_order, slippage_bps, tax_rate)):
            existing = _normalize_costs(costs)
            direct_costs = TransactionCosts(
                brokerage_per_order=(existing.brokerage_per_order if brokerage_per_order is None else float(brokerage_per_order)),
                slippage_bps=(existing.slippage_bps if slippage_bps is None else float(slippage_bps)),
                tax_rate=(existing.tax_rate if tax_rate is None else float(tax_rate)),
            )
        normalized_config = _normalize_config(
            config,
            underlyings=(requested_underlyings or None),
            start=start,
            end=end,
            interval=interval,
            stop_fraction=stop_fraction,
            target_fraction=target_fraction,
            profit_step_fraction=profit_step_fraction,
            exit_policy=exit_policy,
            costs=direct_costs,
            capital=capital,
            risk_fraction=risk_fraction,
            max_lots=max_lots,
            quote_policy=quote_policy,
            quote_alignment=quote_alignment,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        return _empty_report(
            reason="invalid_backtest_inputs", underlyings=requested_underlyings,
            start=start, end=end, interval=interval or "5minute",
            archive_path=archive_path, signal_source=signal_source,
            quote_policy=(quote_policy or quote_alignment or QUOTE_POLICY_EXACT),
        ) | {"error": str(exc)}

    start_ms = _parse_ms(normalized_config.start)
    end_ms = _parse_ms(normalized_config.end, end_of_day=True)
    base = _empty_report(
        underlyings=normalized_config.underlyings,
        start=_iso_ms(start_ms), end=_iso_ms(end_ms), interval=normalized_config.interval,
        archive_path=archive_path, signal_source=signal_source,
        quote_policy=normalized_config.quote_policy,
    )
    base["provenance"].update({
        "exit_policy": normalized_config.exit_policy,
        "initial_stop_fraction": normalized_config.stop_fraction,
        "profit_step_fraction": normalized_config.profit_step_fraction,
        "final_target_fraction": normalized_config.target_fraction,
        "exit_trigger_basis": (
            "executable_bid_touch"
            if normalized_config.exit_policy == EXIT_POLICY_STAGED_BID_LADDER
            else "option_candle_ohlc"
        ),
    })
    base["coverage"]["exit_policy"] = normalized_config.exit_policy
    counts: Counter[str] = Counter()
    rejected: list[dict[str, Any]] = []
    try:
        connection, should_close, actual_path = _source_connection(source)
    except _ArchiveError as exc:
        _reject(counts, rejected, "archive_unavailable", detail=str(exc))
        base["diagnostics"] = {"counts": dict(counts), "rejected_data": rejected}
        return base
    if actual_path:
        base["provenance"]["archive_path"] = actual_path
    try:
        _required_schema(connection)
        signals_normalized, signal_counts, signal_rejected = _normalize_signals(
            signals, signal_callback, signal_source=signal_source,
            start_ms=start_ms, end_ms=end_ms,
        )
        counts.update(signal_counts)
        rejected.extend(signal_rejected)
        contracts, data = _load_contract_data(
            connection, normalized_config, start_ms=start_ms, end_ms=end_ms,
            counts=counts, rejected=rejected,
        )
        base["coverage"].update({
            "contracts": len(contracts),
            "candles": sum(len(item.candles) for item in data.values()),
            "invalid_candles": sum(item.invalid_candles for item in data.values()),
            "quotes": sum(item.quote_rows for item in data.values()),
            "aligned_quotes": sum(item.aligned_quote_rows for item in data.values()),
            "exact_quotes": sum(item.exact_quote_rows for item in data.values()),
            "interval_aligned_quotes": sum(
                item.interval_aligned_quote_rows for item in data.values()
            ),
            "invalid_aligned_quotes": sum(item.invalid_aligned_quotes for item in data.values()),
            "misaligned_quotes": sum(item.misaligned_quote_rows for item in data.values()),
            "quotes_observed_after_candle_completion": sum(
                item.quotes_observed_after_candle_completion for item in data.values()
            ),
            "first_candle": _iso_ms(min((c["timestamp_ms"] for item in data.values() for c in item.candles), default=None)),
            "last_candle": _iso_ms(max((c["timestamp_ms"] for item in data.values() for c in item.candles), default=None)),
        })
        if not signals_normalized:
            return _finalize_report(base, [], counts, rejected)

        # Contract identity is resolved only from the supplied signal.  No
        # nearest strike, expiry substitution, or premium-based selection is
        # allowed.  A signal matching zero or multiple archive contracts is
        # rejected.
        resolved: list[tuple[dict[str, Any], _ContractData]] = []
        for signal in signals_normalized:
            matches = [contract for contract in contracts if _same_identity(contract, signal)]
            if len(matches) != 1:
                reason = "missing_contract_identity" if not matches else "ambiguous_contract_identity"
                _reject(counts, rejected, reason, signal_id=signal["signal_id"],
                        trading_symbol=signal["trading_symbol"])
                continue
            contract = matches[0]
            resolved.append((signal, data[contract["contract_id"]]))

        # Any quote row for a selected contract which cannot be exactly joined
        # to an option candle makes the selected historical sample unsafe.  Do
        # not silently use the aligned subset in that case.
        selected_contract_ids = {item.contract["contract_id"] for _, item in resolved}
        fatal_alignment = any(
            item.contract["contract_id"] in selected_contract_ids and item.misaligned_quote_rows
            for item in data.values()
        )
        if fatal_alignment:
            _reject(counts, rejected, "quote_candle_alignment_failed")
            return _finalize_report(base, [], counts, rejected)

        last_exit_by_contract: dict[int, int] = {}
        trades: list[dict[str, Any]] = []
        for signal, item in resolved:
            contract_id = item.contract["contract_id"]
            if contract_id in last_exit_by_contract and signal["timestamp_ms"] <= last_exit_by_contract[contract_id]:
                _reject(counts, rejected, "overlapping_contract_trade",
                        contract_id=contract_id, signal_id=signal["signal_id"])
                continue
            trade = _trade_for_signal(signal, item, normalized_config, counts, rejected)
            if trade is not None:
                trades.append(trade)
                last_exit_by_contract[contract_id] = _parse_ms(trade["exit_timestamp"]) or 0
        return _finalize_report(base, trades, counts, rejected)
    except (sqlite3.Error, _ArchiveError, KeyError, TypeError, ValueError) as exc:
        _reject(counts, rejected, "archive_read_error", detail=str(exc))
        base["diagnostics"] = {"counts": dict(counts), "rejected_data": rejected}
        return base
    finally:
        if should_close:
            connection.close()


def load_signal_json(path: str | Path) -> Any:
    """Load a signal JSON document without accepting code or network input."""
    with Path(path).expanduser().open("r", encoding="utf-8") as handle:
        return json.load(handle)


def compare_walk_forward(
    candidate_results: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    train_end: Any = None,
    validation_end: Any = None,
    train_fraction: float = 0.60,
    validation_fraction: float = 0.20,
    timestamp_field: str = "timestamp",
    score_field: str = "net_pnl",
) -> dict[str, Any]:
    """Compare fixed candidate results on chronological train/validation/test data.

    ``candidate_results`` contains already-produced observations for each named
    candidate.  This helper never runs a strategy or searches parameters.  The
    candidate is selected using validation score (with train score as a
    deterministic tie-breaker), and the test result is then reported for that
    already-fixed candidate.  Test data is never used for selection.

    If explicit boundaries are omitted, the union of observation timestamps is
    split by ``train_fraction`` and ``validation_fraction``.  Boundaries are
    exclusive: train is before ``train_end``, validation is from train end up
    to validation end, and test is from validation end onward.
    """
    if not isinstance(candidate_results, Mapping) or not candidate_results:
        raise ValueError("candidate_results must be a non-empty mapping")
    if not (0 < float(train_fraction) < 1):
        raise ValueError("train_fraction must be between zero and one")
    if not (0 < float(validation_fraction) < 1):
        raise ValueError("validation_fraction must be between zero and one")
    if float(train_fraction) + float(validation_fraction) >= 1:
        raise ValueError("train and validation fractions must leave test data")

    normalized: dict[str, list[dict[str, Any]]] = {}
    all_timestamps: set[int] = set()
    for candidate, supplied in candidate_results.items():
        name = str(candidate)
        if isinstance(supplied, Mapping):
            for key in ("results", "trade_ledger", "trades", "rows", "data"):
                if key in supplied:
                    supplied = supplied[key]
                    break
            else:
                supplied = [supplied]
        try:
            rows = list(supplied)
        except TypeError as exc:
            raise ValueError(f"candidate {name!r} results must be iterable") from exc
        records: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise ValueError(f"candidate {name!r} row {index} must be a mapping")
            timestamp = _parse_ms(
                _field(row, timestamp_field, "timestamp_ms", "date", "time")
            )
            score = _finite_number(_field(row, score_field, "score", "value"))
            if timestamp is None or score is None:
                raise ValueError(
                    f"candidate {name!r} row {index} needs valid timestamp and {score_field}"
                )
            record = {
                "timestamp_ms": timestamp,
                "score": score,
            }
            records.append(record)
            all_timestamps.add(timestamp)
        records.sort(key=lambda item: item["timestamp_ms"])
        normalized[name] = records

    timestamps = sorted(all_timestamps)
    if len(timestamps) < 3 and train_end is None and validation_end is None:
        raise ValueError("at least three chronological timestamps are required")

    if train_end is None:
        train_index = max(1, min(len(timestamps) - 2, int(len(timestamps) * train_fraction)))
        train_boundary = timestamps[train_index]
    else:
        train_boundary = _parse_ms(train_end)
        if train_boundary is None:
            raise ValueError("invalid train_end timestamp")
    if validation_end is None:
        validation_index = max(
            1,
            min(
                len(timestamps) - 1,
                int(len(timestamps) * (float(train_fraction) + float(validation_fraction))),
            ),
        )
        validation_boundary = timestamps[validation_index]
    else:
        validation_boundary = _parse_ms(validation_end)
        if validation_boundary is None:
            raise ValueError("invalid validation_end timestamp")
    if train_boundary >= validation_boundary:
        raise ValueError("train_end must be before validation_end")

    minimum_timestamp = timestamps[0]
    maximum_timestamp = timestamps[-1]
    if not (minimum_timestamp < train_boundary < maximum_timestamp):
        raise ValueError("train_end must leave observations before and after the boundary")
    if not (train_boundary < validation_boundary <= maximum_timestamp):
        raise ValueError("validation_end must be after train_end and within the data")

    def partition(timestamp_ms: int) -> str:
        if timestamp_ms < train_boundary:
            return "train"
        if timestamp_ms < validation_boundary:
            return "validation"
        return "test"

    partition_bounds = {
        "train": (minimum_timestamp, train_boundary),
        "validation": (train_boundary, validation_boundary),
        "test": (validation_boundary, maximum_timestamp),
    }
    metrics: dict[str, dict[str, dict[str, Any]]] = {}
    for candidate, records in normalized.items():
        metrics[candidate] = {}
        for partition_name in ("train", "validation", "test"):
            selected = [
                record for record in records if partition(record["timestamp_ms"]) == partition_name
            ]
            total = sum(float(record["score"]) for record in selected)
            metrics[candidate][partition_name] = {
                "observations": len(selected),
                "score": total,
                "mean_score": total / len(selected) if selected else None,
                "winning_observations": sum(1 for record in selected if record["score"] > 0),
                "losing_observations": sum(1 for record in selected if record["score"] < 0),
            }

    candidates = sorted(metrics)
    selected_candidate = max(
        candidates,
        key=lambda candidate: (
            metrics[candidate]["validation"]["score"],
            metrics[candidate]["train"]["score"],
            candidate,
        ),
    )

    def bounds_json(bounds: tuple[int, int]) -> dict[str, Optional[str]]:
        return {"start": _iso_ms(bounds[0]), "end": _iso_ms(bounds[1])}

    return {
        "partitions": {
            name: {
                **bounds_json(bounds),
                "boundary_end_exclusive": _iso_ms(bounds[1]),
            }
            for name, bounds in partition_bounds.items()
        },
        "candidate_metrics": metrics,
        "selection": {
            "selected_candidate": selected_candidate,
            "selection_partition": "validation",
            "tie_break_partition": "train",
            "test_used_for_selection": False,
        },
        "test": {
            "candidate": selected_candidate,
            **metrics[selected_candidate]["test"],
        },
    }


def report_json(report: Mapping[str, Any]) -> str:
    """Serialize a report for the CLI or callers that need JSON text."""
    return json.dumps(report, indent=2, sort_keys=True, allow_nan=False)


# Short aliases make the public entry point discoverable without changing the
# single implementation above.
run_backtest = run_options_premium_backtest
backtest_options_premium = run_options_premium_backtest
walk_forward_compare = compare_walk_forward
walk_forward_comparison = compare_walk_forward


__all__ = [
    "BacktestConfig",
    "NOT_READY_FOR_OPTIONS_PNL",
    "OPTIONS_PNL_READY",
    "QUOTE_POLICY_EXACT",
    "QUOTE_POLICY_INTERVAL_ALIGNED",
    "TransactionCosts",
    "backtest_options_premium",
    "compare_walk_forward",
    "load_signal_json",
    "report_json",
    "run_backtest",
    "run_options_premium_backtest",
    "walk_forward_comparison",
    "walk_forward_compare",
]
