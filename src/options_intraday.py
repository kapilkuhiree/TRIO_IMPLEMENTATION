"""Policy and validation for the explicit ``options_intraday`` mode.

The normal TRIO path is deliberately not imported by this module.  The
options-only runtime is an integration boundary: it receives a generated
plan, re-validates the concrete exchange chain snapshot in that plan, and
hands a strict, remote-paper candidate to the options adapter.

``config/options_intraday.yaml`` is an overlay.  It is loaded only by callers
which opt into this mode; ``config/config.yaml`` and production defaults are
therefore left untouched.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
import math
import os
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

import yaml

from src.demand_supply import demand_supply_enabled


OPTIONS_INTRADAY = "options_intraday"
COMBINED = "combined"
DEFAULT_TRADING_MODE = "production"
NOT_READY_FOR_OPTIONS_PNL = "NOT_READY_FOR_OPTIONS_PNL"

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_DEFAULTS: Dict[str, Any] = {
    "execution_class": "remote_paper",
    "entry_cutoff": "14:30",
    "hard_flat": "15:10",
    "strategies": ["orb", "pullback", "momentum"],
    "stop_premium_fraction": 0.20,
    "target_premium_fraction": 0.30,
    "max_daily_entries": 5,
    "max_daily_loss_pct": 1.0,
    "max_consecutive_losses": 2,
    "max_unrealized_loss_pct": 1.0,
    "max_portfolio_risk_pct": 1.0,
    "max_correlated_positions": 1,
    "correlated_groups": [["NIFTY", "FINNIFTY"]],
    "plan_max_age_minutes": 15,
    "signal_max_age_minutes": 60,
    "chain_max_age_minutes": 15,
    "state_dir": "",
}


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def resolve_trading_mode(cfg: Optional[Dict[str, Any]] = None,
                         requested: Optional[str] = None) -> str:
    """Resolve the trading mode while retaining production as the default."""
    cfg = cfg or {}
    raw = (os.environ.get("TRIO_TRADING_MODE") or requested or
           (cfg.get("execution", {}) or {}).get("trading_mode") or
           cfg.get("trading_mode") or DEFAULT_TRADING_MODE)
    value = str(raw).strip().lower().replace("-", "_")
    return value or DEFAULT_TRADING_MODE


def options_intraday_enabled(cfg: Optional[Dict[str, Any]] = None,
                             requested: Optional[str] = None) -> bool:
    """Return whether the explicit options-only mode is selected.

    Combined mode also builds a strict options plan, but it must not take the
    options-only execution path.  Keep this predicate exact so existing
    callers cannot accidentally turn combined mode into an options-only
    session.
    """
    return resolve_trading_mode(cfg, requested) == OPTIONS_INTRADAY


def combined_mode_enabled(cfg: Optional[Dict[str, Any]] = None,
                          requested: Optional[str] = None) -> bool:
    """Return whether the equity-plus-strict-options mode is selected."""
    return resolve_trading_mode(cfg, requested) == COMBINED


def strict_options_enabled(cfg: Optional[Dict[str, Any]] = None,
                           requested: Optional[str] = None) -> bool:
    """Return whether a strict options plan is part of the selected mode.

    ``options_intraday_enabled`` intentionally remains the exact options-only
    predicate.  This broader helper is for planning code that needs to know
    whether to build the strict options leg in either supported mode.
    """
    return (options_intraday_enabled(cfg, requested) or
            combined_mode_enabled(cfg, requested))


def load_options_intraday_config(
    cfg: Optional[Dict[str, Any]] = None,
    overlay_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Return a private options-mode config view with its dedicated overlay.

    The optional file may contain either an ``options_intraday`` section or
    the section's keys directly.  A supplied mapping is copied, never
    mutated, which is useful for parent integration and tests.
    """
    if cfg is None:
        from src.utils import load_config
        base = load_config()
    else:
        base = cfg
    result = deepcopy(base or {})
    path = Path(overlay_path) if overlay_path else (
        PROJECT_ROOT / "config" / "options_intraday.yaml")
    overlay: Dict[str, Any] = {}
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle) or {}
            if isinstance(loaded, dict):
                nested = loaded.get("options_intraday")
                overlay = (nested if isinstance(nested, dict) else loaded)
    except (OSError, UnicodeError, yaml.YAMLError):
        # A malformed optional overlay is not allowed to silently alter the
        # production path.  The runtime will use built-in safe defaults.
        overlay = {}

    mode_cfg = _deep_merge(_DEFAULTS, overlay)
    mode_cfg = _deep_merge(mode_cfg, result.get("options_intraday", {}) or {})
    result["options_intraday"] = mode_cfg
    return result


