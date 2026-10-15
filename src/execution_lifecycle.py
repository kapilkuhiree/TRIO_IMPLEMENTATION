"""
TRIO — Execution lifecycle (single shared path for every entry and exit)
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

Every trade — `python main.py paper`, premarket plan, session runner, live
rescan, local paper or MegaBull — MUST go through:

    needs   = reconcile_broker(trader)            # positions + PENDING scan
    ok, why = preflight(trader, candidate, proposed)  # halt/max/sector/margin/dup
    rec     = submit_candidate(trader, candidate, reason="premarket-plan"|"scan")
    record_confirmed_close(trader, symbol, record)    # pnl/consec/position/halt/log/alert once

No caller is allowed to `place_order()` + `_log_event()` + `add_position()`
by hand anymore — that is what produced the divergent scan_once/execute_plan
behaviour. The lifecycle owns refresh, risk refresh, halt, max, sector,
margin, dedup, submit, confirm, register, audit and alert in one place.

Shadow option spreads are tagged venue="local_shadow" and are EXCLUDED from
real account risk/equity by construction.

DISCLAIMER: Educational purposes only. Not financial advice.
"""
from __future__ import annotations

import math
import time
from collections.abc import Mapping
from typing import Any, Dict, List, Optional, Tuple

from src.utils import get_logger, load_config

logger = get_logger("execution_lifecycle")

TERMINAL_SUBMIT = ("FILLED", "REJECTED")
_PENDING = ("PENDING", "PLACED", "ACCEPTED")

# Module-level state for reconciliation conservatism (Issue #2)
# Track consecutive successful refreshes per trader instance
_refresh_state: Dict[int, Dict[str, Any]] = {}

# Idempotency cache for record_confirmed_close (Issue #3)
# Map of (trader_id, symbol, order_id/close_key) -> timestamp
_processed_closes: Dict[Tuple[int, str, str], float] = {}
_IDEMPOTENCY_TTL = 86400  # 24 hours


def validate_available_balance(balance: Any) -> Tuple[Optional[float], str]:
    """Validate an adapter balance without exposing its payload.

    Returns ``(available, "")`` for a finite numeric value, including a
    numeric string. Invalid schemas and values return ``margin-check-failed``;
    finite non-positive values return ``margin-exhausted``.
    """
    if not isinstance(balance, Mapping) or "available" not in balance:
        return None, "margin-check-failed"
    raw_available = balance["available"]
    if isinstance(raw_available, bool):
        return None, "margin-check-failed"
    try:
        available = float(raw_available)
    except (TypeError, ValueError, OverflowError):
        return None, "margin-check-failed"
    if not math.isfinite(available):
        return None, "margin-check-failed"
    if available <= 0:
        return available, "margin-exhausted"
    return available, ""


def _get_trader_id(trader) -> int:
    """Get a stable identifier for the trader instance."""
    return id(trader)


def _make_close_key(record: Dict[str, Any]) -> str:
    """Create a unique key for a close record."""
    # Use order_id if available, otherwise use a composite key
    for k in ("order_id", "trade_id", "close_id"):
        v = record.get(k)
        if v:
            return f"{k}:{v}"
    # Fallback composite key
    return ("cmp:%s|%s|%s|%s|%s" % (
        record.get("symbol", ""),
        record.get("side", ""),
        record.get("entry", ""),
        record.get("exit", ""),
        record.get("quantity", "")))


# ---------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------

