"""Strict options runtime for the remote-paper intraday modes.

This module owns the lifecycle after a plan has been generated.  It never
calls the equity order path.  A plan candidate is revalidated against its
private exchange chain snapshot, enriched with the exact MegaBull instrument
metadata, and submitted through ``open_spread`` as one long CE/PE leg.

    ``options_intraday`` remains options-only; ``combined`` shares the equity
    MegaBull account while this module still owns only strict option legs.

    The runtime keeps its own small, restart-safe intent/position ledger outside
the repository's tracked output tree.  The production :mod:`src.risk_manager`
remains the only risk state used for accounting; this ledger is an execution
reconciliation aid, not an experimental risk implementation.
"""

from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, TimeoutError as ScanTimeout
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from src.market_clock import IST, market_clock_from_config
from src.broker.megabull import ASSET_EQUITY, ASSET_OPTIONS, ASSET_UNKNOWN, classify_instrument
from src.options_intraday import (
    NOT_READY_FOR_OPTIONS_PNL,
    OPTIONS_INTRADAY,
    _finite,
    load_options_intraday_config,
    options_intraday_config,
    parse_exchange_timestamp,
    parse_timestamp,
    resolve_trading_mode,
    validate_options_intraday_risk_config,
    validate_option_candidate,
    validate_options_mode_environment,
)
from src.utils import get_logger

logger = get_logger("options_intraday_runtime")


class OptionsIntradayError(RuntimeError):
    """Base error for a fail-closed strict-options runtime."""


class OptionsIntradayPolicyError(OptionsIntradayError):
    """The plan, account mode, or concrete contract is not admissible."""


class OptionsIntradayUnresolved(OptionsIntradayError):
    """A remote state cannot safely be considered flat or reconciled.

    The exception deliberately contains identifiers only.  It never embeds a
    response body, request payload, credential, or raw remote error.
    """

    def __init__(self, identifiers: Iterable[Any], reason: str = "remote state unresolved"):
        self.identifiers = tuple(str(item) for item in identifiers if item)
        label = ", ".join(self.identifiers) or "options-book"
        super().__init__(f"OptionsIntradayUnresolved: {reason} [{label}]")


_FILLED = {"FILLED", "COMPLETE", "EXECUTED"}
_PENDING = {"PENDING", "SUBMITTED", "PLACED", "ACCEPTED", "QUEUED", "OPEN"}
_REJECTED = {"REJECTED", "CANCELLED", "FAILED", "DECLINED"}
_COMBINED = "combined"


def _finite_number(value: Any, default: float = 0.0) -> float:
    number = _finite(value)
    return default if number is None else number


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "to_dict"):
        try:
            converted = value.to_dict()
            return dict(converted) if isinstance(converted, dict) else {}
        except Exception:
            return {}
    return {}


def _status(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("status", "")).strip().upper()
    return str(getattr(value, "status", "")).strip().upper()


def _identifier(value: Any, *keys: str) -> str:
    if isinstance(value, dict):
        for key in keys:
            if value.get(key):
                return str(value[key])
    else:
        for key in keys:
            if getattr(value, key, None):
                return str(getattr(value, key))
    return ""


def _position_symbol(value: Any) -> str:
    return _identifier(value, "symbol", "trading_symbol", "instrument")


