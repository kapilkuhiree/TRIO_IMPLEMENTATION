"""Read-only Zerodha signal capture for option-premium research.

The artifact produced by this module is a JSON document whose ``signals``
array can be supplied directly to ``options_premium_backtest``.  It is a
capture boundary only:

* underlying candles come from :class:`ZerodhaMarketData` at 5 minutes;
* the option identity comes from the current NFO instrument snapshot;
* the quote is a genuine current NFO bid/ask snapshot, never a calculated
  premium; and
* strategy evaluation is either the existing pure CandidatePipeline boundary
  or a caller-supplied pure evaluator.

Only current-day completed signals are eligible for a current quote.  Older
signals are reported as unavailable rather than being paired with a current
quote.  This deliberately gives a ``NOT_READY`` report when the provider has
no usable current-day data.

No broker, order, risk, or backtest runtime is imported or called here.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
import json
import math
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from src.zerodha_market_data import ZerodhaMarketData


IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PROVIDER = "zerodha"
READY = "READY"
NOT_READY = "NOT_READY"
DEFAULT_UNDERLYINGS = ("NIFTY",)
DEFAULT_STRATEGIES = ("orb", "pullback", "momentum")
RESEARCH_STRATEGY = "trend_pullback_breakout_research"
SUPPORTED_STRATEGIES = frozenset((*DEFAULT_STRATEGIES, RESEARCH_STRATEGY))
INDEX_UNDERLYINGS = frozenset({
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
})
FIVE_MINUTES = timedelta(minutes=5)


PureEvaluator = Callable[[list[dict[str, Any]], str, str], Iterable[Any]]


def _field(row: Any, *names: str) -> Any:
    if isinstance(row, Mapping):
        for name in names:
            if name in row:
                return row[name]
            wanted = name.lower()
            for actual, value in row.items():
                if str(actual).lower() == wanted:
                    return value
    else:
        for name in names:
            if hasattr(row, name):
                return getattr(row, name)
    return None


def _as_ist(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if not math.isfinite(number):
            return None
        seconds = number / 1000.0 if abs(number) >= 100_000_000_000 else number
        parsed = datetime.fromtimestamp(seconds, UTC)
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
                "%Y-%m-%d",
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


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.astimezone(IST).isoformat() if value is not None else None


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _positive_int(value: Any) -> Optional[int]:
    number = _finite(value)
    if number is None or number <= 0 or not number.is_integer():
        return None
    return int(number)


def _expiry(value: Any) -> Optional[str]:
    parsed = _as_ist(value)
    return parsed.date().isoformat() if parsed is not None else None


def _normalise_underlying(value: Any) -> str:
    name = str(value or "").strip().upper()
    aliases = {
        "NIFTY 50": "NIFTY",
        "NIFTY50": "NIFTY",
        "^NSEI": "NIFTY",
        "NIFTY BANK": "BANKNIFTY",
        "^NSEBANK": "BANKNIFTY",
        "NIFTY FIN SERVICE": "FINNIFTY",
        "FINNIFTY": "FINNIFTY",
        "NIFTY MID SELECT": "MIDCPNIFTY",
    }
    return aliases.get(name, name)


def _normalise_many(values: Iterable[str], default: Sequence[str]) -> tuple[str, ...]:
    supplied = list(values or ())
    if not supplied:
        supplied = list(default)
    output: list[str] = []
    for value in supplied:
        for item in str(value or "").split(","):
            normalised = _normalise_underlying(item)
            if normalised and normalised not in output:
                output.append(normalised)
    if not output:
        raise ValueError("at least one underlying is required")
    return tuple(output)


def _normalise_strategies(values: Iterable[str]) -> tuple[str, ...]:
    output: list[str] = []
    for value in values:
        for item in str(value or "").split(","):
            strategy = item.strip().lower()
            if not strategy:
                continue
            if strategy not in SUPPORTED_STRATEGIES:
                raise ValueError(
                    f"unsupported strategy {strategy!r}; choose from "
                    f"{', '.join(sorted(SUPPORTED_STRATEGIES))}"
                )
            if strategy not in output:
                output.append(strategy)
    return tuple(output or DEFAULT_STRATEGIES)


def _reject(
    counts: Counter[str], rejected: list[dict[str, Any]], reason: str, **details: Any
) -> None:
    counts[reason] += 1
    item: dict[str, Any] = {"reason": reason}
    item.update({key: value for key, value in details.items() if value is not None})
    rejected.append(item)


def _safe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:240]}"


def _default_pure_evaluator(
    candles: list[dict[str, Any]], strategy_name: str, underlying: str
) -> list[Any]:
    """Evaluate every supplied completed bar through the pure strategy boundary.

    The CandidatePipeline class is not constructed.  Its classmethod delegates
    to the strategy's value-only ``evaluate_signal`` method and does not touch
    the pipeline's risk singleton, trade lifecycle, or P&L state.
    """
    if strategy_name == RESEARCH_STRATEGY:
        from src.research_cepe_strategy import generate_candidates
        if not candles:
            return []
        first = candles[0]
        return generate_candidates(
            candles,
            expected_underlying=underlying,
            expected_exchange="NSE",
            expected_instrument_type=first.get("instrument_type"),
        )

    from candidate_pipeline import CandidatePipeline

    candidates: list[Any] = []
    for index in range(len(candles)):
        candidates.extend(
            CandidatePipeline.evaluate_strategy_signals_pure(
                candles,
                strategy_name,
                underlying_symbol=underlying,
                contract_specs={"lot_size": 1, "expiry_preference": "weekly"},
                index=index,
            )
        )
    return candidates


def _normalise_candles(
    rows: Iterable[Any],
    underlying: str,
    observed_at: datetime,
    counts: Counter[str],
    rejected: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert Kite candles to the strategy input without filling missing data."""
    normalised: list[dict[str, Any]] = []
    for index, raw in enumerate(rows or ()):
        if isinstance(raw, (list, tuple)):
            if len(raw) < 5:
                _reject(counts, rejected, "invalid_underlying_candle", index=index)
                continue
            timestamp_value, open_value, high_value, low_value, close_value = raw[:5]
            volume_value = raw[5] if len(raw) > 5 else 0
            supplied_exchange = None
            supplied_symbol = None
            supplied_type = None
        elif isinstance(raw, Mapping):
            timestamp_value = _field(raw, "timestamp", "date", "datetime")
            open_value = _field(raw, "open", "Open")
            high_value = _field(raw, "high", "High")
            low_value = _field(raw, "low", "Low")
            close_value = _field(raw, "close", "Close")
            volume_value = _field(raw, "volume", "Volume") or 0
            supplied_exchange = _field(raw, "exchange")
            supplied_symbol = _field(raw, "underlying_symbol", "underlying")
            supplied_type = _field(raw, "instrument_type")
        else:
            _reject(counts, rejected, "invalid_underlying_candle", index=index)
            continue

        timestamp = _as_ist(timestamp_value)
        if timestamp is None:
            _reject(counts, rejected, "invalid_underlying_timestamp", index=index)
            continue
        if timestamp + FIVE_MINUTES > observed_at:
            _reject(
                counts,
                rejected,
                "incomplete_underlying_candle",
                index=index,
                timestamp=_iso(timestamp),
            )
            continue
        if supplied_exchange not in (None, "", "NSE"):
            _reject(
                counts,
                rejected,
                "non_nse_underlying_candle",
                index=index,
                timestamp=_iso(timestamp),
            )
            continue
        if supplied_symbol not in (None, "", underlying):
            _reject(
                counts,
                rejected,
                "underlying_candle_mismatch",
                index=index,
                timestamp=_iso(timestamp),
            )
            continue
        if supplied_type not in (None, "", "index", "stock"):
            _reject(
                counts,
                rejected,
                "non_equity_underlying_candle",
                index=index,
                timestamp=_iso(timestamp),
            )
            continue

        values = [_finite(item) for item in (
            open_value, high_value, low_value, close_value
        )]
        if any(value is None or value <= 0 for value in values):
            _reject(counts, rejected, "invalid_underlying_ohlc", index=index)
            continue
        open_price, high, low, close = values  # type: ignore[misc]
        if high < max(open_price, low, close) or low > min(open_price, high, close):
            _reject(counts, rejected, "inconsistent_underlying_ohlc", index=index)
            continue
        volume = _finite(volume_value)
        if volume is None or volume < 0:
            volume = 0.0

        normalised.append({
            "timestamp": _iso(timestamp),
            "provider_timestamp_raw": timestamp_value,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "exchange": "NSE",
            "underlying_symbol": underlying,
            "instrument_type": (
                str(supplied_type).lower()
                if supplied_type not in (None, "")
                else ("index" if underlying in INDEX_UNDERLYINGS else "stock")
            ),
        })

    normalised.sort(key=lambda row: _as_ist(row["timestamp"]) or datetime.min.replace(tzinfo=IST))
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in normalised:
        timestamp = str(row["timestamp"])
        if timestamp in seen:
            _reject(counts, rejected, "duplicate_underlying_candle", timestamp=timestamp)
            continue
        seen.add(timestamp)
        unique.append(row)
    counts["underlying_candles_completed"] += len(unique)
    return unique