def options_intraday_config(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Get the effective private mode section."""
    merged = load_options_intraday_config(cfg)
    return dict(merged.get("options_intraday", {}) or {})


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse an ISO timestamp as an aware UTC datetime."""
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def parse_exchange_timestamp(value: Any) -> Optional[datetime]:
    """Read NSE exchange record time (naive exchange strings mean IST)."""
    from src.market_clock import IST
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
        for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%m-%Y %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(timezone.utc)


def _now_utc(now: Optional[datetime]) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _option_settings(cfg: Dict[str, Any]) -> Dict[str, Any]:
    mode = cfg.get("options_intraday", {}) or {}
    base = cfg.get("options", {}) or {}
    # The ordinary options block supplies the exchange's lot size and quote
    # thresholds.  Mode-specific values always win.
    mode_values = {
        key: mode[key] for key in (
            "underlying", "underlyings", "exchange", "lot_size", "strike_step",
            "min_oi", "max_spread_pct", "chain_max_age_minutes",
        ) if key in mode
    }
    return _deep_merge(
        _deep_merge(dict(base), dict(mode.get("option_settings", {}) or {})),
        mode_values,
    )


def _quote_value(leg: Dict[str, Any], name: str) -> Optional[float]:
    aliases = {
        "bid": ("bid", "bidPrice", "buyPrice1"),
        "ask": ("ask", "askPrice", "sellPrice1"),
    }
    for key in aliases[name]:
        value = _finite(leg.get(key))
        if value is not None:
            return value
    return None


def _same_expiry(left: Any, right: Any) -> bool:
    def as_date(value: Any) -> Optional[date]:
        text = str(value or "").strip()
        for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        return None
    left_date, right_date = as_date(left), as_date(right)
    if left_date is not None and right_date is not None:
        return left_date == right_date
    return str(left or "").strip() == str(right or "").strip()


def _find_chain_row(chain: Dict[str, Any], expiry: str,
                    strike: float) -> Optional[Dict[str, Any]]:
    by_expiry = chain.get("by_expiry", {}) or {}
    rows = by_expiry.get(expiry, []) or []
    if not rows:
        for key, values in by_expiry.items():
            if _same_expiry(key, expiry):
                rows = values or []
                break
    for row in rows:
        row_strike = _finite(row.get("strike"))
        if row_strike is not None and abs(row_strike - strike) <= 1e-8:
            return row
    return None


def _expiry_is_listed(chain: Dict[str, Any], expiry: str) -> Tuple[bool, str]:
    listed_value = next((value for value in (chain.get("expiries", []) or [])
                         if _same_expiry(value, expiry)), None)
    if listed_value is None:
        return False, "expiry is not exchange-listed"
    by_expiry = chain.get("by_expiry", {}) or {}
    rows = by_expiry.get(expiry, []) or []
    if not rows:
        rows = by_expiry.get(str(listed_value), []) or []
    if not rows:
        for key, values in by_expiry.items():
            if _same_expiry(key, expiry):
                rows = values or []
                break
    if not rows:
        return False, "expiry has no exchange strikes"
    if not any(isinstance(row.get("CE"), dict) for row in rows):
        return False, "expiry has no CE listing"
    if not any(isinstance(row.get("PE"), dict) for row in rows):
        return False, "expiry has no PE listing"
    return True, ""


def _configured_underlyings(settings: Dict[str, Any]) -> Iterable[str]:
    values = settings.get("underlyings")
    if isinstance(values, (list, tuple, set)):
        return {str(item).strip().upper() for item in values if str(item).strip()}
    configured = str(settings.get("underlying", "NIFTY") or "NIFTY").strip().upper()
    return {configured}


def validate_option_candidate(
    candidate: Dict[str, Any],
    plan: Optional[Dict[str, Any]] = None,
    cfg: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Tuple[bool, str]:
    """Revalidate one concrete, long, single-leg candidate.

    Validation intentionally requires ``plan['option_chain']``.  Archived
    expiry diagnostics or a selector's ``premium`` are not enough to route a
    remote order: the selected strike must still be listed and its bid/ask
    must match the exchange quote in the in-memory chain snapshot.
    """
    cfg = load_options_intraday_config(cfg) if cfg is not None else load_options_intraday_config()
    settings = _option_settings(cfg)
    candidate = candidate or {}
    option = candidate.get("option_legs") or candidate
    if not isinstance(option, dict):
        return False, "missing option contract"
    asset = str(candidate.get("asset", "")).strip().lower()
    if asset != "options":
        return False, "options_intraday accepts options candidates only"
    strategy = str(option.get("strategy") or "").strip().lower()
    if strategy not in {"long-call", "long-put"}:
        return False, f"unsupported options strategy: {strategy or 'missing'}"
    legs = option.get("legs") or []
    if len(legs) != 1 or not isinstance(legs[0], dict):
        return False, "options_intraday requires exactly one option leg"
    leg = legs[0]
    if str(leg.get("side", "")).strip().upper() != "BUY":
        return False, "options_intraday permits one BUY leg only"
    kind = str(leg.get("kind", "")).strip().upper()
    wanted_kind = "CE" if strategy == "long-call" else "PE"
    if kind != wanted_kind:
        return False, f"strategy/type mismatch: {strategy} requires {wanted_kind}"

    underlying = str(option.get("underlying") or
                      candidate.get("option_underlying") or "").strip().upper()
    if not underlying or underlying not in set(_configured_underlyings(settings)):
        return False, f"unapproved option underlying: {underlying or '?'}"
    expected_exchange = str(settings.get("exchange", "NSE") or "NSE").upper()
    exchange = str(option.get("exchange") or leg.get("exchange") or "").upper()
    if exchange != expected_exchange:
        return False, f"exchange mismatch: {exchange or '?'} != {expected_exchange}"

    expiry = str(option.get("expiry") or leg.get("expiry") or "").strip()
    if not expiry or str(leg.get("expiry") or expiry).strip() != expiry:
        return False, "missing or inconsistent expiry"
    strike = _finite(leg.get("strike"))
    if strike is None or strike <= 0:
        return False, "invalid strike"
    step = _finite(settings.get("strike_step", 50))
    if step is None or step <= 0 or abs(strike / step - round(strike / step)) > 1e-8:
        return False, "strike does not match configured interval"

    lot_size = option.get("lot_size", candidate.get("lot_size"))
    if isinstance(lot_size, bool) or not isinstance(lot_size, int) or lot_size <= 0:
        return False, "missing or invalid contract lot size"
    configured_lot = _finite(settings.get("lot_size", 65))
    if configured_lot is None or lot_size != int(configured_lot):
        return False, "contract lot size mismatch"

    chain = (plan or {}).get("option_chain")
    if not isinstance(chain, dict):
        return False, "missing private option chain snapshot"
    chain_symbol = str(chain.get("symbol") or chain.get("underlying_symbol") or
                       "").strip().upper()
    if chain_symbol != underlying:
        return False, "chain underlying mismatch"
    listed, listing_reason = _expiry_is_listed(chain, expiry)
    if not listed:
        return False, listing_reason
    row = _find_chain_row(chain, expiry, strike)
    if row is None:
        return False, "selected strike is not exchange-listed"
    exchange_leg = row.get(kind)
    if not isinstance(exchange_leg, dict):
        return False, "selected option type is not exchange-listed"
    bid = _quote_value(exchange_leg, "bid")
    ask = _quote_value(exchange_leg, "ask")
    selected_bid = _quote_value(leg, "bid")
    selected_ask = _quote_value(leg, "ask")
    premium = _finite(leg.get("premium"))
    if (bid is None or ask is None or bid <= 0 or ask <= bid or
            selected_bid is None or selected_ask is None):
        return False, "selected quote has no executable bid/ask"
    if abs(selected_bid - bid) > 1e-8 or abs(selected_ask - ask) > 1e-8:
        return False, "selected quote differs from exchange chain"
    if premium is None or premium <= 0:
        return False, "missing option premium"
    if str(leg.get("premium_src") or "").lower() != "live":
        return False, "quote is not live bid/ask sourced"
    mid = (bid + ask) / 2.0
    max_spread = _finite(settings.get("max_spread_pct", 5))
    if max_spread is None or ((ask - bid) / mid * 100.0) > max_spread:
        return False, "option spread exceeds configured limit"
    oi = _finite(exchange_leg.get("openInterest", exchange_leg.get("oi")))
    volume = _finite(exchange_leg.get("totalTradedVolume",
                                      exchange_leg.get("volume")))
    min_oi = _finite(settings.get("min_oi", 0)) or 0.0
    if oi is None or oi < min_oi:
        return False, "option open interest below configured limit"
    if volume is None or volume <= 0:
        return False, "option volume is missing"

    # This must be the exchange quote timestamp, not the request/retrieval
    # timestamp.  The plan generator owns the exchange adapter and must copy
    # this field into its private chain snapshot.
    quote_timestamp = chain.get("quote_timestamp")
    quoted_at = parse_exchange_timestamp(quote_timestamp)
    if quoted_at is None:
        return False, "missing exchange quote timestamp"
    current = _now_utc(now)
    age = (current - quoted_at).total_seconds()
    ttl = _finite(settings.get("chain_max_age_minutes", 15)) or 15.0
    if age < 0 or age > ttl * 60:
        return False, "stale exchange option quote"
    selected_time = (exchange_leg.get("quote_timestamp") or
                     row.get("quote_timestamp") or quote_timestamp)
    selected_at = parse_exchange_timestamp(selected_time)
    if selected_at is None or not 0 <= (current - selected_at).total_seconds() <= ttl * 60:
        return False, "stale selected option quote"

    action = str(candidate.get("action", "")).strip().upper()
    direction = str(candidate.get("direction", "")).strip().lower()
    if action not in {"BUY", "SELL"}:
        return False, "missing signal action"
    wanted_direction = "bullish" if action == "BUY" else "bearish"
    if direction != wanted_direction:
        return False, "signal direction/action mismatch"
    if kind != ("CE" if action == "BUY" else "PE"):
        return False, "signal action/option type mismatch"
    if demand_supply_enabled(cfg, context="options", timeframe="5m"):
        confirmation = (candidate.get("option_diagnostic", {}) or {}).get(
            "demand_supply")
        if not isinstance(confirmation, dict) or confirmation.get("confirmed") is not True:
            return False, "missing demand/supply confirmation"
    expiry_date = None
    for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            expiry_date = datetime.strptime(expiry, fmt).date()
            break
        except ValueError:
            continue
    from src.market_clock import IST
    if expiry_date is None or expiry_date < current.astimezone(IST).date():
        return False, "invalid or expired option expiry"
    return True, ""


def is_live_execution(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Whether an explicit live switch is active; options mode rejects it."""
    cfg = cfg or {}
    general = cfg.get("general", {}) or {}
    raw = os.environ.get("TRIO_LIVE")
    if raw is not None and str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}:
        return True
    return str(general.get("mode", cfg.get("mode", ""))).strip().lower() in {
        "live", "real"
    }


def validate_options_mode_environment(cfg: Dict[str, Any]) -> Tuple[bool, str]:
    """Require MegaBull remote paper and reject every live/local fallback."""
    if is_live_execution(cfg):
        return False, "options_intraday rejects live execution"
    provider = str(os.environ.get("TRIO_BROKER_PROVIDER") or
                   (cfg.get("broker", {}) or {}).get("provider", "")).strip().lower()
    if provider != "megabull":
        return False, "options_intraday requires TRIO_BROKER_PROVIDER=megabull"
    remote = os.environ.get("TRIO_REMOTE_REQUIRED")
    remote_required = (str(remote).strip().lower() in {"1", "true", "yes", "on", "y"}
                       if remote is not None else
                       bool((cfg.get("execution", {}) or {}).get(
                           "remote_paper_required", False)))
    if not remote_required:
        return False, "options_intraday requires remote paper execution"
    shadow = os.environ.get("TRIO_ALLOW_LOCAL_OPTION_SHADOW")
    if shadow is not None and str(shadow).strip().lower() in {
            "1", "true", "yes", "on", "y"}:
        return False, "options_intraday forbids local option shadowing"
    if (cfg.get("execution", {}) or {}).get("allow_local_option_shadow") is True:
        return False, "options_intraday forbids local option shadowing"
    mode_cfg = cfg.get("options_intraday", {}) or {}
    if str(mode_cfg.get("execution_class", "remote_paper")) != "remote_paper":
        return False, "options_intraday execution_class must be remote_paper"
    return True, ""


__all__ = [
    "COMBINED", "DEFAULT_TRADING_MODE", "NOT_READY_FOR_OPTIONS_PNL",
    "OPTIONS_INTRADAY",
    "combined_mode_enabled",
    "is_live_execution", "load_options_intraday_config",
    "options_intraday_config", "options_intraday_enabled",
    "parse_exchange_timestamp",
    "parse_timestamp", "resolve_trading_mode", "strict_options_enabled",
    "validate_option_candidate",
    "validate_options_mode_environment",
]
