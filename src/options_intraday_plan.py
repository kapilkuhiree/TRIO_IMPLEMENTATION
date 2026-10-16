"""Production options-intraday plan generation.

This module is deliberately a plan builder, not a backtester.  It consumes a
completed NSE 5-minute NIFTY bar, calls the strategy classes' signal boundary,
and selects a single executable long option from an exchange quote snapshot.
There is no synthetic option price, backtest result, P&L simulation, or risk
singleton reset on this path.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import math
import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

from candidate_pipeline import CandidatePipeline, OptionsCandidate
from contract_selector import select_option_contract
from src.demand_supply import confirm_demand_supply, demand_supply_enabled


IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
BAR_SIZE = timedelta(minutes=5)
DEFAULT_STRATEGIES = ("orb", "pullback", "momentum")
ALLOWED_STRATEGIES = set(DEFAULT_STRATEGIES)
NSE_OPEN = time(9, 15)
NSE_LAST_BAR_START = time(15, 10)


def fetch_market_data(symbol: str, timeframe: str):
    """Late-bound provider hook, convenient for offline boundary tests."""
    from src.data_fetcher import fetch_market_data as provider_fetch
    return provider_fetch(symbol, timeframe)


def fetch_chain(symbol: str = "NIFTY", *, use_cache: bool = False):
    """Late-bound NSE chain hook; production calls it with cache disabled."""
    from src.options_chain import fetch_chain as nse_fetch
    return nse_fetch(symbol, use_cache=use_cache)


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _parse_timestamp(value: Any, *, allow_naive: bool = False) -> Optional[datetime]:
    """Parse a timestamp and return it in IST.

    Candle timestamps must be timezone-aware.  Exchange quote timestamps are
    allowed to be naïve only when they came from an NSE record, where the
    exchange convention is IST; local retrieval timestamps are never passed to
    this function by the chain normalizer.
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            text = str(value).strip().replace("Z", "+00:00")
            parsed = datetime.fromisoformat(text)
        except (TypeError, ValueError, OverflowError):
            parsed = None
            text = str(value).strip()
            for fmt in (
                "%d-%b-%Y %H:%M:%S",
                "%d-%b-%Y %H:%M",
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
        if not allow_naive:
            return None
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def _iso_ist(value: datetime) -> str:
    return value.astimezone(IST).isoformat()


def _as_now(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(IST)
    if isinstance(now, date) and not isinstance(now, datetime):
        return datetime.combine(now, time(15, 15), tzinfo=IST)
    parsed = _parse_timestamp(now, allow_naive=True)
    return parsed or datetime.now(IST)


def _cfg_float(cfg: Dict[str, Any], keys: Iterable[str], default: float) -> float:
    for key in keys:
        value = _finite(cfg.get(key))
        if value is not None:
            return value
    return default


def _strategies(cfg: Dict[str, Any]) -> List[str]:
    mode_cfg = cfg.get("options_intraday", {}) or {}
    env_raw = os.environ.get("TRIO_RESEARCH_STRATEGIES", "").strip()
    if env_raw:
        raw = env_raw
    else:
        raw = mode_cfg.get("strategies", list(DEFAULT_STRATEGIES))
        if raw is None:
            raw = list(DEFAULT_STRATEGIES)
    if isinstance(raw, str):
        values = [item.strip().lower() for item in raw.split(",") if item.strip()]
    else:
        values = [str(item).strip().lower() for item in (raw or [])]
    result = []
    for value in values:
        if value in ALLOWED_STRATEGIES and value not in result:
            result.append(value)
    return result


def _empty_plan(now: datetime, *, option_chain: Any = None) -> Dict[str, Any]:
    return {
        "date": now.astimezone(IST).date().isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "trading_mode": "options_intraday",
        "asset": "options",
        "candidates": [],
        "option_candidates": [],
        "option_chain": option_chain,
        "option_chain_diagnostic": {},
        "options_pnl_readiness": "NOT_READY_FOR_OPTIONS_PNL",
        "historical_options_pnl": "NOT_READY",
        "universe": "NIFTY",
        "timeframe": "5m",
        "scanned": 1,
        "requested": 1,
        "failed": 0,
        "actionable": 0,
        "executed": False,
    }


def _row_value(row: Dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in row:
            return row.get(name)
    return None


def _dataframe_rows(frame: Any) -> List[Dict[str, Any]]:
    """Convert a DataFrame-like object without adding a pandas dependency."""
    if not hasattr(frame, "iterrows"):
        return []
    rows: List[Dict[str, Any]] = []
    columns = set(str(item) for item in getattr(frame, "columns", []))
    for index, series in frame.iterrows():
        row = dict(series)
        if "timestamp" not in row and "Timestamp" not in row:
            row["timestamp"] = index
        normalized = {}
        for key, value in row.items():
            normalized[str(key).lower()] = value
        # yfinance uses title-cased OHLCV columns; the lower-case map above
        # makes both provider and fixture DataFrames use one schema.
        rows.append(normalized)
    return rows


def _normalise_candles(
    raw: Any,
    now: datetime,
    *,
    max_age_minutes: float = 15.0,
    require_orb_opening_range: bool = True,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if raw is None:
        return [], {"valid": False, "reason": "no_candles"}
    if hasattr(raw, "iterrows"):
        source_rows = _dataframe_rows(raw)
    elif isinstance(raw, list):
        source_rows = list(raw)
    else:
        return [], {"valid": False, "reason": "unsupported_candle_container"}
    normalized: List[Dict[str, Any]] = []
    errors: List[str] = []
    seen = set()
    ignored = {
        "outside_session": 0,
        "future_or_incomplete": 0,
    }

    for row_number, source in enumerate(source_rows, start=1):
        if not isinstance(source, dict):
            errors.append(f"row_{row_number}:not_a_mapping")
            continue
        timestamp = _row_value(source, "timestamp", "Timestamp", "datetime", "date")
        parsed = _parse_timestamp(timestamp, allow_naive=False)
        if parsed is None:
            errors.append(f"row_{row_number}:timestamp_not_aware_or_invalid")
            continue
        key = parsed.astimezone(UTC)
        if key in seen:
            errors.append(f"row_{row_number}:duplicate_timestamp")
            continue
        seen.add(key)
        if parsed > now:
            ignored["future_or_incomplete"] += 1
            continue
        completion = parsed + BAR_SIZE
        if completion > now:
            # Providers commonly include the currently-forming candle.  It
            # must not poison an otherwise valid current-session dataset; it
            # is simply excluded until the next completed-bar pass.
            ignored["future_or_incomplete"] += 1
            continue

        open_price = _finite(_row_value(source, "open", "Open"))
        high_price = _finite(_row_value(source, "high", "High"))
        low_price = _finite(_row_value(source, "low", "Low"))
        close_price = _finite(_row_value(source, "close", "Close"))
        volume = _finite(_row_value(source, "volume", "Volume"))
        if None in (open_price, high_price, low_price, close_price):
            errors.append(f"row_{row_number}:non_finite_ohlc")
            continue
        if min(open_price, high_price, low_price, close_price) <= 0:
            errors.append(f"row_{row_number}:non_positive_ohlc")
            continue
        if high_price < max(open_price, close_price, low_price):
            errors.append(f"row_{row_number}:high_inconsistent")
            continue
        if low_price > min(open_price, close_price, high_price):
            errors.append(f"row_{row_number}:low_inconsistent")
            continue
        if volume is not None and volume < 0:
            errors.append(f"row_{row_number}:negative_volume")
            continue

        underlying = str(
            _row_value(source, "underlying_symbol", "underlying") or "NIFTY"
        ).strip().upper()
        exchange = str(_row_value(source, "exchange") or "NSE").strip().upper()
        instrument_type = str(
            _row_value(source, "instrument_type", "instrument") or "index"
        ).strip().lower()
        if underlying != "NIFTY":
            errors.append(f"row_{row_number}:underlying_not_nifty")
            continue
        if exchange != "NSE" or instrument_type != "index":
            errors.append(f"row_{row_number}:not_nse_index")
            continue
        if parsed.time() < NSE_OPEN or parsed.time() > NSE_LAST_BAR_START:
            # Historical yfinance intraday responses can contain pre/post
            # market rows.  They are irrelevant to the NSE signal and must
            # not make the current session fail closed.
            ignored["outside_session"] += 1
            continue
        if parsed.minute % 5 or parsed.second or parsed.microsecond:
            errors.append(f"row_{row_number}:not_a_5min_bar_start")
            continue
        normalized.append({
            "timestamp": _iso_ist(parsed),
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": volume,
            "exchange": exchange,
            "underlying_symbol": underlying,
            "instrument_type": instrument_type,
        })

    normalized.sort(key=lambda item: item["timestamp"])
    current_date = now.astimezone(IST).date()
    current_rows = [
        item for item in normalized
        if _parse_timestamp(item["timestamp"]).date() == current_date
    ]
    if not current_rows:
        errors.append("no_completed_bar_for_current_ist_date")
    else:
        for previous, current in zip(current_rows, current_rows[1:]):
            previous_ts = _parse_timestamp(previous["timestamp"])
            current_ts = _parse_timestamp(current["timestamp"])
            if current_ts - previous_ts != BAR_SIZE:
                errors.append(
                    f"gap_between_{previous['timestamp']}_{current['timestamp']}"
                )
        if require_orb_opening_range:
            expected_or = {
                time(9, 15), time(9, 20), time(9, 25)
            }
            present_or = {
                _parse_timestamp(item["timestamp"]).time() for item in current_rows
                if _parse_timestamp(item["timestamp"]).time() in expected_or
            }
            if not expected_or.issubset(present_or):
                errors.append("missing_orb_opening_range_bar")
            if not any(
                _parse_timestamp(item["timestamp"]).time() >= time(9, 30)
                for item in current_rows
            ):
                errors.append("no_completed_signal_bar_after_opening_range")

        latest_ts = _parse_timestamp(current_rows[-1]["timestamp"])
        age_minutes = (now - (latest_ts + BAR_SIZE)).total_seconds() / 60.0
        max_age = max_age_minutes
        if age_minutes < 0:
            errors.append("latest_bar_is_future_or_incomplete")
        if age_minutes > max_age:
            errors.append(f"stale_latest_bar:{age_minutes:.1f}m")

    diagnostic = {
        "valid": not errors,
        "rows": len(normalized),
        "current_date_rows": len(current_rows),
        "max_age_minutes": max_age_minutes,
        "latest_timestamp": current_rows[-1]["timestamp"] if current_rows else None,
        "latest_completion_timestamp": (
            _iso_ist(_parse_timestamp(current_rows[-1]["timestamp"]) + BAR_SIZE)
            if current_rows else None
        ),
        "errors": errors,
        "ignored_rows": ignored,
        "timestamp_timezone": "Asia/Kolkata",
    }
    return normalized, diagnostic


def _load_candles(cfg: Dict[str, Any], now: datetime, supplied: Any) -> Tuple[Any, Dict[str, Any]]:
    if supplied is not None:
        return supplied, {"source": "injected"}
    data_path = os.environ.get("TRIO_INTRADAY_DATA_PATH", "").strip()
    if data_path:
        try:
            from data_importer import import_5min_index_candles
            return import_5min_index_candles(
                data_path, underlying_symbol="NIFTY", exchange="NSE"
            ), {"source": "csv", "path": data_path}
        except Exception as exc:  # fail closed, with an auditable reason
            return [], {"source": "csv", "path": data_path, "error": str(exc)}
    mode_cfg = cfg.get("options_intraday", {}) or {}
    mapping = mode_cfg.get("underlying_mapping") or {}
    data_symbol = (
        mode_cfg.get("market_symbol")
        or mode_cfg.get("data_symbol")
        or mapping.get("NIFTY")
        or "^NSEI"
    )
    if str(mode_cfg.get("underlying", "NIFTY")).upper() != "NIFTY":
        return [], {"source": "provider", "error": "only NIFTY is permitted"}
    try:
        market_data = fetch_market_data(str(data_symbol), "5m")
        frame = getattr(market_data, "ohlcv", market_data)
        return frame, {
            "source": getattr(market_data, "source", "provider"),
            "symbol": str(data_symbol),
            "fetched_at": getattr(market_data, "fetched_at", None),
        }
    except Exception as exc:
        return [], {"source": "provider", "symbol": str(data_symbol), "error": str(exc)}


def _parse_expiry(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%B-%Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def _nse_expiry(value: Any) -> Optional[str]:
    parsed = _parse_expiry(value)
    return parsed.strftime("%d-%b-%Y") if parsed else None


def _select_expiry(chain: Dict[str, Any], now: datetime) -> Tuple[Optional[str], Optional[str]]:
    # The exchange listing is authoritative.  In particular, never select the
    # first row when expiryDates is empty or when all listed dates are expired.
    choices = []
    for raw in chain.get("expiries", []) or []:
        parsed = _parse_expiry(raw)
        canonical = _nse_expiry(raw)
        if parsed is not None and canonical is not None:
            choices.append((parsed.date(), canonical, str(raw)))
    future = [item for item in choices if item[0] >= now.astimezone(IST).date()]
    if not future:
        return None, None
    future.sort(key=lambda item: item[0])
    return future[0][1], future[0][2]


def _rows_for_expiry(chain: Dict[str, Any], raw_expiry: str,
                     canonical_expiry: str) -> List[Dict[str, Any]]:
    by_expiry = chain.get("by_expiry", {}) or {}
    if raw_expiry in by_expiry:
        return list(by_expiry.get(raw_expiry) or [])
    if canonical_expiry in by_expiry:
        return list(by_expiry.get(canonical_expiry) or [])
    # Never fall back to another expiry's rows.
    return []


def _chain_timestamp(chain: Dict[str, Any]) -> Any:
    records = chain.get("records") or {}
    return (
        chain.get("quote_timestamp")
        or chain.get("exchange_timestamp")
        or records.get("timestamp")
        or records.get("quote_timestamp")
    )


def _quote_timestamp(chain: Dict[str, Any], row: Dict[str, Any], quote: Dict[str, Any]) -> Any:
    return (
        quote.get("quote_timestamp")
        or quote.get("timestamp")
        or quote.get("row_timestamp")
        or row.get("quote_timestamp")
        or row.get("row_timestamp")
        or _chain_timestamp(chain)
    )


def _chain_diagnostic(chain: Any, now: datetime, *, expiry: Optional[str] = None) -> Dict[str, Any]:
    diagnostic = deepcopy((chain or {}).get("diagnostics") or {}) if isinstance(chain, dict) else {}
    diagnostic.setdefault("symbol", (chain or {}).get("symbol") if isinstance(chain, dict) else None)
    diagnostic.setdefault("retrieval", {})
    diagnostic["http_retrieved_at"] = (chain or {}).get("retrieved_at") if isinstance(chain, dict) else None
    diagnostic["selected_expiry"] = expiry
    diagnostic.setdefault("timestamp_diagnostics", {})
    return diagnostic


def _verified_lot_size(chain: Dict[str, Any], rows: List[Dict[str, Any]],
                       mode_cfg: Dict[str, Any], now: datetime) -> Tuple[Optional[int], Dict[str, Any]]:
    live_values = []
    for row in rows:
        for kind in ("CE", "PE"):
            quote = row.get(kind) or {}
            value = _row_value(quote, "lot_size", "lotSize")
            if value is None:
                value = _row_value(row, "lot_size", "lotSize")
            parsed = _finite(value)
            if parsed is not None:
                live_values.append(int(parsed))
    if live_values:
        if any(value <= 0 for value in live_values) or len(set(live_values)) != 1:
            return None, {"valid": False, "reason": "inconsistent_live_lot_size"}
        lot_size = live_values[0]
        return lot_size, {"valid": True, "source": "live_chain", "lot_size": lot_size}

    configured = mode_cfg.get("contract_specs") or {}
    if isinstance(configured, dict) and isinstance(configured.get("NIFTY"), dict):
        configured = configured["NIFTY"]
    if not isinstance(configured, dict):
        return None, {"valid": False, "reason": "missing_verified_contract_specs"}
    lot_value = _finite(configured.get("lot_size"))
    provenance = str(configured.get("provenance") or "").strip()
    valid_from = _parse_expiry(configured.get("valid_from"))
    valid_until = _parse_expiry(configured.get("valid_until"))
    today = now.astimezone(IST).date()
    if (
        lot_value is None or lot_value <= 0 or not provenance
        or valid_from is None or valid_until is None
        or valid_from.date() > today or valid_until.date() < today
    ):
        return None, {
            "valid": False,
            "reason": "contract_specs_require_lot_size_provenance_and_validity",
        }
    lot_size = int(lot_value)
    return lot_size, {
        "valid": True,
        "source": "configured_verified_specs",
        "lot_size": lot_size,
        "provenance": provenance,
        "valid_from": configured.get("valid_from"),
        "valid_until": configured.get("valid_until"),
    }


def _live_contracts(chain: Dict[str, Any], rows: List[Dict[str, Any]],
                    expiry: str, lot_size: int) -> Tuple[List[Dict[str, Any]], List[str]]:
    contracts: List[Dict[str, Any]] = []
    errors: List[str] = []
    chain_symbol = str(chain.get("symbol") or "NIFTY").upper()
    chain_exchange = str(chain.get("exchange") or "NSE").upper()
    for row_number, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            errors.append(f"row_{row_number}:not_a_mapping")
            continue
        row_expiry = row["expiry"] if "expiry" in row else expiry
        canonical_row_expiry = _nse_expiry(row_expiry) if row_expiry else None
        for kind in ("CE", "PE"):
            quote = row.get(kind) or {}
            if not isinstance(quote, dict):
                continue
            strike = _finite(row.get("strike"))
            bid = _finite(_row_value(quote, "bidPrice", "bid", "buyPrice1"))
            ask = _finite(_row_value(quote, "askPrice", "ask", "sellPrice1"))
            oi = _finite(_row_value(quote, "openInterest", "open_interest", "oi"))
            volume = _finite(_row_value(quote, "volume", "totalTradedVolume"))
            timestamp_value = _quote_timestamp(chain, row, quote)
            timestamp = _parse_timestamp(timestamp_value, allow_naive=True)
            if strike is None or bid is None or ask is None or oi is None or volume is None:
                errors.append(f"row_{row_number}_{kind}:missing_live_quote_fields")
                continue
            if timestamp is None:
                errors.append(f"row_{row_number}_{kind}:missing_or_bad_quote_timestamp")
                continue
            contracts.append({
                "strike": strike,
                "expiry": canonical_row_expiry or "",
                "option_type": kind,
                "underlying_symbol": str(row.get("underlying_symbol") or chain_symbol).upper(),
                "exchange": str(row.get("exchange") or chain_exchange).upper(),
                "bid": bid,
                "ask": ask,
                "open_interest": oi,
                "volume": volume,
                "quote_timestamp": _iso_ist(timestamp),
                "row_timestamp": row.get("row_timestamp") or timestamp_value,
                "lot_size": lot_size,
            })
    return contracts, errors


def _strategy_confidence(mode_cfg: Dict[str, Any], strategy: str, candidate: OptionsCandidate) -> int:
    signal_conf = getattr(candidate, "confidence", None)
    value = signal_conf
    configured = mode_cfg.get("confidence", mode_cfg.get("strategy_confidence"))
    if isinstance(configured, dict):
        value = configured.get(strategy, configured.get("default", value))
    elif configured is not None:
        value = configured
    parsed = _finite(value)
    return int(parsed if parsed is not None else 80)


def _candidate_dict(candidate: OptionsCandidate, *, rank: int, confidence: int,
                    option_contract: Dict[str, Any], expiry: str,
                    expiry_status: Dict[str, Any], lot_size: int,
                    quote_age: float, risk_budget: float,
                    signal_timestamp: str, strategy: str) -> Dict[str, Any]:
    direction = str(candidate.direction or "").lower()
    signal = "BUY" if direction == "bullish" else "SELL"
    kind = "CE" if signal == "BUY" else "PE"
    premium = float(option_contract["ask"])
    max_loss = round(premium * lot_size, 2)
    quote_timestamp = option_contract["quote_timestamp"]
    setup = candidate.strategy_name
    return {
        "asset": "options",
        "symbol": "NIFTY",
        "signal_underlying": "NIFTY",
        "option_underlying": "NIFTY",
        "underlying": "NIFTY",
        "exchange": "NSE",
        "signal_timestamp": signal_timestamp,
        "source": f"5m:{strategy}",
        "strategy": strategy,
        "setup": setup,
        "setup_name": setup,
        "direction": direction,
        "signal": signal,
        "action": signal,
        "order_side": "BUY",
        "entry_price": float(candidate.entry_price),
        "stop_loss": float(candidate.stop_loss),
        "target": float(candidate.target),
        "underlying_entry": float(candidate.entry_price),
        "underlying_stop_loss": float(candidate.stop_loss),
        "underlying_target": float(candidate.target),
        "confidence": confidence,
        "rank": rank,
        "rank_score": round(float(candidate.contract_rank_score), 4),
        "risk_budget": risk_budget,
        "risk_amount": max_loss,
        "position_size": lot_size,
        "lots": 1,
        "option_legs": {
            "strategy": "long-call" if kind == "CE" else "long-put",
            "underlying": "NIFTY",
            "spot": float(candidate.entry_price),
            "exchange": "NSE",
            "lot_size": lot_size,
            "expiry": expiry,
            "legs": [{
                "strike": float(option_contract["strike"]),
                "expiry": expiry,
                "kind": kind,
                "side": "BUY",
                "premium": premium,
                "premium_src": "live",
                "bid": float(option_contract["bid"]),
                "ask": premium,
                "oi": float(option_contract["open_interest"]),
                "open_interest": float(option_contract["open_interest"]),
                "volume": float(option_contract["volume"]),
                "quote_timestamp": quote_timestamp,
                "row_timestamp": option_contract.get("row_timestamp"),
            }],
            "net_debit": premium,
            "maxLoss": max_loss,
            "max_loss": max_loss,
            "rank": rank,
            "confidence": confidence,
            "risk_budget": risk_budget,
        },
        "option_diagnostic": {
            "signal_underlying": "NIFTY",
            "option_underlying": "NIFTY",
            "underlying_mapping": "NIFTY_index_only",
            "requested_expiry": expiry,
            "expiry_status": expiry_status,
            "selection_stage": "selected",
            "quote_timestamp": quote_timestamp,
            "quote_age_seconds": round(quote_age, 3),
            "lot_size_source": "verified",
            "rejection_reasons": [],
            "demand_supply": getattr(candidate, "demand_supply_confirmation", {
                "confirmed": True,
                "reason": "demand/supply disabled",
            }),
        },
        "signal_reason": candidate.signal_reason,
        "historical_options_pnl": "NOT_READY",
    }


def build_intraday_plan(
    cfg: Dict[str, Any],
    now: Optional[datetime] = None,
    candles: Any = None,
    chain: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a production ``options_intraday`` plan.

    ``candles`` and ``chain`` are injectable for deterministic replay.  When
    omitted, the builder uses the real ``^NSEI`` 5-minute provider and a
    cache-bypassing NSE chain request.  A failure at any data-quality or
    contract-identity gate returns an empty options plan with diagnostics.
    """
    from src.options_intraday import load_options_intraday_config
    cfg = load_options_intraday_config(cfg or {})
    now_ist = _as_now(now)
    plan = _empty_plan(now_ist, option_chain=chain)
    mode_cfg = cfg.get("options_intraday", {}) or {}
    underlying = str(mode_cfg.get("underlying", "NIFTY")).strip().upper()
    exchange = str(mode_cfg.get("exchange", "NSE")).strip().upper()
    plan["option_underlying"] = underlying

    if underlying != "NIFTY" or exchange != "NSE":
        plan["option_chain_diagnostic"] = {
            "valid": False,
            "failure_reason": "only NIFTY options on NSE are supported",
        }
        return plan

    selected_strategies = _strategies(cfg)
    plan["strategies"] = selected_strategies
    plan["demand_supply"] = {
        "enabled": demand_supply_enabled(
            cfg, context="options", timeframe="5m"),
        "context": "NIFTY 5m",
    }
    raw_candles, candle_source = _load_candles(cfg, now_ist, candles)
    max_candle_age = _cfg_float(
        mode_cfg,
        ("max_candle_age_minutes", "data_max_age_minutes"),
        15.0,
    )
    normalized_candles, candle_diag = _normalise_candles(
        raw_candles,
        now_ist,
        max_age_minutes=max_candle_age,
        require_orb_opening_range="orb" in selected_strategies,
    )
    candle_diag["source"] = candle_source
    plan["intraday_data_diagnostic"] = candle_diag
    if not normalized_candles or not candle_diag.get("valid"):
        plan["failed"] = 1
        plan["option_chain_diagnostic"] = {
            "valid": False,
            "failure_reason": "invalid_or_stale_intraday_data",
        }
        return plan

    if chain is None:
        try:
            chain = fetch_chain("NIFTY", use_cache=False)
        except Exception as exc:
            plan["option_chain_diagnostic"] = {
                "valid": False,
                "failure_reason": f"chain_fetch_exception: {exc}",
            }
            return plan
    plan["option_chain"] = chain
    if not isinstance(chain, dict):
        plan["option_chain_diagnostic"] = {
            "valid": False,
            "failure_reason": "chain_unavailable_or_invalid",
        }
        return plan
    if not str(chain.get("symbol") or "").strip():
        plan["option_chain_diagnostic"] = {
            "valid": False,
            "failure_reason": "chain_underlying_identity_missing",
        }
        return plan
    if str(chain.get("symbol") or "").upper() != "NIFTY":
        plan["option_chain_diagnostic"] = {
            "valid": False,
            "failure_reason": "chain_underlying_mismatch",
        }
        return plan

    expiry, raw_expiry = _select_expiry(chain, now_ist)
    plan_diag = _chain_diagnostic(chain, now_ist, expiry=expiry)
    plan_diag["selected_expiry_raw"] = raw_expiry
    if expiry is None or raw_expiry is None:
        plan_diag.update({
            "valid": False,
            "failure_reason": "no_future_exchange_listed_expiry",
        })
        plan["option_chain_diagnostic"] = plan_diag
        return plan

    from src.options_chain import expiry_status
    status = expiry_status(chain, raw_expiry)
    plan_diag["selected_expiry_status"] = status
    rows = _rows_for_expiry(chain, raw_expiry, expiry)
    if not status.get("listed_by_exchange") or not rows:
        plan_diag.update({"valid": False, "failure_reason": "invalid_or_empty_expiry_rows"})
        plan["option_chain_diagnostic"] = plan_diag
        return plan

    lot_size, lot_diag = _verified_lot_size(chain, rows, mode_cfg, now_ist)
    plan_diag["lot_size_diagnostic"] = lot_diag
    if lot_size is None:
        plan_diag.update({"valid": False, "failure_reason": lot_diag.get("reason")})
        plan["option_chain_diagnostic"] = plan_diag
        return plan

    live_contracts, quote_errors = _live_contracts(chain, rows, expiry, lot_size)
    max_quote_age = _cfg_float(
        mode_cfg,
        ("max_quote_age_minutes", "chain_cache_minutes"),
        _cfg_float(cfg.get("options", {}) or {}, ("chain_cache_minutes",), 15.0),
    )
    fresh_contracts = []
    stale_contracts = 0
    bad_timestamp_contracts = 0
    for contract in live_contracts:
        quote_time = _parse_timestamp(contract.get("quote_timestamp"), allow_naive=False)
        if quote_time is None:
            bad_timestamp_contracts += 1
            continue
        age = (now_ist - quote_time).total_seconds()
        if age < -60 or age > max_quote_age * 60:
            stale_contracts += 1
            continue
        fresh_contracts.append(contract)
    plan_diag["timestamp_diagnostics"] = {
        "fresh": bool(fresh_contracts),
        "max_age_minutes": max_quote_age,
        "fresh_contracts": len(fresh_contracts),
        "stale_contracts": stale_contracts,
        "bad_timestamp_contracts": bad_timestamp_contracts,
        "quote_timestamp_source": "exchange_record_or_option_leg",
    }
    plan_diag["quote_errors"] = quote_errors
    if not fresh_contracts:
        plan_diag.update({"valid": False, "failure_reason": "no_fresh_live_quotes"})
        plan["option_chain_diagnostic"] = plan_diag
        return plan

    risk_cfg = cfg.get("risk_management", {}) or {}
    risk_pct = _cfg_float(mode_cfg, ("risk_per_trade_pct",), 1.0)
    capital = _cfg_float(mode_cfg, ("capital",), _cfg_float(risk_cfg, ("capital",), 500000.0))
    risk_budget = round(capital * risk_pct / 100.0, 2)
    strike_interval = _cfg_float(
        mode_cfg,
        ("strike_step", "strike_interval"),
        _cfg_float(cfg.get("options", {}) or {}, ("strike_step",), 50.0),
    )
    contract_specs = {
        "lot_size": lot_size,
        "strike_interval": strike_interval,
        "strike_step": strike_interval,
        "expiry_preference": mode_cfg.get("expiry_preference", "weekly"),
    }
    min_oi = int(_cfg_float(
        mode_cfg,
        ("min_oi",),
        _cfg_float(cfg.get("options", {}) or {}, ("min_oi",), 1000.0),
    ))
    max_spread = _cfg_float(
        mode_cfg,
        ("max_spread_pct",),
        _cfg_float(cfg.get("options", {}) or {}, ("max_spread_pct",), 5.0),
    )

    latest_index = len(normalized_candles) - 1
    candidates: List[OptionsCandidate] = []
    strategy_diag: Dict[str, Any] = {}
    for strategy in selected_strategies:
        if strategy in {"pullback"}:
            latest_volume_missing = any(
                row.get("volume") is None for row in normalized_candles
            )
            if latest_volume_missing:
                strategy_diag[strategy] = {
                    "status": "rejected",
                    "reason": "index_volume_missing_for_volume_strategy",
                }
                continue
        try:
            generated = CandidatePipeline.evaluate_strategy_signals_pure(
                normalized_candles,
                strategy,
                underlying_symbol="NIFTY",
                contract_specs=contract_specs,
                index=latest_index,
            )
        except Exception as exc:
            strategy_diag[strategy] = {"status": "rejected", "reason": str(exc)}
            continue
        candidates.extend(generated)
        strategy_diag[strategy] = {
            "status": "signal" if generated else "no_signal",
            "signals": len(generated),
        }
    plan["strategy_diagnostics"] = strategy_diag

    if not candidates:
        plan_diag["valid"] = True
        plan_diag["failure_reason"] = "no_strategy_signal"
        plan["option_chain_diagnostic"] = plan_diag
        return plan

    selected: List[Tuple[OptionsCandidate, Dict[str, Any], float]] = []
    rejected = []
    latest_candle_ts = _parse_timestamp(normalized_candles[-1]["timestamp"])
    signal_timestamp = _iso_ist(latest_candle_ts + BAR_SIZE)
    for candidate in candidates:
        direction = str(candidate.direction or "").lower()
        expected_kind = "CE" if direction == "bullish" else "PE" if direction == "bearish" else ""
        if expected_kind not in {"CE", "PE"} or candidate.option_type.upper() != expected_kind:
            rejected.append({
                "strategy": candidate.strategy_name,
                "reason": "signal direction/option identity mismatch",
            })
            continue
        values = [candidate.entry_price, candidate.stop_loss, candidate.target]
        if any(_finite(value) is None or float(value) <= 0 for value in values):
            rejected.append({"strategy": candidate.strategy_name, "reason": "invalid underlying levels"})
            continue
        confirmation = confirm_demand_supply(
            normalized_candles,
            direction,
            index=latest_index,
            cfg=cfg,
            context="options",
        )
        candidate.demand_supply_confirmation = confirmation.to_dict()
        if not confirmation.confirmed:
            rejected.append({
                "strategy": candidate.strategy_name,
                "reason": "demand_supply_confirmation_failed",
                "details": [confirmation.reason],
            })
            continue
        result = select_option_contract(
            underlying_symbol="NIFTY",
            underlying_price=float(candidate.entry_price),
            signal_direction=direction,
            available_contracts=fresh_contracts,
            contract_specs=contract_specs,
            min_oi=min_oi,
            max_spread_pct=max_spread,
            moneyness_target="ATM",
            expected_exchange="NSE",
            expected_expiry=expiry,
            live_quote_only=True,
        )
        if "selected_contract" not in result:
            rejected.append({
                "strategy": candidate.strategy_name,
                "reason": result.get("rejection", {}).get("reason", "no eligible contract"),
                "details": result.get("rejection", {}).get("rejection_reasons", []),
            })
            continue
        contract = dict(result["selected_contract"])
        candidate.selected_contract = contract
        candidate.contract_eligibility = (True, "Eligible live contract selected")
        bid = float(contract["bid"])
        ask = float(contract["ask"])
        mid = (bid + ask) / 2.0
        spread_pct = (ask - bid) / mid * 100.0
        oi_bonus = min(float(contract["open_interest"]) / 10000.0, 10.0)
        candidate.contract_rank_score = 100.0 - spread_pct + oi_bonus
        quote_time = _parse_timestamp(contract["quote_timestamp"], allow_naive=False)
        quote_age = (now_ist - quote_time).total_seconds()
        selected.append((candidate, contract, quote_age))

    if not selected:
        plan_diag["valid"] = True
        plan_diag["failure_reason"] = "no_exact_eligible_long_contract"
        plan_diag["rejected_signals"] = rejected
        plan["option_chain_diagnostic"] = plan_diag
        return plan

    candidates_for_rank = [item[0] for item in selected]
    ranked_candidates = CandidatePipeline.rank_candidates_pure(candidates_for_rank)
    selected_by_id = {item[0].candidate_id: item for item in selected}
    option_candidates = []
    for rank, candidate in enumerate(ranked_candidates, start=1):
        chosen, contract, quote_age = selected_by_id[candidate.candidate_id]
        option_candidates.append(
            _candidate_dict(
                chosen,
                rank=rank,
                confidence=_strategy_confidence(
                    mode_cfg, str(chosen.strategy_name).lower(), chosen
                ),
                option_contract=contract,
                expiry=expiry,
                expiry_status=status,
                lot_size=lot_size,
                quote_age=quote_age,
                risk_budget=risk_budget,
                signal_timestamp=signal_timestamp,
                strategy=str(chosen.strategy_name).lower(),
            )
        )

    plan["option_candidates"] = option_candidates
    plan["actionable"] = len(option_candidates)
    plan_diag.update({
        "valid": True,
        "selected_expiry": expiry,
        "eligible_live_contracts": len(fresh_contracts),
        "selected_candidates": len(option_candidates),
        "rejected_signals": rejected,
    })
    # This field is intentionally the exchange quote timestamp rather than
    # the local HTTP retrieval time.  Existing runtime validation reads it as
    # the quote freshness clock; the HTTP time remains available above.
    first_quote = option_candidates[0]["option_legs"]["legs"][0]["quote_timestamp"]
    snapshot = deepcopy(chain)
    if not snapshot.get("quote_timestamp"):
        # A per-leg NSE timestamp is still an exchange timestamp.  Preserve
        # the injected/fetched rows and add the aggregate field expected by
        # the runtime revalidation boundary.
        snapshot["quote_timestamp"] = first_quote
    plan["option_chain"] = snapshot
    plan_diag["quote_timestamp"] = first_quote
    plan_diag["quote_timestamp_source"] = "exchange_quote_timestamp"
    plan_diag["retrieved_at"] = first_quote
    plan_diag["retrieved_at_source"] = "exchange_quote_timestamp"
    plan["option_chain_diagnostic"] = plan_diag
    return plan


__all__ = ["build_intraday_plan"]