def _equity_position_partition(broker: Any,
                               positions: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Return only positions proven to belong to the equity book.

    MegaBull exposes one account for equity and strict options.  Its
    cache-only partition helper is the boundary that prevents an option leg
    from being rebuilt as a stock position.  Brokers without that optional
    helper retain the existing equity-only behavior.  Unknown MegaBull
    entries are excluded and reported as unresolved rather than guessed into
    the equity ledger.
    """
    provider = str(getattr(broker, "provider_name", "")).strip().lower()
    partition = getattr(broker, "partition_positions", None)
    if provider == "megabull_options" and not callable(partition):
        return {}, list((positions or {}).keys())
    if not callable(partition):
        classifier = getattr(broker, "classify_position", None)
        if callable(classifier):
            equity: Dict[str, Any] = {}
            unknown: List[str] = []
            for key, value in (positions or {}).items():
                try:
                    asset = str(classifier(key, value)).lower()
                except Exception:
                    asset = "unknown"
                if asset == "equity":
                    equity[key] = value
                elif asset == "unknown":
                    unknown.append(str(key))
            return equity, unknown
        return dict(positions or {}), []
    try:
        buckets = partition(positions)
    except Exception as exc:
        logger.warning("reconcile: instrument partition failed: %s", exc)
        return {}, list((positions or {}).keys())
    if not isinstance(buckets, Mapping):
        return {}, list((positions or {}).keys())
    equity = buckets.get("equity", {})
    unknown = buckets.get("unknown", {})
    return (dict(equity) if isinstance(equity, Mapping) else {},
            list(unknown.keys()) if isinstance(unknown, Mapping) else [])


def _equity_closed_trades(broker: Any,
                          closed_trades: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Filter option/unknown close records from the equity rebuild."""
    partition = getattr(broker, "partition_closed_trades", None)
    if not callable(partition):
        if str(getattr(broker, "provider_name", "")).lower() == "megabull_options":
            return [], [str(item.get("symbol", "")) for item in closed_trades]
        classifier = getattr(broker, "classify_instrument", None)
        if callable(classifier):
            equity: List[Dict[str, Any]] = []
            unknown: List[str] = []
            for item in closed_trades:
                if not isinstance(item, Mapping):
                    continue
                symbol = (item.get("trading_symbol") or item.get("tradingSymbol") or
                          item.get("symbol") or "")
                try:
                    asset = str(classifier(symbol, item)).lower()
                except Exception:
                    asset = "unknown"
                if asset == "equity":
                    equity.append(dict(item))
                elif asset == "unknown":
                    unknown.append(str(symbol))
            return equity, unknown
        return list(closed_trades or []), []
    try:
        buckets = partition(closed_trades)
    except Exception as exc:
        logger.warning("reconcile: close partition failed: %s", exc)
        return [], [str(item.get("symbol", "")) for item in closed_trades]
    if not isinstance(buckets, Mapping):
        return [], [str(item.get("symbol", "")) for item in closed_trades]
    equity = buckets.get("equity", [])
    unknown = buckets.get("unknown", [])
    return (list(equity) if isinstance(equity, (list, tuple)) else [],
            [str(item.get("symbol", "")) for item in unknown if isinstance(item, Mapping)])


def _strict_options_risk_positions(trader: Any) -> Dict[str, Dict[str, Any]]:
    """Read the strict runtime's open option positions, if attached.

    The parent integration owns policy and attachment.  This lifecycle only
    consumes a callable export when present, with the legacy mapping retained
    as a compatibility fallback for existing option-only callers.
    """
    owners = [trader, getattr(trader, "_options_runtime", None)]
    found: Any = None
    for owner in owners:
        if owner is None:
            continue
        names = ("_strict_options_risk_positions", "strict_options_risk_positions")
        if owner is getattr(trader, "_options_runtime", None):
            names = names + ("risk_positions",)
        for name in names:
            export = getattr(owner, name, None)
            if callable(export):
                try:
                    found = export()
                except Exception as exc:
                    logger.warning("reconcile: strict option risk export failed: %s", exc)
                    found = None
                break
        if found is not None:
            break
    if found is None:
        found = getattr(trader, "_options_risk_positions", {}) or {}

    normalized: Dict[str, Dict[str, Any]] = {}
    if isinstance(found, Mapping):
        items = found.items()
    elif isinstance(found, (list, tuple, set)):
        items = []
        for value in found:
            if isinstance(value, Mapping):
                key = value.get("position_id") or value.get("symbol") or value.get("trading_symbol")
                if key:
                    items.append((key, value))
    else:
        items = []
    for key, value in items:
        if not isinstance(value, Mapping):
            continue
        info = dict(value)
        # The callable is the strict-options boundary; asset qualification is
        # repeated here so a stale legacy export cannot become an equity row.
        info["asset"] = "options"
        info.setdefault("position_id", str(key))
        info.setdefault("symbol", str(key))
        normalized[str(key)] = info
    return normalized


def _strict_options_closed_trades(trader: Any) -> List[Dict[str, Any]]:
    """Read private strict-runtime close records for shared P&L rebuilds."""
    owners = [trader, getattr(trader, "_options_runtime", None)]
    for owner in owners:
        if owner is None:
            continue
        for name in ("_strict_options_closed_trades", "strict_options_closed_trades"):
            export = getattr(owner, name, None)
            if callable(export):
                try:
                    value = export()
                except Exception as exc:
                    logger.warning("reconcile: strict option close export failed: %s", exc)
                    return []
                return [dict(item) for item in value
                        if isinstance(item, Mapping)] if isinstance(value, (list, tuple)) else []
    return []

def reconcile_broker(trader) -> Dict[str, Any]:
    """Refresh the mirror, scan for PENDING/UNKNOWN rows, rebuild risk.

    Requires consecutive successful refreshes (1 for local paper, 2 for remote)
    and does not clear uncertainty on empty responses if there are pending orders.
    Returns {"pending": [...], "uncertain": bool}.
    """
    out: Dict[str, Any] = {"pending": [], "uncertain": False}
    broker = getattr(trader, "broker", None)
    tid = _get_trader_id(trader)
    # Detect paper trading mode.
    # The broker's provider_name may be "megabull" even in paper mode,
    # so we check: if the trader is a PaperTrader instance, it's paper trading.
    is_paper = (getattr(broker, "provider_name", "paper") == "paper"
                or type(trader).__name__ == "PaperTrader")
    required_consecutive = 1 if is_paper else 2
    # Start at zero: a remote broker needs two independently successful
    # refreshes before an uncertainty halt may be cleared.
    state = _refresh_state.setdefault(tid, {"consecutive_successes": 0, "last_empty_at": 0.0})

    if broker is None:
        return out
    refresh_ok = True
    try:
        refresh_result = broker.refresh_positions()
        # Remote brokers must explicitly report a healthy refresh.  Older
        # local test doubles do not implement refresh_positions at all, which
        # is safe for the in-memory paper broker but not for a remote book.
        if refresh_result is False:
            refresh_ok = False
        if not is_paper and refresh_result is not True:
            refresh_ok = False
        if hasattr(broker, "refresh_orders") and broker.refresh_orders() is not True:
            refresh_ok = False
    except AttributeError:
        if not is_paper:
            refresh_ok = False
            state["consecutive_successes"] = 0
            try:
                from src.risk_manager import set_broker_uncertain
                set_broker_uncertain("remote broker has no position refresh")
                out["uncertain"] = True
            except Exception:
                pass
            return out
    except Exception as exc:
        logger.warning("reconcile: refresh failed: %s", exc)
        refresh_ok = False
        state["consecutive_successes"] = 0
        try:
            from src.risk_manager import set_broker_uncertain
            set_broker_uncertain(f"refresh failed: {exc}")
            out["uncertain"] = True
        except Exception:
            pass
        return out

    if refresh_ok:
        state["consecutive_successes"] += 1
    else:
        state["consecutive_successes"] = 0
        out["uncertain"] = True

    try:
        positions_dict = getattr(broker, "positions", {}) or {}
        pending = [s for s, p in positions_dict.items()
                   if str(getattr(p, "status", "") or "").upper() in _PENDING
                   or str(getattr(p, "status", "") or "").upper().startswith("UNKNOWN")]
    except Exception:
        pending = []

    # Check for pending orders in broker if available
    try:
        if hasattr(broker, "_pending_orders") and broker._pending_orders:
            pending = list(set(pending + list(broker._pending_orders)))
    except Exception:
        pass

    out["pending"] = pending

    try:
        from src.risk_manager import (set_broker_uncertain,
                                      clear_broker_uncertain, rebuild_from_ledger,
                                      get_risk_state)

        # Rebuild only the equity book.  Strict option risk is merged back
        # immediately below from the runtime's callable export.
        equity_positions, unknown_positions = _equity_position_partition(
            broker, positions_dict)
        equity_closed, unknown_closes = _equity_closed_trades(
            broker, list(getattr(broker, "closed_trades", []) or []))
        strict_closed = _strict_options_closed_trades(trader)
        rebuild_closed = equity_closed + strict_closed
        try:
            from datetime import datetime, timezone, timedelta as _td
            IST = timezone(_td(hours=5, minutes=30))
            today = datetime.now(IST).strftime("%Y-%m-%d")
        except Exception:
            today = None
        rebuild_from_ledger(equity_positions,
                            rebuild_closed,
                            today=today)
        rs = get_risk_state()
        for opt_symbol, opt_info in _strict_options_risk_positions(trader).items():
            rs.positions[opt_symbol] = dict(opt_info)
        rs.open_positions = len(rs.positions)
        if unknown_positions or unknown_closes:
            try:
                set_broker_uncertain("unclassified remote instrument(s)")
            except Exception:
                pass
            out["uncertain"] = True
        # Runtime close records are now part of this rebuild.  Mark them as
        # already read back so a runtime reconstruction cannot apply P&L a
        # second time in the same process.
        closed_ids = getattr(rs, "_options_intraday_closed_ids", set())
        if not isinstance(closed_ids, set):
            closed_ids = set(closed_ids or [])
        for record in strict_closed:
            close_id = str(record.get("position_id") or record.get("symbol") or "")
            closed_at = str(record.get("closed_at") or "")
            if close_id and closed_at:
                closed_ids.add(close_id + "|" + closed_at)
        setattr(rs, "_options_intraday_closed_ids", closed_ids)
        try:
            trader._proposed = dict(getattr(rs, "positions", {}) or {})
        except Exception:
            pass

        # Rebuild resets the in-memory RiskState. Apply the broker uncertainty
        # decision *after* rebuilding so a pending/ambiguous remote book cannot
        # accidentally lose its safety halt.
        if (pending or unknown_positions or unknown_closes or
                state["consecutive_successes"] < required_consecutive):
            if unknown_positions or unknown_closes:
                reason = "unclassified remote instrument(s)"
            elif pending:
                reason = f"{len(pending)} pending/unknown position(s)"
            else:
                reason = (f"warming up refresh confidence "
                          f"({state['consecutive_successes']}/{required_consecutive})")
            set_broker_uncertain(reason)
            out["uncertain"] = True
        else:
            clear_broker_uncertain()
    except Exception as exc:
        logger.warning("reconcile: risk rebuild failed: %s", exc)
        out["uncertain"] = True
    return out


def _proposed_view(trader) -> Dict[str, Any]:
    """Current + already-accepted-this-pass portfolio for sector checks."""
    try:
        prop = getattr(trader, "_proposed", None)
        if isinstance(prop, dict) and prop:
            return dict(prop)
    except Exception:
        pass
    # Build from the live broker book (fresh start / first candidate).
    out: Dict[str, Any] = {}
    try:
        for s, p in (getattr(getattr(trader, "broker", None), "positions", {}) or {}).items():
            out[s] = {"size": int(getattr(p, "quantity", 0) or 0),
                      "entry": float(getattr(p, "avg_price", 0.0) or 0.0)}
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# preflight (returns ok, reason)
# ---------------------------------------------------------------------------

def preflight(trader, candidate: Dict[str, Any],
              proposed: Optional[Dict[str, Any]] = None,
              cfg: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """Halt, max, duplicate, sector, margin checks for ONE candidate.

    `cfg` should be the caller's already-loaded config (tests patch the
    caller's load_config binding). Falls back to load_config() when None.
    """
    from src.risk_manager import get_risk_state, would_breach_sector
    cfg = cfg if cfg is not None else load_config()
    if bool(getattr(trader, "options_only", False)):
        return False, "options-only-mode"
    allow_shorts = bool(cfg.get("trading", {}).get("allow_shorts", False))
    max_open = int(cfg.get("risk_management", {}).get("max_open_positions", 5) or 5)

    rs = get_risk_state()
    if getattr(rs, "halt_active", False):
        return False, f"halted: {getattr(rs, 'halt_reason', '')}"

    sym = str(candidate.get("symbol", ""))
    action = str(candidate.get("action", "HOLD"))
    try:
        qty = int(candidate.get("position_size") or 0)
    except Exception:
        qty = 0
    try:
        entry = float(candidate.get("entry_price") or 0.0)
    except Exception:
        entry = 0.0
    if action not in ("BUY", "SELL") or qty <= 0:
        return False, "invalid action/qty"

    broker = getattr(trader, "broker", None)
    npos = len(getattr(broker, "positions", {}) or {})
    if npos >= max_open:
        return False, f"max_open_positions {max_open}"
    if getattr(rs, "open_positions", 0) >= max_open:
        return False, f"max_open_positions(risk) {max_open}"

    held = (getattr(broker, "positions", {}) or {}).get(sym)
    same_side = (held is not None and int(getattr(held, "quantity", 0) or 0) > 0 and (
        (action == "SELL" and getattr(held, "side", "") == "SHORT")
        or (action == "BUY" and getattr(held, "side", "") == "LONG")))
    if same_side:
        return False, "already-holding"
    if action == "SELL" and not allow_shorts:
        held_qty = int(getattr(held, "quantity", 0) or 0) \
            if held is not None and getattr(held, "side", "") == "LONG" else 0
        if held_qty <= 0:
            return False, "no-holdings"

    base = dict(proposed if proposed is not None else _proposed_view(trader))
    try:
        if would_breach_sector(sym, qty, entry, existing=base):
            return False, "sector-exposure"
    except Exception as exc:
        # A risk calculation failure must not become permission to trade.
        logger.error("sector preflight failed for %s: %s", sym, exc)
        return False, "sector-check-failed"

    try:
        # BaseBroker.get_balance() has no ``strict`` keyword.  Keep the
        # preflight portable across the local paper broker and remote
        # adapters; an invalid balance response is still rejected below.
        bal = broker.get_balance() if broker is not None else None
        _, margin_reason = validate_available_balance(bal)
        if margin_reason:
            return False, margin_reason
    except Exception:
        # Unknown available margin is unsafe; fail closed rather than submit.
        # Adapter exception text may contain a response body or credentials.
        logger.error("margin preflight failed for %s", sym)
        return False, "margin-check-failed"
    return True, ""


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------

def submit_candidate(trader, candidate: Dict[str, Any],
                     reason: str = "order",
                     proposed: Optional[Dict[str, Any]] = None,
                     cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Single entry path. Returns {"status","record"|"reason"}.

    Updates the transaction-like `proposed` map on confirmed fills so the
    next candidate in the same pass sees the new exposure (atomic sector).
    """
    ok, why = preflight(trader, candidate, proposed=proposed, cfg=cfg)
    if not ok:
        try:
            trader._log_event("skip", {"symbol": candidate.get("symbol"),
                                       "action": candidate.get("action"),
                                       "reason": why})
        except Exception:
            pass
        return {"status": "SKIP", "reason": why}

    sym = str(candidate.get("symbol", ""))
    action = str(candidate.get("action", ""))
    broker = getattr(trader, "broker", None)
    held = (getattr(broker, "positions", {}) or {}).get(sym)
    qty = int(candidate.get("position_size") or 0)
    # SELL without shorts and without holdings is never submitted — the
    # CNC settlement rule. (preflight already returned no-holdings for it;
    # this is the belt-and-braces submit-side guard.)
    _cfg = cfg if isinstance(cfg, dict) else load_config()
    if action == "SELL" and not bool(_cfg.get("trading", {}).get("allow_shorts", False)):
        held_qty = int(getattr(held, "quantity", 0) or 0) \
            if held is not None and getattr(held, "side", "") == "LONG" else 0
        if held_qty <= 0:
            return {"status": "SKIP", "reason": "no-holdings"}
        if held_qty > 0:
            qty = min(qty, held_qty)

    try:
        order = broker.place_order(
            symbol=sym, side=action, quantity=qty,
            order_type="MKT" if getattr(broker, "provider_name", "") == "megabull" else "LIMIT",
            price=candidate.get("entry_price"),
            stop_loss=candidate.get("stop_loss"), target=candidate.get("target"),
            reason=reason)
    except Exception as exc:
        logger.error("submit %s: broker raised: %s", sym, exc)
        try:
            trader._log_event("skip", {"symbol": sym, "action": action,
                                       "reason": f"broker-error: {exc}"})
        except Exception:
            pass
        return {"status": "ERROR", "reason": str(exc)}

    status = str(getattr(order, "status", "") or "").upper()
    if status in _PENDING or "UNKNOWN" in status:
        try:
            from src.risk_manager import set_broker_uncertain
            set_broker_uncertain(f"{sym} submitted but unconfirmed ({status})")
        except Exception:
            pass
        try:
            trader._log_event("skip", {"symbol": sym, "action": action,
                                       "reason": f"pending: {status}"})
        except Exception:
            pass
        return {"status": "PENDING", "reason": status}
    if status != "FILLED":
        try:
            trader._log_event("skip", {"symbol": sym, "action": action,
                                       "reason": f"rejected: {getattr(order, 'error', '')}"})
        except Exception:
            pass
        return {"status": "REJECTED", "reason": str(getattr(order, "error", ""))}

    record = record_confirmed_fill(trader, candidate, order, qty)
    # Transaction-like exposure update for the NEXT candidate in this pass.
    try:
        if proposed is not None and isinstance(proposed, dict):
            proposed[sym] = {"size": qty, "entry": float(candidate.get("entry_price") or 0.0)}
        cur = getattr(trader, "_proposed", None)
        if isinstance(cur, dict):
            cur[sym] = {"size": qty, "entry": float(candidate.get("entry_price") or 0.0)}
    except Exception:
        pass
    return {"status": "FILLED", "record": record}


def record_confirmed_fill(trader, candidate: Dict[str, Any],
                          order: Any, qty: int) -> Dict[str, Any]:
    """Register a CONFIRMED fill: risk + audit + alert. Alert fires once."""
    if str(getattr(order, "status", "") or "").upper() != "FILLED":
        logger.warning("record_confirmed_fill: ignoring unconfirmed %s order %s",
                       getattr(order, "status", ""),
                       getattr(order, "order_id", ""))
        return {}
    from src.risk_manager import add_position as _add
    sym = str(candidate.get("symbol", ""))
    try:
        _add(sym, qty, float(candidate.get("entry_price") or 0.0),
             float(candidate.get("risk_amount") or 0.0))
    except Exception:
        pass
    record = {
        "symbol": sym, "action": str(candidate.get("action", "")),
        "entry": candidate.get("entry_price"), "stop": candidate.get("stop_loss"),
        "target": candidate.get("target"), "qty": qty,
        "rank": candidate.get("rank"), "setup": candidate.get("setup_name"),
        "confidence": int(candidate.get("confidence") or 0),
        "order_id": getattr(order, "order_id", ""),
    }
    try:
        trader._log_event("order", record)
    except Exception:
        pass
    try:
        from src.alerts import send_signal_alert
        send_signal_alert({**candidate, "order_id": getattr(order, "order_id", "")})
    except Exception as exc:
        logger.warning("Entry alert failed for %s: %s", sym, exc)
    return record


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------

def record_confirmed_close(trader, symbol: str,
                           record: Dict[str, Any]) -> Dict[str, Any]:
    """Single close path: realized pnl + consec + position + halts + log + alert-once.
    Idempotent: prevents duplicate accounting if called multiple times with the same close record.
    """
    if not record:
        return {}
    # The strict runtime books option closes itself before returning them to a
    # caller.  Its marker survives lifecycle rebuilds, so a parent callback
    # forwarding that same record must not apply the realized P&L again.
    if str(record.get("asset", "")).strip().lower() in {"option", "options"}:
        try:
            from src.risk_manager import get_risk_state as _risk
            seen = getattr(_risk(), "_options_intraday_closed_ids", set())
            if not isinstance(seen, set):
                seen = set(seen or [])
            marker = str(record.get("close_id") or "")
            fallback_marker = str(record.get("position_id") or symbol) + "|" + str(
                record.get("closed_at") or "")
            if marker in seen or fallback_marker in seen:
                return record
        except Exception:
            pass
    tid = _get_trader_id(trader)
    ckey = _make_close_key(record)
    now = time.time()

    # Cleanup expired TTL cache items
    expired = [k for k, v in _processed_closes.items() if now - v > _IDEMPOTENCY_TTL]
    for k in expired:
        _processed_closes.pop(k, None)

    cache_key = (tid, symbol, ckey)
    if cache_key in _processed_closes:
        logger.debug("record_confirmed_close: duplicate close event ignored for %s (%s)", symbol, ckey)
        return record
    _processed_closes[cache_key] = now

    try:
        from src.risk_manager import (close_position as _rm_close,
                                      get_risk_state as _risk,
                                      update_pnl as _rm_pnl)
        _rm_pnl(float(record.get("pnl") or 0.0))
        if str(record.get("asset", "")).strip().lower() in {"option", "options"}:
            risk_state = _risk()
            seen = getattr(risk_state, "_options_intraday_closed_ids", set())
            if not isinstance(seen, set):
                seen = set(seen or [])
            marker = str(record.get("close_id") or "")
            seen.add(marker)
            seen.add(str(record.get("position_id") or symbol) + "|" + str(
                record.get("closed_at") or ""))
            setattr(risk_state, "_options_intraday_closed_ids", seen)
        # partial exits keep the runner visible — only full closes pop the key
        try:
            broker = getattr(trader, "broker", None)
            still_open = (getattr(broker, "positions", {}) or {}).get(symbol)
            if still_open is None or int(getattr(still_open, "quantity", 0) or 0) <= 0:
                _rm_close(symbol)
            else:
                # refresh counts from the mirror for partials
                from src.risk_manager import get_risk_state as _grs
                _grs().open_positions = len(getattr(broker, "positions", {}) or {})
        except Exception:
            _rm_close(symbol)
    except Exception:
        pass
    try:
        trader._log_event("close", record)
    except Exception:
        pass
    # The broker already fired the exit alert via _record_close; do NOT
    # re-fire here — exactly-once alerting lives in the broker.
    return record