def _position_quantity(value: Any) -> int:
    raw = value.get("quantity", value.get("qty", 0)) if isinstance(value, dict) else getattr(value, "quantity", 0)
    try:
        return abs(int(float(raw or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _normalize_symbol(symbol: Any) -> str:
    return str(symbol or "").strip().upper().removesuffix(".NS")


def _now_ist(now: Optional[datetime]) -> datetime:
    current = now or datetime.now(IST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=IST)
    return current.astimezone(IST)


def _minutes(value: Any) -> Optional[int]:
    try:
        hour, minute = str(value).strip().split(":")
        hour_i, minute_i = int(hour), int(minute)
        if not (0 <= hour_i < 24 and 0 <= minute_i < 60):
            return None
        return hour_i * 60 + minute_i
    except (TypeError, ValueError):
        return None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _date_from(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _date_for_expiry(value: Any) -> Optional[date]:
    text = str(value or "").strip()
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _same_expiry(left: Any, right: Any) -> bool:
    l_date, r_date = _date_for_expiry(left), _date_for_expiry(right)
    if l_date is not None and r_date is not None:
        return l_date == r_date
    return str(left or "").strip() == str(right or "").strip()


class OptionsIntradayRuntime:
    """Restart-safe strict-options lifecycle controller.

    Parent integration surface::

        runtime = OptionsIntradayRuntime(trader, cfg)
        runtime.execute_plan(plan, opt_broker=None)
        runtime.tick(now=None)
        runtime.finalize(now=None)
        runtime.run(interval_seconds, max_seconds)
    """

    execution_class = "remote_paper"
    historical_options_pnl = NOT_READY_FOR_OPTIONS_PNL

    def __init__(self, trader: Any, cfg: Optional[Dict[str, Any]] = None,
                 trading_mode: Optional[str] = None):
        self.trader = trader
        source_cfg = cfg if cfg is not None else getattr(trader, "cfg", None)
        try:
            self.cfg = (load_options_intraday_config(source_cfg)
                        if source_cfg is not None else load_options_intraday_config())
        except ValueError as exc:
            raise OptionsIntradayPolicyError(str(exc)) from exc
        requested_mode = trading_mode or getattr(trader, "trading_mode", None)
        resolved_mode = resolve_trading_mode(self.cfg, requested_mode)
        if resolved_mode not in {OPTIONS_INTRADAY, _COMBINED}:
            raise OptionsIntradayPolicyError(
                "OptionsIntradayRuntime requires trading_mode=options_intraday or combined")
        self.trading_mode = resolved_mode
        ok, reason = validate_options_mode_environment(self.cfg)
        if not ok:
            raise OptionsIntradayPolicyError(reason)

        self.mode_cfg = options_intraday_config(self.cfg)
        valid_risk, risk_reason = validate_options_intraday_risk_config(self.mode_cfg)
        if not valid_risk:
            raise OptionsIntradayPolicyError(risk_reason)
        self._broker: Any = getattr(trader, "opt_broker", None)
        self._last_plan: Optional[Dict[str, Any]] = None
        self.last_scan_diagnostics: Dict[str, Any] = {}
        self._state_path = self._resolve_state_path()
        self._state = self._load_state()
        self._broker_blocked = bool(self._state.get("broker_blocked", False))
        self._chain_provider = self.mode_cfg.get("chain_provider")
        self._bar_provider = self.mode_cfg.get("completed_bar_provider")
        self._roll_daily_state(_now_ist(None).date().isoformat())

    # ------------------------------------------------------------------
    # Private local state.  It intentionally defaults outside the checkout.
    # ------------------------------------------------------------------

    def _resolve_state_path(self) -> Path:
        configured = (
            self.mode_cfg.get("state_path") or
            getattr(self.trader, "options_intraday_state_path", None) or
            os.environ.get("TRIO_OPTIONS_INTRADAY_STATE_PATH")
        )
        if configured:
            path = Path(str(configured)).expanduser()
            if path.suffix.lower() not in {".json", ".state"}:
                path = path / "options_intraday_state.json"
            return path
        state_dir = (self.mode_cfg.get("state_dir") or
                     os.environ.get("TRIO_OPTIONS_INTRADAY_STATE_DIR"))
        if state_dir:
            return Path(str(state_dir)).expanduser() / "options_intraday_state.json"
        # LOCALAPPDATA/home is private to the user and cannot become a tracked
        # output/runtime artifact in this repository.
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
        if root:
            return Path(root) / "TRIO" / "options_intraday" / "state.json"
        return Path.home() / ".trio" / "options_intraday" / "state.json"

    @staticmethod
    def _empty_state() -> Dict[str, Any]:
        return {
            "schema_version": 1,
            "session_date": "",
            "daily_entries": 0,
            "intents": {},
            "positions": {},
            "closed": [],
            "last_reconciled_at": "",
        }

    def _load_state(self) -> Dict[str, Any]:
        if not self._state_path.exists():
            return self._empty_state()
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("state is not an object")
            state = self._empty_state()
            state.update(raw)
            for key in ("intents", "positions"):
                if not isinstance(state.get(key), dict):
                    state[key] = {}
            if not isinstance(state.get("closed"), list):
                state["closed"] = []
            return state
        except Exception as exc:
            raise OptionsIntradayUnresolved(("state",), "private state unreadable") from exc

    def _persist(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._state, indent=2, sort_keys=True, default=str)
        temp = self._state_path.with_name(
            f".{self._state_path.name}.{os.getpid()}.tmp")
        temp.write_text(payload, encoding="utf-8")
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        temp.replace(self._state_path)

    def _roll_daily_state(self, today: str) -> None:
        previous = str(self._state.get("session_date") or "")
        if previous != today:
            from src.risk_manager import get_risk_state
            risk = get_risk_state()
            if previous or (risk.last_session_date and risk.last_session_date != today):
                risk.reset_daily()
            self._state["session_date"] = today
            self._state["daily_entries"] = 0
            self._persist()
        self._sync_closed_risk()

    def _sync_closed_risk(self) -> None:
        """Read back this runtime's umbrella closes into production RiskState.

        MegaBull may expose one record per remote leg.  Only the private
        runtime umbrella records are considered here, and a marker attached
        to the in-process production state prevents a second sync during a
        same-process restart/reconstruction.
        """
        try:
            from src.risk_manager import get_risk_state, update_pnl
            risk_state = get_risk_state()
            seen = getattr(risk_state, "_options_intraday_closed_ids", set())
            if not isinstance(seen, set):
                seen = set(seen or [])
            today = str(self._state.get("session_date") or "")
            for record in self._state.get("closed", []) or []:
                if not isinstance(record, dict):
                    continue
                closed_at = str(record.get("closed_at") or "")
                parsed = parse_timestamp(closed_at)
                if parsed is None or parsed.astimezone(IST).date().isoformat() != today:
                    continue
                close_id = str(record.get("position_id") or "") + "|" + closed_at
                if not close_id or close_id in seen:
                    continue
                update_pnl(_finite_number(record.get("pnl"), 0.0))
                seen.add(close_id)
            setattr(risk_state, "_options_intraday_closed_ids", seen)
            risk_state.last_session_date = today
        except Exception as exc:
            raise OptionsIntradayUnresolved(("risk-ledger",), "closed-risk read-back failed") from exc

    def risk_positions(self) -> Dict[str, Dict[str, Any]]:
        """Export open strict-option risk entries for the shared account book.

        The export is a copy and is asset-qualified so an equity lifecycle
        rebuild cannot mistake a contract or spread id for an equity symbol.
        It intentionally reports only open entries; realized option P&L is
        exported separately for rebuild/deduplication.
        """
        exported: Dict[str, Dict[str, Any]] = {}
        for position_id, raw in (self._state.get("positions", {}) or {}).items():
            if not isinstance(raw, dict):
                continue
            info = deepcopy(raw)
            lots = int(_finite_number(info.get("lots"), 0.0))
            entry = _finite_number(info.get("entry_premium"), 0.0)
            info["asset"] = ASSET_OPTIONS
            info["execution_class"] = self.execution_class
            info.setdefault("position_id", str(position_id))
            info.setdefault("symbol", str(position_id))
            info.setdefault("side", "LONG")
            info.setdefault("size", lots)
            info.setdefault("entry", entry)
            info.setdefault("risk_amount", _finite_number(info.get("risk_amount"), 0.0))
            exported[str(position_id)] = info
        return exported

    def strict_options_risk_positions(self) -> Dict[str, Dict[str, Any]]:
        """Named shared-risk export used by the execution lifecycle."""
        return self.risk_positions()

    def _strict_options_risk_positions(self) -> Dict[str, Dict[str, Any]]:
        """Private compatibility name for parent trader integrations."""
        return self.risk_positions()

    def strict_options_closed_trades(self) -> List[Dict[str, Any]]:
        """Export private option closes for one-time shared P&L rebuilding."""
        records: List[Dict[str, Any]] = []
        for raw in self._state.get("closed", []) or []:
            if not isinstance(raw, dict):
                continue
            record = dict(raw)
            record["asset"] = ASSET_OPTIONS
            record.setdefault("symbol", record.get("position_id", ""))
            record.setdefault("close_id", "options:" + str(
                record.get("position_id", "")) + "|" + str(record.get("closed_at", "")))
            records.append(record)
        return records

    def _strict_options_closed_trades(self) -> List[Dict[str, Any]]:
        return self.strict_options_closed_trades()

    # ------------------------------------------------------------------
    # Mode, clock, plan and broker boundaries
    # ------------------------------------------------------------------

    def _clock(self):
        raw = deepcopy(self.cfg)
        market = dict(raw.get("market", {}) or {})
        cutoff_values = [(_minutes(value), value) for value in (
            market.get("entry_cutoff", "15:00"),
            self.mode_cfg.get("entry_cutoff", "14:30"))]
        market["entry_cutoff"] = (min(cutoff_values)[1]
                                   if all(value[0] is not None for value in cutoff_values)
                                   else "invalid")
        configured_flat = _minutes(market.get("hard_flat", "15:10"))
        mode_flat = _minutes(self.mode_cfg.get("hard_flat", "15:10"))
        if configured_flat is None or mode_flat is None:
            market["hard_flat"] = "15:10"
        else:
            market["hard_flat"] = (self.mode_cfg.get("hard_flat", "15:10")
                                    if mode_flat < configured_flat
                                    else market.get("hard_flat", "15:10"))
        raw["market"] = market
        return market_clock_from_config(raw)

    def _require_broker(self, opt_broker: Any = None) -> Any:
        ok, reason = validate_options_mode_environment(self.cfg)
        if not ok:
            raise OptionsIntradayPolicyError(reason)
        broker = opt_broker or self._broker or getattr(self.trader, "opt_broker", None)
        if broker is None:
            ensure = getattr(self.trader, "ensure_option_broker", None)
            if callable(ensure):
                broker = ensure(self.cfg)
        if broker is None:
            raise OptionsIntradayPolicyError("remote MegaBull options broker is required")
        provider = str(getattr(broker, "provider_name", "")).lower()
        if provider != "megabull_options":
            raise OptionsIntradayPolicyError(
                "options_intraday refuses a local or non-MegaBull options broker")
        # Parent integration also sets this flag.  Setting it at this narrow
        # boundary makes accidental adapter fallback impossible for callers
        # which construct the runtime directly.
        try:
            broker.strict_options_only = True
        except Exception:
            pass
        self._broker = broker
        return broker

    def _plan_timestamp(self, plan: Dict[str, Any]) -> Optional[datetime]:
        diagnostics = plan.get("diagnostics", {}) or {}
        chain = plan.get("option_chain", {}) or {}
        for value in (
            plan.get("retrieved_timestamp"), plan.get("retrieved_at"),
            diagnostics.get("retrieved_timestamp"), diagnostics.get("retrieved_at"),
            chain.get("quote_timestamp"),
        ):
            parsed = parse_timestamp(value) or parse_exchange_timestamp(value)
            if parsed is not None:
                return parsed
        return None

    def _validate_plan_window(self, plan: Dict[str, Any], now: datetime) -> None:
        if not isinstance(plan, dict):
            raise OptionsIntradayPolicyError("options plan is not an object")
        if str(plan.get("trading_mode", "")).strip().lower() != OPTIONS_INTRADAY:
            raise OptionsIntradayPolicyError("plan trading_mode is not options_intraday")
        plan_date = _date_from(plan.get("date"))
        if plan_date is None or plan_date != now.date():
            raise OptionsIntradayPolicyError("plan date is not today's valid session")
        clock = self._clock()
        if not clock.valid or not clock.is_trading_day(now):
            raise OptionsIntradayPolicyError("invalid or closed trading day")
        if not clock.can_enter(now):
            raise OptionsIntradayPolicyError("options entry cutoff or session window passed")
        retrieved = self._plan_timestamp(plan)
        if retrieved is None:
            raise OptionsIntradayPolicyError("plan has no exchange retrieval timestamp")
        age = (now.astimezone(timezone.utc) - retrieved).total_seconds()
        max_age = _finite_number(self.mode_cfg.get("plan_max_age_minutes"), 15.0) * 60
        if age < 0 or age > max_age:
            raise OptionsIntradayPolicyError("options plan is future-dated or stale")

    def _candidate_age_ok(self, candidate: Dict[str, Any], now: datetime) -> bool:
        timestamp = parse_timestamp(candidate.get("signal_timestamp"))
        if timestamp is None:
            return False
        age = (now.astimezone(timezone.utc) - timestamp).total_seconds()
        max_age = _finite_number(self.mode_cfg.get("signal_max_age_minutes"), 60.0) * 60
        return 0 <= age <= max_age and timestamp.astimezone(IST).date() == now.date()

    # ------------------------------------------------------------------
    # Remote reconciliation.  It only observes positions; it never closes
    # an unknown or equity position.
    # ------------------------------------------------------------------

    def _refresh_source(self, source: Any) -> None:
        refresh = getattr(source, "refresh_positions", None)
        if not callable(refresh):
            return
        try:
            result = refresh()
        except Exception as exc:
            raise OptionsIntradayUnresolved(("remote-mirror",), "remote mirror unhealthy") from exc
        if result is False:
            raise OptionsIntradayUnresolved(("remote-mirror",), "remote mirror unhealthy")

    def _remote_asset(self, source: Any, value: Any) -> str:
        """Classify a mirrored holding before it enters option reconciliation.

        The strict options adapter owns an option book, so its umbrella
        positions are option candidates even when the synthetic spread id has
        no instrument-cache row.  Holdings observed through the shared
        MegaBull equity mirror are classified through its cache-only helper;
        known equities are ignored and unknowns remain blocking candidates.
        """
        provider = str(getattr(source, "provider_name", "")).strip().lower()
        symbol = _position_symbol(value)
        metadata = _as_dict(value)
        if provider == "megabull_options":
            # An explicitly identified equity is still ignored even if a
            # faulty adapter exposes it through the options wrapper.  A bare
            # unknown wrapper holding remains a blocking option candidate.
            if str(metadata.get("asset", "")).strip().lower() in {ASSET_EQUITY, "stock"}:
                return ASSET_EQUITY
            identified = classify_instrument(symbol, metadata)
            if identified == ASSET_EQUITY:
                return ASSET_EQUITY
            return ASSET_OPTIONS

        classifier = getattr(source, "classify_position", None)
        if callable(classifier):
            try:
                return str(classifier(symbol, value)).strip().lower()
            except Exception:
                return ASSET_UNKNOWN
        explicit_asset = str(metadata.get("asset", "")).strip().lower()
        if explicit_asset in {ASSET_EQUITY, "stock"}:
            return ASSET_EQUITY
        if explicit_asset in {ASSET_OPTIONS, "option"}:
            return ASSET_OPTIONS
        return classify_instrument(symbol, metadata)

    def _remote_positions(self, broker: Any) -> List[Any]:
        sources: List[Any] = []
        for source in (
            broker,
            getattr(broker, "mega", None),
            getattr(self.trader, "broker", None),
        ):
            if source is not None and all(source is not existing for existing in sources):
                sources.append(source)
        positions: List[Any] = []
        for source in sources:
            self._refresh_source(source)
            raw = getattr(source, "positions", None)
            if isinstance(raw, dict):
                values = list(raw.values())
            elif raw is not None:
                values = list(raw) if isinstance(raw, (list, tuple, set)) else []
            else:
                getter = getattr(source, "get_positions", None)
                try:
                    values = list(getter() or []) if callable(getter) else []
                except Exception as exc:
                    raise OptionsIntradayUnresolved(("remote-mirror",), "remote positions unavailable") from exc
            for value in values:
                if _position_quantity(value) <= 0:
                    continue
                # Combined mode shares the equity broker/account.  Only
                # option holdings participate in this strict runtime's
                # ownership check; a cache-proven equity position is normal
                # and must not block option entries or exits.
                if self._remote_asset(source, value) == ASSET_EQUITY:
                    continue
                positions.append(value)
        return positions

    def _owned_remote_symbols(self) -> set[str]:
        owned: set[str] = set()
        for sid, position in (self._state.get("positions", {}) or {}).items():
            owned.add(_normalize_symbol(sid))
            if isinstance(position, dict):
                for value in (position.get("trading_symbol"), *(position.get("remote_symbols", []) or [])):
                    if value:
                        owned.add(_normalize_symbol(value))
        return owned

    def _reconcile(self, broker: Any) -> List[Any]:
        positions = self._remote_positions(broker)
        remote_symbols = {_normalize_symbol(_position_symbol(item)) for item in positions}
        remote_symbols.discard("")
        owned = self._owned_remote_symbols()
        unknown = sorted(remote_symbols - owned)
        if unknown:
            self._broker_blocked = True
            self._state["broker_blocked"] = True
            self._persist()
            try:
                from src.risk_manager import set_broker_uncertain
                set_broker_uncertain("options remote book has unknown holdings")
            except Exception:
                pass
            raise OptionsIntradayUnresolved(unknown, "unknown remote holding")
        missing: List[str] = []
        for sid, position in (self._state.get("positions", {}) or {}).items():
            expected = {_normalize_symbol(sid)}
            if isinstance(position, dict):
                expected.update(_normalize_symbol(value) for value in
                               [position.get("trading_symbol"),
                                *(position.get("remote_symbols", []) or [])]
                               if value)
            if not (expected & remote_symbols):
                missing.append(str(sid))
        if missing:
            self._broker_blocked = True
            self._state["broker_blocked"] = True
            self._persist()
            try:
                from src.risk_manager import set_broker_uncertain
                set_broker_uncertain("owned options position is not mirrored")
            except Exception:
                pass
            raise OptionsIntradayUnresolved(missing, "owned remote position missing")
        was_blocked = self._broker_blocked
        self._broker_blocked = False
        self._state["broker_blocked"] = False
        if was_blocked:
            try:
                from src.risk_manager import clear_broker_uncertain
                clear_broker_uncertain()
            except Exception:
                pass
        self._state["last_reconciled_at"] = _iso(datetime.now(timezone.utc))
        self._persist()
        return positions

    # ------------------------------------------------------------------
    # Concrete instrument validation and adapter payload construction.
    # ------------------------------------------------------------------

    def _instrument_lookup(self, broker: Any, symbol: str) -> Dict[str, Any]:
        providers = [
            broker,
            getattr(broker, "mega", None),
            getattr(self.trader, "broker", None),
        ]
        row: Any = None
        for source in providers:
            lookup = getattr(source, "instrument_for", None)
            if callable(lookup):
                try:
                    row = lookup(symbol)
                except Exception:
                    row = None
                if row:
                    break
        if not isinstance(row, dict):
            raise OptionsIntradayPolicyError("exact MegaBull option instrument metadata is unavailable")
        return dict(row)

    @staticmethod
    def _row_value(row: Dict[str, Any], *keys: str) -> Any:
        for key in keys:
            if key in row and row[key] not in (None, ""):
                return row[key]
            lower = key.lower()
            for actual, value in row.items():
                if str(actual).lower() == lower and value not in (None, ""):
                    return value
        return None

    def _validate_instrument(self, candidate: Dict[str, Any], option: Dict[str, Any],
                             leg: Dict[str, Any], broker: Any) -> Tuple[str, Dict[str, Any]]:
        underlying = str(option.get("underlying") or candidate.get("option_underlying") or "").upper()
        expiry = str(option.get("expiry") or leg.get("expiry") or "")
        strike = _finite_number(leg.get("strike"), -1.0)
        kind = str(leg.get("kind") or "").upper()
        trading_symbol = (
            leg.get("trading_symbol") or option.get("trading_symbol") or
            (option.get("metadata", {}) or {}).get("trading_symbol") or
            candidate.get("trading_symbol") or
            (candidate.get("metadata", {}) or {}).get("trading_symbol")
        )
        if not trading_symbol:
            try:
                from src.broker.megabull_options import option_symbol
                trading_symbol = option_symbol(underlying, expiry, strike, kind)
            except Exception as exc:
                raise OptionsIntradayPolicyError("option trading symbol is missing") from exc
        trading_symbol = str(trading_symbol).strip()
        row = self._instrument_lookup(broker, trading_symbol)
        actual_symbol = self._row_value(row, "tradingSymbol", "trading_symbol", "symbol")
        token = self._row_value(row, "instrumentToken", "instrument_token", "token")
        exchange = self._row_value(row, "exchange", "exchangeSegment")
        instrument_type = self._row_value(row, "instrumentType", "instrument_type", "segment")
        actual_lot = self._row_value(row, "lotSize", "lot_size", "lot")
        actual_expiry = self._row_value(row, "expiry", "expiryDate", "expiry_date")
        actual_strike = self._row_value(row, "strike", "strikePrice", "strike_price")
        actual_underlying = self._row_value(row, "underlying", "underlyingSymbol", "instrumentName")
        if any(value in (None, "") for value in
               (actual_symbol, token, exchange, instrument_type, actual_lot,
                actual_expiry, actual_strike)):
            raise OptionsIntradayPolicyError("MegaBull instrument metadata is incomplete")
        if str(actual_symbol).strip().upper() != trading_symbol.upper():
            raise OptionsIntradayPolicyError("MegaBull instrument symbol mismatch")
        if str(exchange).strip().upper() != str(option.get("exchange", "NSE")).upper():
            raise OptionsIntradayPolicyError("MegaBull instrument exchange mismatch")
        if "OPT" not in str(instrument_type).upper() and "OPTION" not in str(instrument_type).upper():
            raise OptionsIntradayPolicyError("MegaBull instrument is not an option")
        if not trading_symbol.upper().endswith(kind):
            raise OptionsIntradayPolicyError("MegaBull instrument CE/PE mismatch")
        try:
            if float(actual_lot) != int(option.get("lot_size")):
                raise OptionsIntradayPolicyError("MegaBull instrument lot-size mismatch")
        except (TypeError, ValueError, OverflowError) as exc:
            raise OptionsIntradayPolicyError("MegaBull instrument lot-size is invalid") from exc
        if not _same_expiry(actual_expiry, expiry):
            raise OptionsIntradayPolicyError("MegaBull instrument expiry mismatch")
        if abs(_finite_number(actual_strike, -1.0) - strike) > 1e-8:
            raise OptionsIntradayPolicyError("MegaBull instrument strike mismatch")
        actual_root = str(actual_underlying or "").strip().upper()
        if actual_root and actual_root != underlying and not actual_root.startswith(underlying + " "):
            raise OptionsIntradayPolicyError("MegaBull instrument underlying mismatch")
        if not trading_symbol.upper().startswith(underlying) or not trading_symbol[len(underlying):len(underlying) + 1].isdigit():
            raise OptionsIntradayPolicyError("MegaBull instrument underlying root mismatch")
        return trading_symbol, row

    def _adapter_payload(self, candidate: Dict[str, Any], option: Dict[str, Any],
                         row: Dict[str, Any], trading_symbol: str,
                         quote_timestamp: Any) -> Dict[str, Any]:
        payload = deepcopy(option)
        payload["legs"] = deepcopy(option.get("legs") or [])
        payload["asset"] = "options"
        payload["execution_class"] = self.execution_class
        payload["validated_contract"] = deepcopy(row)
        payload["lot_size"] = int(option["lot_size"])
        payload["exchange"] = str(option.get("exchange", "NSE"))
        payload["trading_symbol"] = trading_symbol
        payload["quote_timestamp"] = quote_timestamp
        payload["order_action"] = "BUY"
        payload["strict_options_only"] = True
        payload["signal_action"] = str(candidate.get("action", "")).upper()
        payload["signal_underlying"] = candidate.get("signal_underlying", "")
        payload["option_underlying"] = candidate.get(
            "option_underlying", option.get("underlying", ""))
        payload["signal_timestamp"] = candidate.get("signal_timestamp")
        payload["direction"] = candidate.get("direction")
        return payload

    # ------------------------------------------------------------------
    # Risk and sizing.  The unit is a complete exchange lot and the amount
    # reserved is the option debit plus round-trip costs, never underlying
    # points or a stock-style stop distance.
    # ------------------------------------------------------------------

    def _option_settings(self) -> Dict[str, Any]:
        return dict(self.cfg.get("options", {}) or {})

    def _capital(self) -> float:
        risk_cfg = self.cfg.get("risk_management", {}) or {}
        raw = self.mode_cfg.get("capital", risk_cfg.get("capital"))
        capital = _finite(raw)
        if capital is None or capital <= 0:
            raise OptionsIntradayPolicyError(
                "options_intraday requires finite positive risk capital")
        return capital

    def _max_open_positions(self) -> int:
        value = self.mode_cfg.get("max_open_positions")
        if isinstance(value, bool) or _finite(value) is None:
            raise OptionsIntradayPolicyError(
                "options_intraday max_open_positions is invalid")
        return int(value)

    def _brokerage_per_order(self) -> float:
        value = _finite(self._option_settings().get("brokerage_per_order"))
        if value is None or value < 0:
            raise OptionsIntradayPolicyError(
                "options_intraday brokerage_per_order is unavailable")
        return value

    def _round_trip_charges(self) -> float:
        """Return the configured entry-plus-exit brokerage for one long leg."""
        return round(self._brokerage_per_order() * 2.0, 2)

    def _stop_loss_cash(self, ask: float, lots: int, lot_size: int) -> float:
        """Cash loss at the initial 20% premium stop plus round-trip brokerage."""
        if lots <= 0 or ask <= 0 or lot_size <= 0:
            return 0.0
        stop_fraction = _finite(self.mode_cfg.get("stop_premium_fraction"))
        if stop_fraction is None:
            raise OptionsIntradayPolicyError(
                "options_intraday stop_premium_fraction is unavailable")
        return round(ask * stop_fraction * lots * lot_size +
                     self._round_trip_charges(), 2)

    def _full_debit(self, ask: float, lots: int, lot_size: int) -> float:
        """Worst-case long debit reservation including round-trip charges."""
        if lots <= 0 or ask <= 0 or lot_size <= 0:
            return 0.0
        return round(ask * lots * lot_size + self._round_trip_charges(), 2)

    def _position_premium_exposure(self, position: Dict[str, Any]) -> float:
        explicit = _finite(position.get("premium_exposure"))
        if explicit is not None and explicit >= 0:
            return explicit
        ask = _finite(position.get("ask_premium"))
        lots = _finite(position.get("lots"))
        lot_size = _finite(position.get("lot_size"))
        if ask is None or lots is None or lot_size is None:
            raise OptionsIntradayPolicyError(
                "options position has no complete ask-debit exposure")
        return self._full_debit(ask, int(lots), int(lot_size))

    def _reserved_premium_exposure(self) -> float:
        total = 0.0
        for position in (self._state.get("positions", {}) or {}).values():
            if isinstance(position, dict):
                total += self._position_premium_exposure(position)
        for intent in (self._state.get("intents", {}) or {}).values():
            if (isinstance(intent, dict) and
                    intent.get("status") in {"pending", "uncertain"}):
                exposure = _finite(intent.get("premium_exposure"))
                if exposure is None or exposure < 0:
                    raise OptionsIntradayPolicyError(
                        "options intent has no complete premium exposure")
                total += exposure
        return round(total, 2)

    def _active_position_count(self) -> int:
        count = sum(1 for item in (self._state.get("positions", {}) or {}).values()
                    if isinstance(item, dict))
        count += sum(1 for item in (self._state.get("intents", {}) or {}).values()
                     if isinstance(item, dict) and
                     item.get("status") in {"pending", "uncertain"})
        return count

    def _underlying_has_active_entry(self, underlying: str) -> bool:
        wanted = _normalize_symbol(underlying)
        for position in (self._state.get("positions", {}) or {}).values():
            if (isinstance(position, dict) and
                    _normalize_symbol(position.get("option_underlying")) == wanted):
                return True
        for intent in (self._state.get("intents", {}) or {}).values():
            if (isinstance(intent, dict) and
                    intent.get("status") in {"pending", "uncertain"} and
                    _normalize_symbol(intent.get("option_underlying")) == wanted):
                return True
        return False

    def _balance_available(self, broker: Any) -> float:
        sources: List[Any] = []
        for source in (getattr(self.trader, "broker", None), broker):
            if source is not None and all(source is not item for item in sources):
                sources.append(source)
        saw_balance = False
        for source in sources:
            getter = getattr(source, "get_balance", None)
            if not callable(getter):
                continue
            saw_balance = True
            try:
                balance = getter()
            except Exception:
                continue
            if not isinstance(balance, dict):
                continue
            value = balance.get("available", balance.get("available_balance"))
            if isinstance(value, bool):
                continue
            number = _finite(value)
            if number is not None and number >= 0:
                return number
        if saw_balance:
            raise OptionsIntradayPolicyError("remote available balance is not authoritative")
        raise OptionsIntradayPolicyError("remote available balance is unavailable")

    def _risk_state(self) -> Any:
        from src.risk_manager import get_risk_state
        return get_risk_state()

    def _reserved_risk(self) -> float:
        total = 0.0
        for position in (self._state.get("positions", {}) or {}).values():
            if isinstance(position, dict):
                total += max(0.0, _finite_number(position.get("risk_amount"), 0.0))
        for intent in (self._state.get("intents", {}) or {}).values():
            if isinstance(intent, dict) and intent.get("status") in {"pending", "uncertain"}:
                total += max(0.0, _finite_number(intent.get("risk_amount"), 0.0))
        return total

    def _fresh_mark(self, position: Dict[str, Any]) -> Optional[float]:
        """Return a bid only when this management cycle proved it fresh."""
        if position.get("mark_unresolved") or position.get("mark_status") == "unresolved":
            return None
        marked_at = _finite_number(position.get("last_bid_at_ms"), -1.0)
        cycle = _finite_number(self._state.get("mark_cycle_ms"), 0.0)
        if marked_at < 0 or cycle <= 0 or marked_at < cycle:
            return None
        bid = _finite(position.get("last_bid"))
        if bid is None or bid <= 0:
            return None
        return bid

    def _unrealized_pnl(self) -> Optional[float]:
        pnl = 0.0
        for position in (self._state.get("positions", {}) or {}).values():
            if not isinstance(position, dict):
                continue
            entry = _finite_number(position.get("entry_premium"), 0.0)
            lots = int(_finite_number(position.get("lots"), 0.0))
            lot_size = int(_finite_number(position.get("lot_size"), 0.0))
            if entry <= 0 or lots <= 0 or lot_size <= 0:
                continue
            bid = self._fresh_mark(position)
            if bid is None:
                return None
            pnl += (bid - entry) * lots * lot_size
        return pnl

    def _group_for(self, underlying: str) -> Optional[set[str]]:
        wanted = str(underlying).strip().upper()
        for group in self.mode_cfg.get("correlated_groups", []) or []:
            values = {str(item).strip().upper() for item in (group or [])}
            if wanted in values:
                return values
        return None

    def _correlated_count(self, group: Optional[set[str]]) -> int:
        if not group:
            return 0
        count = sum(1 for position in (self._state.get("positions", {}) or {}).values()
                    if isinstance(position, dict) and
                    str(position.get("option_underlying", "")).upper() in group)
        count += sum(
            1 for intent in (self._state.get("intents", {}) or {}).values()
            if isinstance(intent, dict)
            and intent.get("status") in {"pending", "uncertain"}
            and str(intent.get("option_underlying", "")).upper() in group
        )
        return count

    def _candidate_key(self, option: Dict[str, Any], leg: Dict[str, Any]) -> str:
        return "|".join((
            str(option.get("underlying", "")).strip().upper(),
            str(option.get("expiry", "")).strip(),
            str(_finite_number(leg.get("strike"), 0.0)),
            str(leg.get("kind", "")).strip().upper(),
        ))

    def _risk_preflight(self, candidate: Dict[str, Any], option: Dict[str, Any],
                        leg: Dict[str, Any], broker: Any) -> Tuple[int, float, str]:
        state = self._risk_state()
        if bool(getattr(state, "halt_active", False)):
            return 0, 0.0, "production risk halt is active"
        if self._active_position_count() >= self._max_open_positions():
            return 0, 0.0, "options max-open-position limit reached"
        daily_entries = int(_finite_number(self._state.get("daily_entries"), 0.0))
        max_entries = int(_finite_number(self.mode_cfg.get("max_daily_entries"), 5.0))
        if daily_entries >= max_entries:
            return 0, 0.0, "options daily-entry limit reached"
        max_consecutive = int(_finite_number(
            self.mode_cfg.get("max_consecutive_losses"), 2.0))
        if max_consecutive > 0 and int(getattr(state, "consecutive_losses", 0) or 0) >= max_consecutive:
            return 0, 0.0, "options consecutive-loss limit reached"

        capital = self._capital()
        max_loss_pct = _finite(self.mode_cfg.get("max_daily_loss_pct"))
        risk_pct = _finite(self.mode_cfg.get("risk_per_trade_pct"))
        exposure_pct = _finite(self.mode_cfg.get("max_premium_exposure_pct"))
        if max_loss_pct is None or risk_pct is None or exposure_pct is None:
            return 0, 0.0, "options strict risk policy is unavailable"
        unrealized_pnl = self._unrealized_pnl()
        if unrealized_pnl is None:
            return 0, 0.0, "options mark is unresolved; new entries are blocked"
        realized_plus_unrealized = (
            _finite_number(getattr(state, "daily_pnl", 0.0), 0.0) + unrealized_pnl)
        daily_limit = capital * max_loss_pct / 100.0
        # Equality is a hard stop: no entry may consume any amount after the
        # realized-plus-unrealized daily budget reaches -1.50% of capital.
        if realized_plus_unrealized <= -daily_limit:
            return 0, 0.0, "options daily-loss budget exhausted"
        daily_remaining = daily_limit + realized_plus_unrealized

        underlying = str(option.get("underlying", "")).upper()
        if (not self.mode_cfg.get("allow_averaging_down") and
                self._underlying_has_active_entry(underlying)):
            return 0, 0.0, "options averaging down is disabled for this underlying"

        group = self._group_for(underlying)
        group_limit = int(_finite_number(
            self.mode_cfg.get("max_correlated_positions"), 1.0))
        if group and self._correlated_count(group) >= group_limit:
            return 0, 0.0, "correlated-options limit reached"

        ask = _finite_number(leg.get("ask"), 0.0)
        lot_size = int(_finite_number(option.get("lot_size"), 0.0))
        if ask <= 0 or lot_size <= 0:
            return 0, 0.0, "complete-lot debit is unavailable"
        available = self._balance_available(broker)
        exposure_limit = capital * exposure_pct / 100.0
        exposure_remaining = exposure_limit - self._reserved_premium_exposure()
        if exposure_remaining <= 0:
            return 0, 0.0, "options premium-exposure limit reached"
        if exposure_remaining < self._full_debit(ask, 1, lot_size):
            return 0, 0.0, "options premium-exposure limit reached"

        # Bound by the cash-loss cap (premium loss at the initial stop plus
        # brokerage), the remaining daily budget, available cash, and full
        # ask-debit exposure.  Fixed brokerage is checked again below because
        # it means simple division can overstate the affordable lot count.
        risk_budget = min(capital * risk_pct / 100.0, daily_remaining)
        risk_per_premium_lot = ask * _finite(self.mode_cfg["stop_premium_fraction"]) * lot_size
        lots = int(min(
            available / max(ask * lot_size, 0.01),
            exposure_remaining / max(ask * lot_size, 0.01),
            risk_budget / max(risk_per_premium_lot, 0.01),
        ))
        max_lots = int(_finite_number(self.mode_cfg.get("max_lots"), 0.0))
        if max_lots > 0:
            lots = min(lots, max_lots)
        while lots > 0 and (
                self._full_debit(ask, lots, lot_size) >
                min(available, exposure_remaining) or
                self._stop_loss_cash(ask, lots, lot_size) > risk_budget):
            lots -= 1
        if lots <= 0:
            return 0, 0.0, "complete lot is not affordable within strict risk limits"
        return lots, self._stop_loss_cash(ask, lots, lot_size), ""

    # ------------------------------------------------------------------
    # Entry lifecycle
    # ------------------------------------------------------------------

    def _record_event(self, event: str, record: Dict[str, Any]) -> None:
        logger.info("options_intraday %s %s", event, record.get("position_id", ""))
        logger_method = getattr(self.trader, "_log_event", None)
        if callable(logger_method):
            try:
                logger_method(event, dict(record))
            except Exception:
                pass

    def _intent(self, key: str, candidate: Dict[str, Any], payload: Dict[str, Any],
                lots: int, risk_amount: float,
                premium_exposure: float) -> Dict[str, Any]:
        option = payload
        leg = (option.get("legs") or [{}])[0]
        return {
            "key": key,
            "status": "pending",
            "created_at": _iso(datetime.now(timezone.utc)),
            "signal_underlying": candidate.get("signal_underlying", ""),
            "option_underlying": option.get("underlying", ""),
            "expiry": option.get("expiry", ""),
            "strike": _finite_number(leg.get("strike"), 0.0),
            "kind": str(leg.get("kind", "")).upper(),
            "trading_symbol": payload.get("trading_symbol", ""),
            "remote_symbols": [payload.get("trading_symbol", "")],
            "lots": lots,
            "lot_size": int(option.get("lot_size", 0) or 0),
            "risk_amount": risk_amount,
            "ask_premium": _finite_number(leg.get("ask"), 0.0),
            "premium_exposure": premium_exposure,
            "signal_timestamp": candidate.get("signal_timestamp"),
            "order_id": "",
        }

    def _query_order(self, broker: Any, order: Any) -> Any:
        oid = _identifier(order, "order_id", "id")
        getter = getattr(broker, "get_order_status", None)
        if oid and callable(getter):
            try:
                return getter(oid)
            except Exception:
                return order
        return order

    def _current_option_rows(self, broker: Any) -> List[Dict[str, Any]]:
        for source in (getattr(broker, "mega", None), broker,
                       getattr(self.trader, "broker", None)):
            listing = getattr(source, "option_instruments", None)
            if callable(listing):
                return list(listing())
        raise OptionsIntradayPolicyError("current broker option listings are unavailable")

    def _read_only_scan(self, cfg: Dict[str, Any], clock: Callable[[], datetime],
                        instrument_rows: Any) -> Dict[str, Any]:
        """Only discovery runs in the worker; all orders/management stay serial.

        Slow batch/chain requests must not pause the 60-second exit cadence.
        An abandoned read-only request cannot submit orders in the background.
        """
        from src.options_live_scan import build_live_options_scan
        previous = getattr(self, "_scan_future", None)
        if previous is not None and not previous.done():
            raise OptionsIntradayPolicyError("previous read-only scan is still running")
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="option-discovery")
        future = executor.submit(
            build_live_options_scan, cfg, now=clock(), instrument_rows=instrument_rows,
            market_data=self.mode_cfg.get("market_data_provider"),
            chain_provider=self._chain_provider)
        self._scan_future = future
        cadence = max(1.0, _finite_number(
            getattr(self, "management_interval_seconds", 60.0), 60.0))
        try:
            while True:
                try:
                    return future.result(timeout=cadence)
                except ScanTimeout:
                    if future.done():
                        # A provider-raised TimeoutError is a scan failure,
                        # not a reason to poll the completed future forever.
                        return future.result()
                    self.tick(now=clock())
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def scan_and_execute(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Scan the configured broad universe, then freshly confirm each entry.

        No saved plan is read or written. Planner dictionaries are ephemeral
        signal/quote envelopes only. A second, single-underlying scan must
        still produce the same contract, direction and strategy before entry.
        """
        from src.options_live_scan import _metadata, _symbol_cfg, _universe
        initial, started = _now_ist(now), time.monotonic()

        def clock():
            return initial + timedelta(seconds=time.monotonic() - started)

        self._roll_daily_state(initial.date().isoformat())
        broker = self._require_broker()
        self._reconcile(broker)
        if not self._clock().can_enter(initial):
            return []
        rows: List[Dict[str, Any]] = []

        def listings():
            rows[:] = self._current_option_rows(broker)
            return rows

        found = self._read_only_scan(self.cfg, clock, listings)
        self.last_scan_diagnostics = deepcopy(found["diagnostics"])
        self.last_scan_diagnostics.update(submitted=0, entry_rejections=[])
        logger.info("Options scan: %s", {key: self.last_scan_diagnostics.get(key, 0)
                    for key in ("requested", "data_ok", "no_signal", "not_listed",
                                "contract_rejection", "fetch_errors", "selected")})
        self._record_event("options_scan", self.last_scan_diagnostics)
        universe = _universe(self.cfg)
        instruments = {item["symbol"]: item for item in universe}
        contracts = _metadata(rows, universe, clock(), {})
        pool = [(candidate, snapshot) for snapshot in found["snapshots"]
                for candidate in snapshot.get("option_candidates", [])]
        pool.sort(key=lambda item: item[0].get("rank", math.inf))
        refreshed: Dict[str, Any] = {}
        placed: List[Dict[str, Any]] = []
        for original, snapshot in pool:
            current = clock()
            if not self._clock().can_enter(current):
                break
            symbol = str(original.get("option_underlying", "")).upper()
            if symbol not in instruments:
                raise OptionsIntradayPolicyError("scanner returned an unconfigured underlying")
            option = original.get("option_legs") or {}
            leg = (option.get("legs") or [{}])[0]
            key = self._candidate_key(option, leg)
            intent = (self._state.get("intents") or {}).get(key)
            if intent and intent.get("status") != "rejected":
                if intent.get("status") in {"pending", "uncertain"}:
                    raise OptionsIntradayUnresolved((key,), "entry intent already unresolved")
                continue
            if symbol not in refreshed:
                # Manage after the broad pass and between per-symbol refreshes.
                self.tick(now=current)
                scoped = deepcopy(self.cfg)
                scoped["options_intraday"]["scan_underlyings"] = [symbol]
                refreshed[symbol] = self._read_only_scan(scoped, clock, rows)
            fresh_candidates = [(candidate, fresh) for fresh in refreshed[symbol]["snapshots"]
                                for candidate in fresh.get("option_candidates", [])]
            matched = next(((candidate, fresh) for candidate, fresh in fresh_candidates
                            if self._candidate_key(candidate["option_legs"],
                                                   candidate["option_legs"]["legs"][0]) == key
                            and candidate.get("strategy") == original.get("strategy")
                            and candidate.get("action") == original.get("action")), None)
            if matched is None:
                self.last_scan_diagnostics["entry_rejections"].append(
                    {"underlying": symbol, "reason": "setup_or_contract_no_longer_valid"})
                continue
            candidate, fresh = matched
            exact = candidate["option_legs"]
            selected_leg = exact["legs"][0]
            identity = (symbol, _date_for_expiry(exact["expiry"]),
                        float(selected_leg["strike"]), selected_leg["kind"])
            metadata = contracts.get(identity)
            if metadata is None:
                raise OptionsIntradayPolicyError("entry has no exact current broker listing")
            validation_cfg = _symbol_cfg(self.cfg, instruments[symbol], contracts, clock())
            validation_cfg["options_intraday"].update(
                lot_size=metadata["lot_size"],
                # Exact current listing, not an inherited NIFTY strike grid.
                exact_contract=metadata)
            envelope = dict(fresh, option_candidates=[candidate])
            try:
                placed.extend(self.execute_plan(envelope, broker, now=clock(),
                                                _validation_cfg=validation_cfg))
            except OptionsIntradayPolicyError as exc:
                self.last_scan_diagnostics["entry_rejections"].append(
                    {"underlying": symbol, "reason": str(exc)})
        self.last_scan_diagnostics["submitted"] = len(placed)
        return placed

    def execute_plan(self, plan: Optional[Dict[str, Any]], opt_broker: Any = None,
                     now: Optional[datetime] = None,
                     _validation_cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Submit only valid long option candidates from today's plan.

        ``action`` on a plan is the signal (BUY or SELL); the routed order is
        always a BUY of the one selected CE/PE leg and is represented by
        ``order_action`` in the adapter payload.
        """
        current = _now_ist(now)
        self._roll_daily_state(current.date().isoformat())
        broker = self._require_broker(opt_broker)
        self._validate_plan_window(plan or {}, current)
        self._last_plan = deepcopy(plan)
        self._reconcile(broker)
        placed: List[Dict[str, Any]] = []
        candidates = plan.get("option_candidates", []) or []
        if not isinstance(candidates, list):
            raise OptionsIntradayPolicyError("option_candidates is not a list")

        for candidate_raw in candidates:
            candidate = dict(candidate_raw or {})
            if candidate.get("asset") != "options":
                raise OptionsIntradayPolicyError("options candidate asset is not options")
            if not self._candidate_age_ok(candidate, current):
                raise OptionsIntradayPolicyError("options signal is missing, future-dated, or stale")
            valid, reason = validate_option_candidate(
                candidate, plan, _validation_cfg or self.cfg, current)
            if not valid:
                raise OptionsIntradayPolicyError(f"invalid option candidate: {reason}")
            option = deepcopy(candidate.get("option_legs") or {})
            legs = option.get("legs") or []
            leg = legs[0]
            key = self._candidate_key(option, leg)
            existing_intent = (self._state.get("intents", {}) or {}).get(key)
            if existing_intent and existing_intent.get("status") != "rejected":
                if existing_intent.get("status") in {"pending", "uncertain"}:
                    raise OptionsIntradayUnresolved((key, existing_intent.get("order_id")),
                                                    "entry intent already unresolved")
                continue
            trading_symbol, contract = self._validate_instrument(
                candidate, option, leg, broker)
            payload = self._adapter_payload(
                candidate, option, contract, trading_symbol,
                (plan.get("option_chain", {}) or {}).get("quote_timestamp"))
            lots, risk_amount, risk_reason = self._risk_preflight(
                candidate, option, leg, broker)
            if lots <= 0:
                self._record_event("skip", {"asset": "options", "reason": risk_reason,
                                             "execution_class": self.execution_class})
                continue
            ask_premium = _finite_number(leg.get("ask"), 0.0)
            premium_exposure = self._full_debit(
                ask_premium, lots, int(option.get("lot_size", 0) or 0))
            intent = self._intent(key, candidate, payload, lots, risk_amount,
                                  premium_exposure)
            self._state.setdefault("intents", {})[key] = intent
            self._persist()  # intent precedes the remote request
            try:
                order = broker.open_spread(payload, lots=lots,
                                           reason="options-intraday-entry")
            except Exception as exc:
                intent["status"] = "uncertain"
                self._persist()
                try:
                    from src.risk_manager import set_broker_uncertain
                    set_broker_uncertain("options entry request state is unknown")
                except Exception:
                    pass
                raise OptionsIntradayUnresolved((key,), "entry request state unknown") from exc
            status = _status(order)
            if status not in _FILLED:
                checked = self._query_order(broker, order)
                if _status(checked) in _FILLED:
                    order = checked
                    status = _status(checked)
                else:
                    intent["order_id"] = _identifier(order, "order_id", "id")
                    intent["status"] = "uncertain" if status not in _REJECTED else "rejected"
                    self._persist()
                    if status in _PENDING or status not in _REJECTED:
                        raise OptionsIntradayUnresolved(
                            (key, intent.get("order_id")), "entry order is pending or uncertain")
                    continue

            order_id = _identifier(order, "order_id", "id")
            position_id = _identifier(order, "symbol") or f"OPT_{key.replace('|', '_')}"
            filled_premium = _finite_number(
                order.get("price") if isinstance(order, dict) else getattr(order, "price", 0.0),
                _finite_number(leg.get("premium"), 0.0))
            if filled_premium <= 0:
                filled_premium = _finite_number(leg.get("ask"), 0.0)
            stop_fraction = max(0.0, _finite_number(
                self.mode_cfg.get("stop_premium_fraction"), 0.20))
            target_fraction = max(0.0, _finite_number(
                self.mode_cfg.get("target_premium_fraction"), 0.30))
            profit_step_fraction = max(0.0, _finite_number(
                self.mode_cfg.get("profit_lock_step_fraction"), 0.10))
            position = {
                "position_id": position_id,
                "status": "open",
                "asset": "options",
                "execution_class": self.execution_class,
                "signal_action": str(candidate.get("action", "")).upper(),
                "direction": candidate.get("direction"),
                "signal_underlying": candidate.get("signal_underlying", ""),
                "option_underlying": option.get("underlying", ""),
                "expiry": option.get("expiry", ""),
                "strike": _finite_number(leg.get("strike"), 0.0),
                "kind": str(leg.get("kind", "")).upper(),
                "trading_symbol": trading_symbol,
                "remote_symbols": [trading_symbol],
                "validated_contract": contract,
                "lots": lots,
                "lot_size": int(option.get("lot_size", 0) or 0),
                "entry_premium": filled_premium,
                "ask_premium": ask_premium,
                "premium_exposure": premium_exposure,
                "last_bid": _finite_number(leg.get("bid"), 0.0),
                "stop_premium": round(filled_premium * (1.0 - stop_fraction), 2),
                "target_premium": round(filled_premium * (1.0 + target_fraction), 2),
                "initial_stop_premium": round(filled_premium * (1.0 - stop_fraction), 2),
                "final_target_premium": round(filled_premium * (1.0 + target_fraction), 2),
                "profit_lock_step_fraction": profit_step_fraction,
                "profit_lock_stage": 0,
                "exit_policy": "staged_bid_ladder",
                "risk_amount": risk_amount,
                "entry_order_id": order_id,
                "opened_at": _iso(datetime.now(timezone.utc)),
                "quote_timestamp": payload.get("quote_timestamp"),
                "metadata": deepcopy(candidate.get("metadata", {}) or {}),
            }
            self._state.setdefault("positions", {})[position_id] = position
            intent.update({"status": "open", "order_id": order_id,
                           "position_id": position_id})
            self._state["daily_entries"] = int(
                _finite_number(self._state.get("daily_entries"), 0.0)) + 1
            self._persist()
            try:
                from src.risk_manager import add_position
                add_position(position_id, lots, filled_premium, risk_amount)
            except Exception as exc:
                raise OptionsIntradayUnresolved((position_id,),
                                                "production risk registration failed") from exc
            record = {
                "position_id": position_id,
                "symbol": position_id,
                "asset": "options",
                "execution_class": self.execution_class,
                "order_action": "BUY",
                "signal_action": position["signal_action"],
                "direction": position["direction"],
                "signal_underlying": position["signal_underlying"],
                "option_underlying": position["option_underlying"],
                "trading_symbol": trading_symbol,
                "expiry": position["expiry"],
                "strike": position["strike"],
                "kind": position["kind"],
                "lots": lots,
                "lot_size": position["lot_size"],
                "entry_premium": filled_premium,
                "risk_amount": risk_amount,
                "order_id": order_id,
                "quote_timestamp": position["quote_timestamp"],
                "historical_options_pnl": NOT_READY_FOR_OPTIONS_PNL,
            }
            placed.append(record)
            self._record_event("order", record)
        return placed

    # ------------------------------------------------------------------
    # Quote and completed-bar access for exits.  A close is never based on a
    # stored LTP, BS value, mid, or request timestamp: only a fresh exchange
    # chain bid is executable for this long position.
    # ------------------------------------------------------------------

    def _chain_from_provider(self, provider: Callable[..., Any], underlying: str) -> Any:
        try:
            return provider(underlying, use_cache=False)
        except TypeError:
            try:
                return provider(underlying)
            except TypeError:
                return provider()

    def _fresh_chain(self, broker: Any, now: datetime,
                     underlying: str = "NIFTY") -> Optional[Dict[str, Any]]:
        def admissible(chain):
            return (isinstance(chain, dict) and
                    str(chain.get("symbol", "")).upper() == underlying.upper() and
                    str(chain.get("exchange", "NSE")).upper() == "NSE" and
                    self._chain_timestamp_fresh(chain, now))
        providers: List[Callable[..., Any]] = []
        if callable(self._chain_provider):
            providers.append(self._chain_provider)
        for source, names in (
            (self.trader, ("fetch_options_chain", "get_options_chain", "get_option_chain")),
            (broker, ("fetch_options_chain", "get_options_chain", "get_option_chain")),
        ):
            for name in names:
                callback = getattr(source, name, None)
                if callable(callback):
                    providers.append(callback)
        for provider in providers:
            try:
                chain = self._chain_from_provider(provider, underlying)
            except Exception:
                continue
            if admissible(chain):
                return chain

        # The generator's private snapshot can serve an immediate management
        # tick while it is still exchange-fresh.  It is not an LTP/BS fallback.
        snapshot = (self._last_plan or {}).get("option_chain")
        if self.trading_mode == _COMBINED and admissible(snapshot):
            return snapshot
        try:
            from src.options_chain import fetch_chain
            chain = fetch_chain(underlying, use_cache=False,
                                diagnostic_label="options_intraday_runtime")
            if admissible(chain):
                return chain
        except Exception:
            pass
        return None

    def _chain_timestamp_fresh(self, chain: Dict[str, Any], now: datetime) -> bool:
        retrieval = {**((chain.get("diagnostics") or {}).get("retrieval") or {}),
                     **(chain.get("retrieval") or {})}
        if retrieval.get("served_from_cache") or retrieval.get("success") is False:
            return False
        timestamp = parse_exchange_timestamp(chain.get("quote_timestamp"))
        if timestamp is None:
            return False
        age = now.astimezone(timezone.utc) - timestamp
        ttl = _finite_number(self.mode_cfg.get("chain_max_age_minutes"), 15.0)
        return timedelta(0) <= age <= timedelta(minutes=ttl)

    def _chain_quote(self, chain: Dict[str, Any], position: Dict[str, Any],
                     now: datetime) -> Optional[float]:
        if not self._chain_timestamp_fresh(chain, now):
            return None
        if str(chain.get("symbol", "")).upper() != str(position.get("option_underlying", "")).upper():
            return None
        expiry = str(position.get("expiry", ""))
        strike = _finite_number(position.get("strike"), -1.0)
        kind = str(position.get("kind", "")).upper()
        rows = (chain.get("by_expiry", {}) or {}).get(expiry, []) or []
        if not rows:
            # A provider may normalize expiry text differently while retaining
            # the exchange's exact date; do not use the first unrelated row.
            for key, values in (chain.get("by_expiry", {}) or {}).items():
                if _same_expiry(key, expiry):
                    rows = values or []
                    break
        row = next((item for item in rows
                    if abs(_finite_number(item.get("strike"), -1.0) - strike) <= 1e-8), None)
        if not isinstance(row, dict) or not isinstance(row.get(kind), dict):
            return None
        quote = row[kind]
        for item in (row, quote):
            if str(item.get("exchange", "NSE")).upper() != "NSE":
                return None
            if (item.get("underlying_symbol") and
                    str(item["underlying_symbol"]).upper() != str(position["option_underlying"]).upper()):
                return None
            if item.get("expiry") and not _same_expiry(item["expiry"], expiry):
                return None
        if quote.get("option_type") and quote["option_type"] != kind:
            return None
        if quote.get("strike") is not None and _finite(quote["strike"]) != strike:
            return None
        selected_time = (quote.get("quote_timestamp") or quote.get("row_timestamp") or
                         row.get("quote_timestamp") or row.get("row_timestamp"))
        if selected_time and not self._chain_timestamp_fresh(
                {"quote_timestamp": selected_time}, now):
            return None
        bid = _finite(quote.get("bidPrice", quote.get("bid",
                                  quote.get("buyPrice1"))))
        if bid is None or bid <= 0:
            return None
        return bid

    def _completed_bar_trigger(self, position: Dict[str, Any], now: datetime,
                               chain: Optional[Dict[str, Any]]) -> Optional[str]:
        basis = str(self.mode_cfg.get("stop_target_basis", "premium")).lower()
        if basis not in {"underlying", "underlying_bars", "bars"}:
            return None
        provider = self._bar_provider
        if not callable(provider):
            provider = getattr(self.trader, "get_completed_bars", None)
        if not callable(provider):
            return None
        try:
            bars = provider(position.get("option_underlying"), now=now)
        except TypeError:
            try:
                bars = provider(position.get("option_underlying"))
            except Exception:
                return None
        except Exception:
            return None
        if isinstance(bars, dict):
            bars = bars.get("bars") or bars.get("completed") or []
        if not isinstance(bars, (list, tuple)) or not bars:
            return None
        bar = bars[-1]
        if not isinstance(bar, dict) or bar.get("completed") is False:
            return None
        spot = _finite_number((chain or {}).get("underlying"), 0.0)
        high = _finite_number(bar.get("high"), 0.0)
        low = _finite_number(bar.get("low"), 0.0)
        if spot <= 0 or high <= 0 or low <= 0:
            return None
        bullish = str(position.get("direction", "")).lower() == "bullish"
        # Stops are intentionally checked first when a wide/ambiguous bar can
        # satisfy both predicates.
        if (bullish and spot <= low) or (not bullish and spot >= high):
            return "underlying-stop"
        if (bullish and spot >= high) or (not bullish and spot <= low):
            return "underlying-target"
        return None

    def _update_profit_ladder(self, position: Dict[str, Any], bid: float) -> Optional[str]:
        """Advance the long-option bid ladder or return an exit reason.

        This is a full-position ratchet: +10% and +20% only move the stop;
        they do not create partial exits.  The executable bid is the only
        trigger, and a jump over a rung advances directly to the highest rung
        reached.
        """
        entry = _finite_number(position.get("entry_premium"), 0.0)
        if entry <= 0 or bid <= 0:
            return None
        step = max(0.0, _finite_number(
            position.get("profit_lock_step_fraction"),
            _finite_number(self.mode_cfg.get("profit_lock_step_fraction"), 0.10),
        ))
        final_target = max(0.0, _finite_number(
            position.get("final_target_premium"),
            entry * (1.0 + _finite_number(
                self.mode_cfg.get("target_premium_fraction"), 0.30)),
        ))
        if step <= 0 or final_target <= entry:
            return None

        initial_stop = _finite_number(
            position.get("initial_stop_premium"),
            entry * (1.0 - _finite_number(
                self.mode_cfg.get("stop_premium_fraction"), 0.20)),
        )
        stage = max(0, int(_finite_number(position.get("profit_lock_stage"), 0.0)))
        stop_level = _finite_number(position.get("stop_premium"), initial_stop)
        if bid < stop_level or math.isclose(
            bid, stop_level, rel_tol=0.0, abs_tol=1e-9
        ):
            position["last_bid"] = bid
            return "stop-loss"
        if bid > final_target or math.isclose(
            bid, final_target, rel_tol=0.0, abs_tol=1e-9
        ):
            position["profit_lock_stage"] = max(stage, int(round(
                (final_target / entry - 1.0) / step)))
            position["last_bid"] = bid
            return "target"

        reached_stage = stage
        while bid > entry * (1.0 + step * (reached_stage + 1)) or math.isclose(
            bid,
            entry * (1.0 + step * (reached_stage + 1)),
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            reached_stage += 1
        if reached_stage > stage:
            position["profit_lock_stage"] = reached_stage
            locked_stop = entry * (1.0 + step * reached_stage)
            next_target = min(
                final_target,
                entry * (1.0 + step * (reached_stage + 1)),
            )
            position["stop_premium"] = round(locked_stop, 2)
            position["target_premium"] = round(next_target, 2)
        position["last_bid"] = bid
        return None

    # ------------------------------------------------------------------
    # Exit lifecycle and production risk accounting.
    # ------------------------------------------------------------------

    def _close_confirmed(self, broker: Any, position_id: str,
                         result: Any) -> Tuple[bool, List[Any]]:
        if not isinstance(result, dict) or not result:
            return False, []
        status = str(result.get("status", "")).upper()
        if status in _PENDING or status in {"ACCEPTED", "SUBMITTED"}:
            close_order_id = _identifier(result, "order_id", "id")
            getter = getattr(broker, "get_order_status", None)
            if not close_order_id or not callable(getter):
                return False, []
            try:
                status = _status(getter(close_order_id))
            except Exception:
                return False, []
            if status not in _FILLED:
                return False, []
        # A spread adapter's non-empty close result is only accepted after a
        # fresh mirror shows neither the umbrella nor its exact remote leg.
        try:
            remote = self._remote_positions(broker)
        except OptionsIntradayUnresolved:
            raise
        remote_keys = {_normalize_symbol(_position_symbol(item))
                       for item in remote if _position_quantity(item) > 0}
        position = (self._state.get("positions", {}) or {}).get(position_id, {})
        owned = {_normalize_symbol(position_id)}
        if isinstance(position, dict):
            owned.update(_normalize_symbol(value) for value in
                         [position.get("trading_symbol"),
                          *(position.get("remote_symbols", []) or [])]
                         if value)
        return not (owned & remote_keys), remote

    def _close_one(self, broker: Any, position_id: str, position: Dict[str, Any],
                   bid: float, reason: str) -> Dict[str, Any]:
        close = getattr(broker, "close_spread", None)
        if not callable(close):
            raise OptionsIntradayUnresolved((position_id,), "options close API unavailable")
        try:
            result = close(position_id, bid, reason)
        except Exception as exc:
            raise OptionsIntradayUnresolved((position_id,), "options close request failed") from exc
        confirmed, _ = self._close_confirmed(broker, position_id, result)
        if not confirmed:
            raise OptionsIntradayUnresolved((position_id,), "options close is not confirmed flat")

        lots = int(_finite_number(position.get("lots"), 0.0))
        lot_size = int(_finite_number(position.get("lot_size"), 0.0))
        entry = _finite_number(position.get("entry_premium"), 0.0)
        result_dict = dict(result)
        pnl_value = _finite(result_dict.get("pnl"))
        if pnl_value is None:
            pnl_value = ((bid - entry) * lots * lot_size -
                         self._brokerage_per_order() * 2.0)
        closed_at = result_dict.get("closed_at") or _iso(datetime.now(timezone.utc))
        close_id = "options:" + str(position_id) + "|" + str(closed_at)
        record = {
            "position_id": position_id,
            "symbol": position_id,
            "asset": "options",
            "execution_class": self.execution_class,
            "reason": reason,
            "signal_action": position.get("signal_action"),
            "direction": position.get("direction"),
            "signal_underlying": position.get("signal_underlying"),
            "option_underlying": position.get("option_underlying"),
            "trading_symbol": position.get("trading_symbol"),
            "expiry": position.get("expiry"),
            "strike": position.get("strike"),
            "kind": position.get("kind"),
            "lots": lots,
            "lot_size": lot_size,
            "entry_premium": entry,
            "exit_bid": bid,
            "pnl": round(float(pnl_value), 2),
            "closed_at": closed_at,
            "close_id": close_id,
            "historical_options_pnl": NOT_READY_FOR_OPTIONS_PNL,
        }
        try:
            from src.risk_manager import close_position, get_risk_state, update_pnl
            close_position(position_id)
            update_pnl(float(record["pnl"]))
            risk_state = get_risk_state()
            seen = getattr(risk_state, "_options_intraday_closed_ids", set())
            if not isinstance(seen, set):
                seen = set(seen or [])
            seen.add(str(position_id) + "|" + str(record["closed_at"]))
            setattr(risk_state, "_options_intraday_closed_ids", seen)
        except Exception as exc:
            # The remote book is already flat, but losing accounting would
            # make a restart unsafe; keep the private record for reconciliation.
            raise OptionsIntradayUnresolved((position_id,),
                                            "production risk close accounting failed") from exc
        self._state.setdefault("positions", {}).pop(position_id, None)
        for key, intent in (self._state.get("intents", {}) or {}).items():
            if isinstance(intent, dict) and intent.get("position_id") == position_id:
                intent["status"] = "closed"
                intent["closed_at"] = record["closed_at"]
        self._state.setdefault("closed", []).append({
            "position_id": position_id,
            "pnl": record["pnl"],
            "closed_at": record["closed_at"],
            "close_id": close_id,
            "reason": reason,
            "risk_accounted": True,
        })
        self._persist()
        self._record_event("close", record)
        return record

    def _tick(self, now: Optional[datetime], force_flat: bool = False) -> List[Dict[str, Any]]:
        current = _now_ist(now)
        self._roll_daily_state(current.date().isoformat())
        broker = self._require_broker()
        self._reconcile(broker)
        positions = deepcopy(self._state.get("positions", {}) or {})
        self._state["mark_cycle_ms"] = int(current.timestamp() * 1000)
        if not positions:
            self._persist()
            return []
        clock = self._clock()
        hard_flat = force_flat or (clock.valid and clock.is_hard_flat(current))
        chains: Dict[str, Any] = {}
        unresolved: List[str] = []
        closed: List[Dict[str, Any]] = []
        state_changed = False
        for position_id, position in positions.items():
            if not isinstance(position, dict):
                unresolved.append(str(position_id))
                continue
            underlying = str(position.get("option_underlying", "")).upper()
            if underlying not in chains:
                chains[underlying] = self._fresh_chain(broker, current, underlying)
            chain = chains[underlying]
            bid = self._chain_quote(chain, position, current) if chain else None
            reason = ""
            if hard_flat:
                reason = "eod-squareoff"
            elif bid is not None:
                position["last_bid"] = bid
                position["last_bid_at_ms"] = self._state["mark_cycle_ms"]
                position["mark_status"] = "fresh"
                position["mark_unresolved"] = False
                bar_reason = self._completed_bar_trigger(position, current, chain)
                if bar_reason == "underlying-stop":
                    reason = "stop-loss"
                elif bar_reason == "underlying-target":
                    reason = "target"
                else:
                    reason = self._update_profit_ladder(position, bid) or ""
                if not reason:
                    # Persist ratchet state even when this tick does not close.
                    self._state.setdefault("positions", {}).setdefault(
                        position_id, {}
                    ).update({
                        key: position[key]
                        for key in (
                            "last_bid", "last_bid_at_ms", "mark_status",
                            "mark_unresolved", "stop_premium", "target_premium",
                            "profit_lock_stage", "exit_policy",
                        )
                        if key in position
                    })
                    state_changed = True
            if not reason:
                if bid is None and not hard_flat:
                    stored = self._state.setdefault("positions", {}).setdefault(position_id, {})
                    stored["mark_status"] = "unresolved"
                    stored["mark_unresolved"] = True
                    stored.pop("last_bid_at_ms", None)
                    state_changed = True
                continue
            if bid is None or bid <= 0:
                if hard_flat:
                    unresolved.append(str(position_id))
                continue
            self._state.setdefault("positions", {}).setdefault(position_id, {})["last_bid"] = bid
            try:
                closed.append(self._close_one(broker, str(position_id), position, bid, reason))
            except OptionsIntradayUnresolved as exc:
                unresolved.extend(exc.identifiers or (str(position_id),))
        if closed or state_changed:
            self._persist()
        if unresolved:
            raise OptionsIntradayUnresolved(sorted(set(unresolved)),
                                            "one or more option exits are unresolved")
        return closed

    def tick(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Manage owned contracts and attempt hard-flat at the configured clock."""
        return self._tick(now, force_flat=False)

    def finalize(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Attempt to flatten every owned option, raising if any stays open."""
        if not (self._state.get("positions", {}) or {}):
            # Still reconcile an attached book so unrelated remote holdings are
            # never silently ignored at shutdown.
            broker = self._broker or getattr(self.trader, "opt_broker", None)
            if broker is not None:
                self._reconcile(self._require_broker(broker))
            return []
        return self._tick(now, force_flat=True)

    def run(self, interval_seconds: float = 60.0,
            max_seconds: Optional[float] = None) -> None:
        """Tick until the caller's bounded runtime expires, then finalize."""
        started = time.monotonic()
        interval = max(0.01, float(interval_seconds))
        try:
            while True:
                self.tick()
                if max_seconds is not None and time.monotonic() - started >= float(max_seconds):
                    self.finalize()
                    return
                time.sleep(interval)
        except KeyboardInterrupt:
            # The finalization error is intentionally allowed to surface if
            # remote state cannot be proved flat.
            self.finalize()
            raise


__all__ = [
    "OptionsIntradayError", "OptionsIntradayPolicyError",
    "OptionsIntradayRuntime", "OptionsIntradayUnresolved",
]