def _normalise_contracts(
    rows: Iterable[Mapping[str, Any]],
    underlyings: Sequence[str],
    as_of: date,
    counts: Counter[str],
    rejected: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    contracts: list[dict[str, Any]] = []
    wanted = set(underlyings)
    for index, raw in enumerate(rows or ()):
        counts["nfo_rows_seen"] += 1
        exchange = str(_field(raw, "exchange") or "").strip().upper()
        option_type = str(
            _field(raw, "instrument_type", "option_type") or ""
        ).strip().upper()
        underlying = _normalise_underlying(
            _field(raw, "name", "underlying", "underlying_symbol")
        )
        expiry_text = _expiry(_field(raw, "expiry", "expiry_date"))
        expiry = date.fromisoformat(expiry_text) if expiry_text else None
        symbol = str(_field(raw, "tradingsymbol", "trading_symbol") or "").strip()
        token = _field(raw, "instrument_token", "instrumentToken")
        strike = _finite(_field(raw, "strike"))
        lot_size = _positive_int(_field(raw, "lot_size", "lotSize"))

        if exchange != "NFO":
            counts["rejected_non_nfo_rows"] += 1
            continue
        if underlying not in wanted:
            continue
        if option_type not in {"CE", "PE"}:
            _reject(counts, rejected, "rejected_non_option_contract", index=index)
            continue
        if expiry is None or expiry < as_of:
            _reject(
                counts,
                rejected,
                "rejected_expired_or_invalid_contract",
                index=index,
                trading_symbol=symbol or None,
            )
            continue
        if token in (None, "") or not symbol or strike is None or strike <= 0 or lot_size is None:
            _reject(
                counts,
                rejected,
                "incomplete_current_nfo_identity",
                index=index,
                trading_symbol=symbol or None,
            )
            continue

        contracts.append({
            "raw": dict(raw),
            "exchange": "NFO",
            "instrument_token": token,
            "trading_symbol": symbol,
            "underlying": underlying,
            "expiry": expiry,
            "strike": strike,
            "option_type": option_type,
            "lot_size": lot_size,
        })
    counts["active_exact_nfo_rows"] += len(contracts)
    return contracts


def _candidate_mapping(candidate: Any) -> dict[str, Any]:
    if isinstance(candidate, Mapping):
        return dict(candidate)
    return dict(getattr(candidate, "__dict__", {}) or {})


def _quote_item(payload: Any, symbol: str) -> Optional[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return None
    if "bid" in payload or "ask" in payload or "depth" in payload:
        return dict(payload)
    for key in (f"NFO:{symbol}", symbol):
        item = payload.get(key)
        if isinstance(item, Mapping):
            return dict(item)
    return None


def _quote_values(payload: Mapping[str, Any]) -> tuple[Optional[float], Optional[float]]:
    depth = payload.get("depth") or {}
    buys = depth.get("buy", []) if isinstance(depth, Mapping) else []
    sells = depth.get("sell", []) if isinstance(depth, Mapping) else []
    first_buy = buys[0] if buys and isinstance(buys[0], Mapping) else {}
    first_sell = sells[0] if sells and isinstance(sells[0], Mapping) else {}
    bid = _finite(payload.get("bid", first_buy.get("price")))
    ask = _finite(payload.get("ask", first_sell.get("price")))
    return bid, ask


def _select_contract(
    candidate: Mapping[str, Any],
    underlying: str,
    option_type: str,
    underlying_price: Optional[float],
    contracts: Sequence[Mapping[str, Any]],
    counts: Counter[str],
    rejected: list[dict[str, Any]],
) -> Optional[Mapping[str, Any]]:
    nested = _field(candidate, "selected_contract", "contract", "option")
    nested = nested if isinstance(nested, Mapping) else {}
    source = dict(nested)
    source.update(candidate)
    explicit_keys = ("trading_symbol", "tradingsymbol", "strike", "lot_size", "lotSize")
    has_explicit_identity = any(_field(source, key) not in (None, "") for key in explicit_keys)
    candidates = [
        row for row in contracts
        if row["underlying"] == underlying and row["option_type"] == option_type
    ]

    if has_explicit_identity:
        symbol = str(_field(source, "trading_symbol", "tradingsymbol") or "").strip().upper()
        expiry_text = _expiry(_field(source, "expiry", "expiry_date"))
        expiry = date.fromisoformat(expiry_text) if expiry_text else None
        strike = _finite(_field(source, "strike"))
        lot_size = _positive_int(_field(source, "lot_size", "lotSize"))
        if not symbol or expiry is None or strike is None or lot_size is None:
            _reject(counts, rejected, "incomplete_requested_contract_identity", underlying=underlying)
            return None
        exact = [
            row for row in candidates
            if str(row["trading_symbol"]).upper() == symbol
            and row["expiry"] == expiry
            and math.isclose(float(row["strike"]), strike, rel_tol=0, abs_tol=1e-9)
            and row["lot_size"] == lot_size
        ]
        if len(exact) != 1:
            _reject(
                counts,
                rejected,
                "exact_current_nfo_identity_missing_or_ambiguous",
                underlying=underlying,
                trading_symbol=symbol,
            )
            return None
        return exact[0]

    if not candidates:
        _reject(
            counts,
            rejected,
            "missing_current_nfo_contract",
            underlying=underlying,
            option_type=option_type,
        )
        return None
    if underlying_price is None or underlying_price <= 0:
        _reject(counts, rejected, "missing_underlying_price", underlying=underlying)
        return None

    # Selection chooses an existing exact row only.  It never constructs a
    # trading symbol, strike, expiry, lot, or synthetic premium.
    candidates = sorted(
        candidates,
        key=lambda row: (
            abs(float(row["strike"]) - underlying_price),
            row["expiry"],
            float(row["strike"]),
            str(row["trading_symbol"]),
        ),
    )
    best_distance = abs(float(candidates[0]["strike"]) - underlying_price)
    tied = [
        row for row in candidates
        if math.isclose(abs(float(row["strike"]) - underlying_price), best_distance, abs_tol=1e-9)
    ]
    if len(tied) > 1 and tied[0]["expiry"] == tied[1]["expiry"] and tied[0]["strike"] == tied[1]["strike"]:
        _reject(counts, rejected, "ambiguous_current_nfo_contract", underlying=underlying)
        return None
    return candidates[0]


class ZerodhaSignalCapture:
    """Capture current-day, read-only option research signals."""

    provider_name = PROVIDER
    read_only = True

    def __init__(
        self,
        adapter: ZerodhaMarketData,
        *,
        underlyings: Iterable[str] = DEFAULT_UNDERLYINGS,
        strategy_names: Iterable[str] = DEFAULT_STRATEGIES,
        period: str = "30d",
        evaluator: Optional[PureEvaluator] = None,
        now: Callable[[], datetime] = lambda: datetime.now(IST),
        output_path: Optional[str | Path] = None,
        max_provider_clock_skew_seconds: float = 120.0,
        require_demand_supply: bool = False,
    ) -> None:
        self.adapter = adapter
        self.underlyings = _normalise_many(underlyings, DEFAULT_UNDERLYINGS)
        self.strategy_names = _normalise_strategies(strategy_names)
        self.period = str(period or "30d")
        self.evaluator = evaluator or _default_pure_evaluator
        self.now = now
        self.output_path = Path(output_path).expanduser() if output_path else None
        if not math.isfinite(float(max_provider_clock_skew_seconds)) or max_provider_clock_skew_seconds < 0:
            raise ValueError("max_provider_clock_skew_seconds must be finite and non-negative")
        self.max_provider_clock_skew_seconds = float(max_provider_clock_skew_seconds)
        self.require_demand_supply = bool(require_demand_supply)

    def _fetch_quote(self, contract: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        symbol = str(contract["trading_symbol"])
        quote_method = getattr(self.adapter, "quote", None)
        if callable(quote_method):
            return _quote_item(quote_method("NFO", [symbol]), symbol)
        option_quote = getattr(self.adapter, "option_quote", None)
        if callable(option_quote):
            return _quote_item(option_quote(dict(contract["raw"])), symbol)
        raise RuntimeError("adapter does not expose a read-only quote method")

    def capture(self, *, as_of: Optional[datetime] = None) -> dict[str, Any]:
        observed_at = _as_ist(as_of or self.now())
        if observed_at is None:
            raise ValueError("as_of/now must be a valid datetime")
        counts: Counter[str] = Counter()
        rejected: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        signals: list[dict[str, Any]] = []
        candle_dates: dict[str, list[str]] = {}
        current_day = observed_at.date()

        try:
            nfo_rows = self.adapter.instruments("NFO") or []
            contracts = _normalise_contracts(
                nfo_rows,
                self.underlyings,
                current_day,
                counts,
                rejected,
            )
        except Exception as exc:
            contracts = []
            counts["nfo_snapshot_error"] += 1
            errors.append({"scope": "nfo_snapshot", "error": _safe_error(exc)})

        quote_cache: dict[str, tuple[Optional[dict[str, Any]], Optional[str]]] = {}
        for underlying in self.underlyings:
            try:
                raw_candles = self.adapter.historical_symbol(
                    underlying, "5m", self.period
                ) or []
                counts["underlying_candles_seen"] += len(raw_candles)
                candles = _normalise_candles(
                    raw_candles, underlying, observed_at, counts, rejected
                )
            except Exception as exc:
                candles = []
                counts["underlying_history_errors"] += 1
                errors.append({
                    "scope": "underlying_history",
                    "underlying": underlying,
                    "error": _safe_error(exc),
                })
                continue

            current_candles = [
                candle for candle in candles
                if (_as_ist(candle["timestamp"]) or observed_at).date() == current_day
            ]
            candle_dates[underlying] = sorted({
                (_as_ist(candle["timestamp"]) or observed_at).date().isoformat()
                for candle in candles
            })
            counts[f"current_day_candles_{underlying}"] += len(current_candles)
            if not current_candles:
                counts["missing_current_day_underlying_data"] += 1

            for strategy_name in self.strategy_names:
                counts["strategies_evaluated"] += 1
                try:
                    evaluated = self.evaluator(candles, strategy_name, underlying)
                    candidates = list(evaluated or ())
                except Exception as exc:
                    counts["strategy_evaluation_errors"] += 1
                    errors.append({
                        "scope": "strategy_evaluation",
                        "strategy": strategy_name,
                        "underlying": underlying,
                        "error": _safe_error(exc),
                    })
                    continue

                counts["strategy_candidates_seen"] += len(candidates)
                for candidate_value in candidates:
                    candidate = _candidate_mapping(candidate_value)
                    signal_timestamp_raw = _field(
                        candidate,
                        "signal_timestamp",
                        "timestamp",
                        "trigger_time",
                        "completed_at",
                    )
                    signal_timestamp = _as_ist(signal_timestamp_raw)
                    if signal_timestamp is None:
                        _reject(counts, rejected, "invalid_signal_timestamp", underlying=underlying)
                        continue
                    if _field(candidate, "completed", "bar_closed", "is_completed") is False:
                        _reject(counts, rejected, "incomplete_signal", timestamp=_iso(signal_timestamp))
                        continue
                    if signal_timestamp.date() != current_day:
                        _reject(
                            counts,
                            rejected,
                            "historical_signal_not_capturable_with_current_quote",
                            underlying=underlying,
                            timestamp=_iso(signal_timestamp),
                        )
                        continue
                    if signal_timestamp + FIVE_MINUTES > observed_at:
                        _reject(
                            counts,
                            rejected,
                            "signal_bar_not_completed_at_observation",
                            underlying=underlying,
                            timestamp=_iso(signal_timestamp),
                        )
                        continue

                    candidate_underlying = _normalise_underlying(
                        _field(candidate, "underlying", "underlying_symbol")
                    )
                    if candidate_underlying and candidate_underlying != underlying:
                        _reject(
                            counts,
                            rejected,
                            "evaluator_underlying_mismatch",
                            underlying=underlying,
                            candidate_underlying=candidate_underlying,
                        )
                        continue

                    action = str(
                        _field(candidate, "action", "side", "order_side", "signal") or "BUY"
                    ).strip().upper()
                    direction = str(_field(candidate, "direction", "variant") or "").strip().lower()
                    action_tokens = set(re.findall(r"[A-Z]+", action.replace("/", " ").replace("_", " ")))
                    if (
                        "SELL" in action_tokens
                        or "SHORT" in action_tokens
                        or direction in {"short", "sell", "short_option"}
                    ):
                        _reject(counts, rejected, "rejected_non_buy_or_short_signal", underlying=underlying)
                        continue
                    if action and action not in {"BUY", "LONG", "BULLISH", "BEARISH", "UP", "DOWN"} and "BUY" not in action_tokens:
                        _reject(counts, rejected, "rejected_non_buy_signal", underlying=underlying)
                        continue

                    nested = _field(candidate, "selected_contract", "contract", "option")
                    nested = nested if isinstance(nested, Mapping) else {}
                    option_type = str(
                        _field(candidate, "option_type", "type", "instrument_type")
                        or _field(nested, "option_type", "instrument_type")
                        or ("CE" if direction in {"bullish", "up", "long"} else "PE" if direction == "bearish" else "")
                    ).strip().upper()
                    if option_type not in {"CE", "PE"}:
                        _reject(counts, rejected, "invalid_option_type", underlying=underlying)
                        continue

                    demand_supply = None
                    if self.require_demand_supply:
                        from src.demand_supply import confirm_demand_supply
                        direction_for_confirmation = str(
                            _field(candidate, "direction", "signal_direction") or
                            ("bullish" if option_type == "CE" else "bearish")
                        ).strip().lower()
                        signal_index = next(
                            (idx for idx, candle in enumerate(candles)
                             if candle["timestamp"] == _iso(signal_timestamp)),
                            None,
                        )
                        if signal_index is None:
                            _reject(counts, rejected, "demand_supply_signal_candle_missing",
                                    underlying=underlying, timestamp=_iso(signal_timestamp))
                            continue
                        demand_supply = confirm_demand_supply(
                            candles, direction_for_confirmation, index=signal_index,
                            cfg={"demand_supply": {"enabled": True}}, context="options")
                        if not demand_supply.confirmed:
                            _reject(counts, rejected, "demand_supply_not_confirmed",
                                    underlying=underlying, timestamp=_iso(signal_timestamp),
                                    reason_detail=demand_supply.reason)
                            continue

                    underlying_price = _finite(
                        _field(candidate, "underlying_price_at_signal", "underlying_price", "entry_price")
                    )
                    if underlying_price is None:
                        matching_candles = [
                            candle for candle in candles
                            if candle["timestamp"] == _iso(signal_timestamp)
                        ]
                        if matching_candles:
                            underlying_price = _finite(matching_candles[-1]["close"])

                    contract = _select_contract(
                        candidate,
                        underlying,
                        option_type,
                        underlying_price,
                        contracts,
                        counts,
                        rejected,
                    )
                    if contract is None:
                        continue

                    symbol = str(contract["trading_symbol"])
                    if symbol not in quote_cache:
                        counts["quote_symbols_requested"] += 1
                        try:
                            quote_cache[symbol] = (self._fetch_quote(contract), None)
                        except Exception as exc:
                            quote_cache[symbol] = (None, _safe_error(exc))
                    payload, quote_error = quote_cache[symbol]
                    if quote_error:
                        counts["quote_errors"] += 1
                        _reject(
                            counts,
                            rejected,
                            "quote_fetch_error",
                            trading_symbol=symbol,
                            detail=quote_error,
                        )
                        continue
                    if payload is None:
                        _reject(
                            counts,
                            rejected,
                            "missing_current_quote",
                            trading_symbol=symbol,
                        )
                        continue

                    bid, ask = _quote_values(payload)
                    if payload.get("executable") is not None and not bool(payload.get("executable")):
                        _reject(counts, rejected, "non_executable_quote", trading_symbol=symbol)
                        continue
                    if bid is None or ask is None:
                        _reject(counts, rejected, "missing_executable_bid_ask", trading_symbol=symbol)
                        continue
                    if bid <= 0 or ask <= 0 or bid >= ask:
                        _reject(counts, rejected, "non_executable_bid_ask", trading_symbol=symbol)
                        continue

                    provider_timestamp_raw = _field(
                        payload, "timestamp", "provider_timestamp", "source_timestamp"
                    )
                    provider_timestamp = _as_ist(provider_timestamp_raw)
                    if provider_timestamp is not None:
                        if provider_timestamp.date() != current_day:
                            _reject(counts, rejected, "stale_provider_quote", trading_symbol=symbol)
                            continue
                        if provider_timestamp > observed_at + timedelta(
                                seconds=self.max_provider_clock_skew_seconds):
                            _reject(counts, rejected, "future_provider_quote", trading_symbol=symbol)
                            continue
                        if provider_timestamp < signal_timestamp:
                            _reject(counts, rejected, "quote_before_signal", trading_symbol=symbol)
                            continue
                    else:
                        counts["quote_provider_timestamp_missing"] += 1

                    observed_iso = _iso(observed_at)
                    signal_iso = str(signal_timestamp_raw)
                    signal_id = str(
                        _field(candidate, "signal_id", "candidate_id", "id")
                        or f"{strategy_name}-{underlying}-{signal_iso}-{option_type}"
                    )
                    signals.append({
                        "signal_id": signal_id,
                        "timestamp": signal_iso,
                        "signal_timestamp": signal_iso,
                        "completed": True,
                        "underlying": underlying,
                        "underlying_symbol": underlying,
                        "action": "BUY",
                        "option_type": option_type,
                        "trading_symbol": symbol,
                        "expiry": contract["expiry"].isoformat(),
                        "strike": contract["strike"],
                        "lot_size": contract["lot_size"],
                        "strategy": strategy_name,
                        "strategy_name": str(
                            _field(candidate, "strategy_name", "strategy") or strategy_name
                        ),
                        "demand_supply": demand_supply.to_dict() if demand_supply else None,
                        "quote_snapshot": {
                            "bid": bid,
                            "ask": ask,
                            "provider_timestamp": provider_timestamp_raw,
                            "provider_timestamp_normalized": _iso(provider_timestamp),
                            "observed_at": observed_iso,
                            "executable": True,
                        },
                        "provider": PROVIDER,
                        "observed_at": observed_iso,
                        "provenance": {
                            "provider": PROVIDER,
                            "read_only": True,
                            "underlying_candle": {
                                "source": "ZerodhaMarketData.historical_symbol",
                                "timestamp": signal_iso,
                                "observed_at": observed_iso,
                            },
                            "contract": {
                                "source": "ZerodhaMarketData.instruments('NFO')",
                                "observed_at": observed_iso,
                                "instrument_token": contract["instrument_token"],
                                "exchange": "NFO",
                                "trading_symbol": symbol,
                            },
                            "quote": {
                                "source": "ZerodhaMarketData.quote",
                                "provider_timestamp": provider_timestamp_raw,
                                "provider_timestamp_normalized": _iso(provider_timestamp),
                                "observed_at": observed_iso,
                                "bid_ask_executable": True,
                            },
                        },
                    })
                    counts["signals_captured"] += 1

        signals.sort(key=lambda row: (
            str(row["timestamp"]), str(row["underlying"]),
            str(row["strategy"]), str(row["trading_symbol"]),
        ))
        if signals:
            status = READY
        else:
            status = NOT_READY
            counts["no_capturable_signals"] += 1

        report: dict[str, Any] = {
            "schema": "zerodha_signal_capture.v1",
            "status": status,
            "ready_for_options_premium_backtest": bool(signals),
            "provider": PROVIDER,
            "read_only": True,
            "orders_placed": 0,
            "observed_at": _iso(observed_at),
            "signals": signals,
            "coverage": {
                "mode": "current_trading_day_only",
                "supported_history": "current day completed underlying candles; older signals are rejected when current quotes would be stale",
                "observed_date": current_day.isoformat(),
                "requested_underlyings": list(self.underlyings),
                "strategies": list(self.strategy_names),
                "period_requested": self.period,
                "interval_requested": "5m",
                "max_provider_clock_skew_seconds": self.max_provider_clock_skew_seconds,
                "underlying_candle_dates": candle_dates,
                "active_current_nfo_rows": len(contracts),
                "quotes_require": "exact current NFO identity plus executable bid and ask",
                "premium_synthesized": False,
                "current_quote_attached_to_historical_signal": False,
            },
            "diagnostics": {
                "counts": dict(sorted(counts.items())),
                "rejected_data": rejected,
                "errors": errors,
            },
        }
        if self.output_path is not None:
            write_signal_file(report, self.output_path)
            report["output_path"] = str(self.output_path)
        return report


def write_signal_file(report: Mapping[str, Any], output_path: str | Path) -> Path:
    """Write a JSON research artifact; this is the only local write performed."""
    path = Path(output_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
    return path


def capture_zerodha_signals(
    adapter: ZerodhaMarketData,
    *,
    underlyings: Iterable[str] = DEFAULT_UNDERLYINGS,
    strategy_names: Iterable[str] = DEFAULT_STRATEGIES,
    strategies: Optional[Iterable[str]] = None,
    period: str = "30d",
    evaluator: Optional[PureEvaluator] = None,
    now: Callable[[], datetime] = lambda: datetime.now(IST),
    as_of: Optional[datetime] = None,
    output_path: Optional[str | Path] = None,
    max_provider_clock_skew_seconds: float = 120.0,
    require_demand_supply: bool = False,
) -> dict[str, Any]:
    """Capture one read-only JSON-ready signal report using an injected adapter."""
    selected_strategies = strategy_names if strategies is None else strategies
    return ZerodhaSignalCapture(
        adapter,
        underlyings=underlyings,
        strategy_names=selected_strategies,
        period=period,
        evaluator=evaluator,
        now=now,
        output_path=output_path,
        max_provider_clock_skew_seconds=max_provider_clock_skew_seconds,
        require_demand_supply=require_demand_supply,
    ).capture(as_of=as_of)


# A short alias is convenient for callers embedding the artifact producer.
capture_signals = capture_zerodha_signals


__all__ = [
    "DEFAULT_STRATEGIES",
    "DEFAULT_UNDERLYINGS",
    "NOT_READY",
    "READY",
    "ZerodhaSignalCapture",
    "capture_signals",
    "capture_zerodha_signals",
    "write_signal_file",
]
