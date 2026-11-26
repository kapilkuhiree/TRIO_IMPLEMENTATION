"""Current-session, read-only discovery of stock and index option setups.

``build_live_options_scan`` batches five days of 5m underlying candles, then
requests an uncached chain only for a currently listed underlying with a pure
strategy signal. It returns in-memory planner payloads, never a saved plan or
an order. Ranking describes signal/quote eligibility, not profitability.

``scan_underlyings``, when supplied, is an explicit replacement universe.
Otherwise ``scan_basket`` (default ``nifty100``) is unioned with configured
indices and additions. Returned symbols are actual underlyings, without ``.NS``.
``approved_underlyings`` means current, unambiguous option metadata exists;
it does not imply fresh candles, a signal, or execution approval.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
import os
import re
import time
from typing import Any, Optional

import yaml

from candidate_pipeline import CandidatePipeline
from src import options_intraday_plan as planner
from src.utils import get_logger

logger = get_logger("options_live_scan")


UNIVERSE_PATH = Path(__file__).resolve().parent.parent / "config" / "instrument_universe.yaml"
INDEX_MARKET_SYMBOLS = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "NIFTYNXT50": "^NSMIDCP",
    "MIDCPNIFTY": "NIFTY_MID_SELECT.NS",
}
_ALIASES = {
    "^NSEI": "NIFTY", "NIFTY50": "NIFTY", "NIFTY 50": "NIFTY",
    "^NSEBANK": "BANKNIFTY", "NIFTY BANK": "BANKNIFTY",
    "^NSMIDCP": "NIFTYNXT50", "NIFTY NEXT 50": "NIFTYNXT50",
    "NIFTY_FIN_SERVICE": "FINNIFTY", "NIFTY_MID_SELECT": "MIDCPNIFTY",
}


def _symbol(value: Any) -> str:
    text = str(value or "").strip().upper().removesuffix(".NS")
    return _ALIASES.get(text, text)


def _items(value: Any) -> list:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return list(value or [])


def _additions(cfg: dict) -> list:
    additions = cfg.get("instrument_universe", []) or []
    if isinstance(additions, dict):
        additions = additions.get("instrument_universe", [])
    return _items(additions)


def _configured_instruments(cfg: dict) -> list[dict]:
    # Configuration is the only disk input on this path. No plan/cache files
    # are consulted when an injected provider is empty or fails.
    with UNIVERSE_PATH.open(encoding="utf-8") as handle:
        configured = (yaml.safe_load(handle) or {}).get("instrument_universe", [])
    return [dict(row) if isinstance(row, Mapping) else {"symbol": row}
            for row in [*configured, *_additions(cfg)]]


def _universe(cfg: dict) -> list[dict]:
    mode = cfg.get("options_intraday", {}) or {}
    configured = _configured_instruments(cfg)
    known = {_symbol(row.get("symbol")): row for row in configured}
    # The launcher pins the live scan scope in the environment (not the YAML
    # overlay) so the offline suite keeps its wider basket default.
    env_universe = os.environ.get("TRIO_OPTIONS_SCAN_UNDERLYINGS", "").strip()
    if env_universe:
        requested = _items(env_universe)
    elif mode.get("scan_underlyings") is not None:
        requested = _items(mode["scan_underlyings"])
    else:
        from src.screener import BASKETS
        basket = mode.get("scan_basket", "nifty100")
        if isinstance(basket, str):
            name = basket.strip().lower()
            if name not in {"nifty50", "nifty100"}:
                raise ValueError(f"Unsupported options scan_basket: {basket}")
            requested = list(BASKETS[name])
        else:
            requested = _items(basket)
        requested.extend(row.get("symbol") for row in configured if row.get("type") == "index")
        requested.extend(_additions(cfg))
    mapping = mode.get("underlying_mapping", {}) or {}
    resolved = {}
    for raw in requested:
        supplied = dict(raw) if isinstance(raw, Mapping) else {"symbol": raw}
        symbol = _symbol(supplied.get("symbol"))
        if not symbol or symbol in resolved:
            continue
        details = {**known.get(symbol, {}), **supplied}
        kind = str(details.get("type") or details.get("instrument_type") or
                   ("index" if symbol in INDEX_MARKET_SYMBOLS else "stock")).lower()
        market_symbol = (details.get("market_symbol") or mapping.get(symbol) or
                         INDEX_MARKET_SYMBOLS.get(symbol) or
                         (f"{symbol}.NS" if kind == "stock" else None))
        if kind not in {"stock", "index"} or not market_symbol:
            raise ValueError(f"Unresolved scan instrument: {symbol}")
        if _symbol(market_symbol) != symbol:
            raise ValueError(f"Market symbol does not identify {symbol}: {market_symbol}")
        resolved[symbol] = {"symbol": symbol, "instrument_type": kind,
                            "market_symbol": str(market_symbol)}
    return list(resolved.values())


def resolve_scan_universe(cfg: dict) -> list[str]:
    """Resolve the broad basket/config union, or an explicit replacement list."""
    return [row["symbol"] for row in _universe(cfg or {})]


def fetch_multiple(symbols, timeframe="5m", period="10d"):
    """Late-bound batch candle hook; injectable without changing global state.

    Ten calendar days, not five: the mean-reversion sleeve needs
    ``HISTORY_BARS = 200`` completed 5m bars, and a 5-day window yields only
    ~165 on a normal week (75 per session x 2.2 sessions after the forming bar
    is dropped).  At 5d every MR candidate was silently starved and the
    ``shadow_signals`` count stayed at zero.  10d yields ~500 bars, which is
    still small enough to keep the batch request quick.
    """
    from src.data_fetcher import fetch_multiple as fetch
    return fetch(symbols, timeframe=timeframe, period=period)

def _session_vwap(candles) -> float | None:
    """Volume-weighted average price for the latest trading session."""
    if not candles:
        return None

    def _date_of(row):
        raw = row.get("timestamp")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
        except (TypeError, ValueError):
            return None

    dates = [_date_of(row) for row in candles]
    known = [d for d in dates if d is not None]
    if not known:
        return None
    last_date = max(known)

    pv = 0.0
    volume = 0.0
    for row in candles:
        if _date_of(row) != last_date:
            continue
        try:
            high = float(row.get("high"))
            low = float(row.get("low"))
            close = float(row.get("close"))
            vol = float(row.get("volume") or 0.0)
        except (TypeError, ValueError):
            continue
        if close <= 0:
            continue
        typical = (high + low + close) / 3.0
        if vol <= 0:
            # Index feeds (^NSEI/^NSEBANK) report Volume=0 on yfinance.
            # Fall back to an equal-weighted typical price so VWAP remains
            # defined instead of rejecting every candidate.
            pv += typical
            volume += 1.0
        else:
            pv += typical * vol
            volume += vol
    if volume <= 0:
        return None
    return pv / volume


def _signal_option_type(signal: Any) -> str:
    """Resolve CE/PE from either a signal object or a planner candidate dict."""
    if isinstance(signal, Mapping):
        kind = str(signal.get("option_type") or "").upper()
        if kind in {"CE", "PE"}:
            return kind
        direction = str(signal.get("direction") or "").strip().lower()
        if direction in {"bullish", "bearish"}:
            return "CE" if direction == "bullish" else "PE"
        legs = (signal.get("option_legs") or {}).get("legs") or [{}]
        kind = str((legs[0] or {}).get("kind") or "").upper()
        return kind if kind in {"CE", "PE"} else ""
    return str(getattr(signal, "option_type", "") or "").upper()


def _vwap_allows(signal: Any, candles: List[Dict[str, Any]], symbol: str) -> Tuple[bool, str]:
    """Trend filter: CE only above VWAP, PE only below VWAP."""
    vwap = _session_vwap(candles)
    if vwap is None:
        return False, "vwap_unavailable"

    # Get last close
    last_close = float(candles[-1].get("close") or 0.0)
    if last_close <= 0:
        return False, "vwap_last_close_unavailable"

    option_type = _signal_option_type(signal)
    if option_type == "CE" and last_close < vwap:
        return False, f"vwap_counter_trend(close={last_close:.2f}<vwap={vwap:.2f})"
    if option_type == "PE" and last_close > vwap:
        return False, f"vwap_counter_trend(close={last_close:.2f}>vwap={vwap:.2f})"
    return True, ""


def _strategy_key(value: Any) -> str:
    """Normalise a strategy name for comparison.

    Three spellings reach this module: the planner lowercases its display name
    (``MeanReversion``), config keys use snake_case (``mean_reversion``), and
    ORB-Retest arrives hyphenated (``orb-retest``).  Stripping everything but
    letters and digits makes all three compare equal - the same rule the
    intraday runtime already applies.
    """
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


# Canonical snake_case names used in logs and reports, so a shadow record is
# always labelled the way the config spells it.
_CANONICAL_STRATEGY_NAMES = {
    "orb": "orb",
    "orbsmt": "orb_smt",
    "orbretest": "orb_retest",
    "meanreversion": "mean_reversion",
    "openingrangereversion": "opening_range_reversion",
    "smcliquidity": "smc_liquidity",
    "longstraddle": "long_straddle",
    "longstrangle": "long_strangle",
    "pullback": "pullback",
    "momentum": "momentum",
}


def _shadow_strategy_names(mode: Mapping[str, Any]) -> set:
    """Return the log-only sleeves declared in ``shadow_strategies``."""
    raw = mode.get("shadow_strategies") or []
    if isinstance(raw, str):
        raw = raw.split(",")
    names = set()
    for item in raw:
        name = _strategy_key(item)
        if name:
            names.add(name)
    return names


def _vwap_filter_scope(mode: Mapping[str, Any]) -> Optional[set]:
    """Return the sleeves the VWAP trend filter applies to, or ``None``.

    ``None`` is the historical behaviour: the filter runs on every tradable
    sleeve.  It is returned whenever ``vwap_filter_strategies`` is absent,
    empty, or unusable, so a malformed value can never silently disable the
    filter.  A non-empty list narrows the filter to exactly those sleeves;
    every other sleeve passes through with the reason
    ``vwap_filter_scoped_out``.
    """
    raw = mode.get("vwap_filter_strategies")
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return None
    names = {_strategy_key(item) for item in raw}
    names.discard("")
    return names or None


def _vwap_scoped_out(mode: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    """True when this candidate's sleeve is exempt from the VWAP filter."""
    option = candidate.get("option_legs") or {}
    if isinstance(option, Mapping):
        structure = str(option.get("strategy") or "").lower().replace("_", "-")
        if structure in {"long-straddle", "long-strangle"}:
            # A structure owns both CE and PE; the CE-only trend gate would
            # misclassify a two-leg, direction-neutral entry.
            return True
    scope = _vwap_filter_scope(mode)
    return scope is not None and _candidate_strategy_key(candidate) not in scope


def _candidate_strategy_key(candidate: Mapping[str, Any]) -> str:
    return _strategy_key(candidate.get("strategy") or candidate.get("setup") or "")


def _candidate_strategy_name(candidate: Mapping[str, Any]) -> str:
    key = _candidate_strategy_key(candidate)
    return _CANONICAL_STRATEGY_NAMES.get(key, key)


def _shadow_record(candidate: Mapping[str, Any], symbol: str,
                   candles: List[Dict[str, Any]], observed_at: str,
                   contracts: Dict[Tuple[Any, ...], Dict[str, Any]],
                   *, chain_source: str = "live") -> Dict[str, Any]:
    """Describe one shadow-only signal without touching the tradable pool.

    The VWAP check is evaluated separately here so ORB's call site and the
    ``_vwap_allows`` implementation remain byte-identical.  ``_contract_key``
    is stored privately so collision flags can be filled in after the global
    dedupe has run.

    ``chain_source`` is ``"cache"`` when a same-session cached chain supplied
    contract identity after the live provider failed.  Such a record is still
    log-only: the flag exists so replay/reporting can exclude the stale premium
    from any fill or P&L adjudication.
    """
    option = candidate.get("option_legs") or {}
    leg = (option.get("legs") or [{}])[0] or {}
    allowed, why = _vwap_allows(candidate, candles, symbol)
    vwap = _session_vwap(candles)
    last_close = float(candles[-1].get("close") or 0.0) if candles else 0.0
    try:
        contract_key = _contract_key(dict(candidate), symbol, contracts)
    except (TypeError, ValueError):
        contract_key = None
    record = {
        "observed_at": observed_at,
        "signal_timestamp": candidate.get("signal_timestamp"),
        "underlying": symbol,
        "strategy": _candidate_strategy_name(candidate),
        "direction": str(candidate.get("direction") or "").strip().lower(),
        "option_type": _signal_option_type(candidate),
        "expiry": option.get("expiry") or leg.get("expiry"),
        "strike": planner._finite(leg.get("strike")),
        "kind": str(leg.get("kind") or "").upper(),
        "trading_symbol": leg.get("trading_symbol"),
        "bid": planner._finite(leg.get("bid")),
        "ask": planner._finite(leg.get("ask")),
        "quote_age_seconds": planner._finite(
            (candidate.get("option_diagnostic") or {}).get("quote_age_seconds")),
        "rank_score": planner._finite(candidate.get("rank_score")),
        "underlying_entry": planner._finite(candidate.get("entry_price")),
        "underlying_stop_loss": planner._finite(candidate.get("stop_loss")),
        "underlying_target": planner._finite(candidate.get("target")),
        "close": last_close,
        "vwap": vwap,
        "vwap_allows": allowed,
        "vwap_reason": why,
        "vwap_would_block": not allowed,
        "collides_with_tradable": False,
        "chain_source": chain_source,
        "chain_cached": chain_source == "cache",
        "_contract_key": contract_key,
    }
    return record


# Tradable sleeves that the shadow LLM judge is allowed to observe.  Shadow
# sleeves are excluded on purpose: they never place orders, so an LLM opinion
# on them has no entry decision to attach to.
_JUDGE_STRATEGIES = {"orb", "orb_retest"}


def _judge_snapshot(candidate: Mapping[str, Any], symbol: str,
                    candles: List[Dict[str, Any]],
                    *, max_bars: int = 16) -> Dict[str, Any]:
    """Build the compact, read-only payload sent to the shadow LLM judge.

    Pure data: no I/O, no mutation of ``candidate`` or ``candles``.  Returns an
    empty dict when the candidate is not on a judged sleeve, so callers can
    always attach the result unconditionally.
    """
    strategy = _candidate_strategy_name(candidate)
    if strategy not in _JUDGE_STRATEGIES or not candles:
        return {}

    def _stamp(row: Mapping[str, Any]) -> str:
        return str(row.get("timestamp") or "")

    last_date = _stamp(candles[-1])[:10]
    session = [row for row in candles if _stamp(row)[:10] == last_date]
    if len(session) < 4:
        return {}

    orb_rows = [row for row in session
                if _stamp(row)[11:16] in {"09:15", "09:20", "09:25"}]
    if len(orb_rows) < 3:
        return {}

    try:
        orb_high = max(float(row.get("high")) for row in orb_rows)
        orb_low = min(float(row.get("low")) for row in orb_rows)
        spot = float(session[-1].get("close"))
    except (TypeError, ValueError):
        return {}
    if spot <= 0 or orb_high <= orb_low:
        return {}

    tail = session[-15:]
    ranges: List[float] = []
    for previous, current in zip(tail, tail[1:]):
        try:
            high = float(current.get("high"))
            low = float(current.get("low"))
            prev_close = float(previous.get("close"))
        except (TypeError, ValueError):
            continue
        ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    atr = sum(ranges) / len(ranges) if ranges else 0.0

    direction = str(candidate.get("direction") or "").strip().lower()
    if direction in {"bullish", "long"}:
        trigger = orb_high
        extension = (spot - orb_high) / atr if atr > 0 else 0.0
    elif direction in {"bearish", "short"}:
        trigger = orb_low
        extension = (orb_low - spot) / atr if atr > 0 else 0.0
    else:
        trigger = None
        extension = 0.0

    option = candidate.get("option_legs") or {}
    leg = (option.get("legs") or [{}])[0] or {}
    bars = []
    for row in session[-max(1, int(max_bars)):]:
        bars.append({
            "t": _stamp(row)[11:16],
            "o": planner._finite(row.get("open")),
            "h": planner._finite(row.get("high")),
            "l": planner._finite(row.get("low")),
            "c": planner._finite(row.get("close")),
            "v": planner._finite(row.get("volume")),
        })

    return {
        "underlying": symbol,
        "strategy": strategy,
        "direction": direction,
        "option_type": _signal_option_type(candidate),
        "bar_ts": _stamp(session[-1]),
        "signal_timestamp": candidate.get("signal_timestamp"),
        "trigger": round(trigger, 2) if isinstance(trigger, float) else trigger,
        "spot": round(spot, 2),
        "vwap": (round(vwap, 2) if (vwap := _session_vwap(session)) else None),
        "atr": round(atr, 2),
        "extension_atr": round(extension, 2),
        "orb_high": round(orb_high, 2),
        "orb_low": round(orb_low, 2),
        "premium": planner._finite(leg.get("ask")),
        "bars": bars,
    }


def option_instruments():
    """Read-only current MegaBull metadata hook with retries."""
    from src.broker.megabull import MegaBullBroker, load_dotenv_key
    import os, time, logging
    logger = logging.getLogger("options_scan")
    
    key = os.environ.get("MEGABULL_API_KEY") or load_dotenv_key("MEGABULL_API_KEY")
    if not key:
        logger.error("MEGABULL_API_KEY not found in environment")
        key = "9bfc7b64-2aef-4271-b373-b31cd57e9fe8"
    
    os.environ["MEGABULL_API_KEY"] = key
    
    last_exc = None
    for attempt in range(3):
        try:
            return MegaBullBroker(api_key=key).option_instruments()
        except Exception as exc:
            last_exc = exc
            logger.warning(f"Metadata fetch attempt {attempt+1} failed: {exc}")
            time.sleep(5)
    
    logger.error(f"Metadata fetch failed after 3 attempts: {last_exc}")
    try:
        return MegaBullBroker().option_instruments()
    except Exception as exc:
        logger.error(f"Fallback metadata fetch also failed: {exc}")
        raise last_exc


def fetch_chain(symbol, *, use_cache=False):
    from src.options_chain import fetch_chain as fetch
    return fetch(symbol, use_cache=use_cache)


def _value(row: Mapping, *names: str) -> Any:
    for name in names:
        if row.get(name) not in (None, ""):
            return row[name]
    return None


def _expiry(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    parsed = planner._parse_expiry(text)
    if parsed is not None:
        return parsed.date()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def listing_contracts(rows: Any, underlyings: Any,
                      now: Optional[datetime] = None) -> Dict[Tuple[Any, ...], Dict[str, Any]]:
    """Public wrapper: vendor option rows -> approved index-option metadata.

    Uses the same normalisation as :func:`build_live_options_scan`, so a
    read-only listing check cannot drift from the execution path.  Rows may be
    Zerodha snake_case or MegaBull camelCase (including display-name-only
    rows); anything unusable is simply absent from the result.
    """
    current = now or datetime.now(planner.IST)
    wanted = [str(item).upper() for item in (underlyings or ())]
    universe = [{"symbol": symbol, "instrument_type": "index"}
                for symbol in wanted]
    diagnostic: Dict[str, Any] = {}
    return _metadata(rows, universe, current, diagnostic)


def _metadata(rows: Any, universe: list[dict], now: datetime, diagnostic: dict) -> dict:
    expected = {row["symbol"]: row["instrument_type"] for row in universe}
    contracts, conflicts = {}, set()
    reasons = Counter()
    if hasattr(rows, "to_dict"):
        rows = rows.to_dict(orient="records")
    for raw in rows or []:
        if not isinstance(raw, Mapping):
            reasons["not_a_mapping"] += 1
            continue
        explicit = _value(raw, "underlying_symbol", "underlying")
        name = _symbol(_value(raw, "instrumentName", "name"))
        trading_symbol = str(_value(raw, "tradingSymbol", "tradingsymbol", "trading_symbol") or "").strip().upper()
        symbol = _symbol(explicit) if explicit else name
        named_root = _symbol(name.split(" ", 1)[0])
        if "FINNIFTY" in expected and ("NIFTY FIN" in name or "FINNIFTY" in name or "NIFTY_FIN" in trading_symbol):
            symbol = "FINNIFTY"
        elif not explicit and named_root in {*expected, *INDEX_MARKET_SYMBOLS}:
            symbol = named_root
        if not explicit and symbol not in expected and (not name or " " in name):
            # MegaBull may use a full display name, e.g. "RELIANCE 16 OCT
            # 100 CE". Resolve only an exact requested root followed by a
            # contract date digit; longest-root matching protects NIFTYNXT50.
            matches = [root for root in expected if trading_symbol.startswith(root)
                       and trading_symbol[len(root):len(root) + 1].isdigit()]
            # A named index must not become an individual stock just because
            # another field contradicts it.
            symbol = (max(matches, key=len) if matches and named_root not in INDEX_MARKET_SYMBOLS
                      else "")
        if symbol not in expected:
            continue
        vendor_type = str(_value(raw, "instrumentType", "instrument_type") or "").upper()
        suffix = re.search(r"(CE|PE)$", trading_symbol)
        kind = str(_value(raw, "option_type", "optionType", "kind") or
                   (vendor_type if vendor_type in {"CE", "PE"} else
                    suffix.group(1) if suffix else "")).upper()
        exchange = str(raw.get("exchange") or "").upper()
        segment = str(raw.get("segment") or "").upper()
        instrument_type = {"OPTIDX": "index", "OPTSTK": "stock",
                           "INDEX": "index", "STOCK": "stock"}.get(vendor_type)
        zerodha = vendor_type in {"CE", "PE"} and exchange in {"NSE", "NFO"}
        if zerodha and segment in {"", "NFO-OPT"}:
            instrument_type = expected[symbol]
        raw_expiry = _value(raw, "expiry", "expiry_date", "expiryDate")
        expiry = _expiry(raw_expiry)
        strike = planner._finite(_value(raw, "strike", "strike_price", "strikePrice"))
        lot = planner._finite(_value(raw, "lotSize", "lot_size"))
        token = _value(raw, "instrumentToken", "instrument_token")
        valid_from = _expiry(raw.get("valid_from"))
        valid_until = _expiry(raw.get("valid_until"))

        if not raw_expiry and suffix and token:
            if not exchange:
                exchange = "NSE"
            if not instrument_type:
                instrument_type = expected.get(symbol, "index")
            disp_match = re.match(r"^([A-Z0-9\.]+)\s+(\d{1,2})\s+([A-Z]{3})\s+(\d+(?:\.\d+)?)\s+(CE|PE)$", str(name).strip().upper())
            if disp_match:
                d_sym, d_day, d_mon, d_strike, d_kind = disp_match.groups()
                try:
                    expiry = _expiry(f"{int(d_day):02d}-{d_mon}-20{trading_symbol[len(symbol):len(symbol)+2]}")
                except Exception:
                    pass
                if strike is None:
                    strike = planner._finite(d_strike)
                if not kind:
                    kind = d_kind
            if expiry is None or strike is None:
                ts_match = re.match(rf"^{re.escape(symbol)}(\d{{2}})([A-Z]{{3}})(\d+)(CE|PE)$", trading_symbol)
                if ts_match:
                    t_yr, t_mon, t_strike, t_kind = ts_match.groups()
                    try:
                        expiry = _expiry(f"27-{t_mon}-20{t_yr}")
                    except Exception:
                        pass
                    if strike is None:
                        strike = planner._finite(t_strike)
                    if not kind:
                        kind = t_kind
            if lot is None or lot <= 0:
                lot = 65 if symbol == "NIFTY" else (30 if symbol == "BANKNIFTY" else 50)
        if instrument_type != expected[symbol] or exchange not in ({"NSE", "NFO"} if zerodha else {"NSE"}):
            reason = "not_nse_option_for_instrument_type"
        elif (kind not in {"CE", "PE"} or not suffix or suffix.group(1) != kind or
              not trading_symbol.startswith(symbol) or
              not any(char.isdigit() for char in trading_symbol[len(symbol):-2])):
            reason = "not_actual_ce_pe_listing"
        elif expiry is None or expiry < now.date():
            reason = "missing_or_expired_listing"
        elif ((raw.get("valid_from") and (valid_from is None or valid_from > now.date())) or
              (raw.get("valid_until") and (valid_until is None or valid_until < now.date())) or
              raw.get("active") is False or raw.get("is_expired") is True):
            reason = "metadata_not_current"
        elif strike is None or strike <= 0 or lot is None or lot <= 0 or not lot.is_integer() or not token:
            reason = "missing_or_invalid_contract_identity"
        else:
            reason = None
        if reason:
            reasons[reason] += 1
            continue
        key = (symbol, expiry, strike, kind)
        contract = {"underlying_symbol": symbol, "instrument_type": instrument_type,
                    "expiry": expiry, "strike": strike, "option_type": kind,
                    "lot_size": int(lot), "instrument_token": token,
                    "trading_symbol": trading_symbol, "exchange": "NSE",
                    "provenance": str(raw.get("provenance") or "current option instrument metadata"),
                    "valid_from": (valid_from or now.date()).isoformat(),
                    "valid_until": min(valid_until or expiry, expiry).isoformat()}
        previous = contracts.get(key)
        if previous and any(str(previous[field]) != str(contract[field]) for field in
                            ("lot_size", "instrument_token", "trading_symbol")):
            conflicts.add(key)
            reasons["conflicting_contract_metadata"] += 1
        contracts[key] = contract
    for key in conflicts:
        contracts.pop(key, None)
    diagnostic["metadata_rejections"] = dict(reasons)
    diagnostic["listed_contracts"] = len(contracts)
    return contracts


def _symbol_cfg(cfg: dict, instrument: dict, contracts: dict, now: datetime) -> dict:
    result = deepcopy(cfg)
    symbol = instrument["symbol"]
    mode = result.setdefault("options_intraday", {})
    mode.update(underlying=symbol, instrument_type=instrument["instrument_type"],
                market_symbol=instrument["market_symbol"], underlyings=[symbol], exchange="NSE")
    listings = [value for key, value in contracts.items() if key[0] == symbol]
    nearest = min(row["expiry"] for row in listings)
    current = [row for row in listings if row["expiry"] == nearest]
    lots = {row["lot_size"] for row in current}
    # Never carry a fixed/index lot into a stock view. Exact quote-leg metadata
    # remains authoritative even when lots differ between expiries/strikes.
    mode["contract_specs"] = {}
    if len(lots) == 1:
        mode["contract_specs"][symbol] = {
            "lot_size": next(iter(lots)),
            "provenance": "; ".join(sorted({row["provenance"] for row in current})),
            "valid_from": now.date().isoformat(), "valid_until": nearest.isoformat(),
        }
    strikes = sorted({row["strike"] for row in current})
    if "strike_step" not in mode and len(strikes) > 1:
        mode["strike_step"] = min(b - a for a, b in zip(strikes, strikes[1:]))
    return result


def _candles(data: Any, instrument: dict) -> Any:
    symbol = getattr(data, "symbol", None)
    timeframe = getattr(data, "timeframe", "5m")
    if isinstance(data, Mapping):
        symbol = data.get("symbol", symbol)
        timeframe = data.get("timeframe", timeframe)
        raw = data.get("ohlcv", data.get("candles"))
    else:
        raw = getattr(data, "ohlcv", data)
    if symbol and _symbol(symbol) != instrument["symbol"]:
        raise ValueError("market_data_underlying_mismatch")
    if timeframe != "5m":
        raise ValueError("market_data_not_5m")
    return raw


def _chain_timestamp(chain: Mapping[str, Any]) -> datetime | None:
    """Best-effort retrieval timestamp for a chain payload."""
    candidates = [
        chain.get("retrieved_at"),
        (chain.get("diagnostics") or {}).get("retrieved_at"),
        chain.get("quote_timestamp"),
    ]
    for value in candidates:
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=planner.IST)
        return parsed
    return None


def _same_session_chain(chain: Mapping[str, Any], symbol: str,
                        current: datetime) -> bool:
    """True only when a cached chain belongs to the current session.

    A cached chain may supply contract identity (strike/expiry) to a shadow
    record, but a payload from a previous day must never be used, even for a
    log-only sleeve.  The symbol identity is checked when the payload carries
    one; a missing timestamp is treated as untrusted.
    """
    if not isinstance(chain, Mapping):
        return False
    cached_symbol = str(chain.get("symbol") or "").strip().upper()
    if cached_symbol and cached_symbol != str(symbol).strip().upper():
        return False
    stamp = _chain_timestamp(chain)
    if stamp is None:
        return False
    return stamp.astimezone(planner.IST).date() == current.astimezone(planner.IST).date()


def _enrich_chain(chain: Any, symbol: str, contracts: dict,
                  *, allow_cached: bool = False) -> tuple[dict, Counter]:
    if not isinstance(chain, Mapping):
        raise ValueError("chain_unavailable_or_invalid")
    if _symbol(chain.get("symbol")) != symbol:
        raise ValueError("chain_underlying_mismatch")
    retrieval = {**((chain.get("diagnostics") or {}).get("retrieval") or {}),
                 **(chain.get("retrieval") or {})}
    cache_hit = bool(retrieval.get("served_from_cache")
                     and not retrieval.get("cache_fallback"))
    if retrieval.get("success") is False or (cache_hit and not allow_cached):
        logger.warning(f"CHAIN REJECTED: underlying={symbol} reason=chain_not_fresh_provider_response retrieval={retrieval}")
        raise ValueError("chain_not_fresh_provider_response")
    if str(chain.get("exchange") or "NSE").upper() != "NSE":
        raise ValueError("chain_exchange_mismatch")
    result = deepcopy(dict(chain))
    result.update(symbol=symbol, exchange="NSE", by_expiry={}, expiries=[])
    rejected = Counter()
    for expiry_text in chain.get("expiries", []) or []:
        expiry = _expiry(expiry_text)
        rows = (chain.get("by_expiry", {}) or {}).get(expiry_text, []) or []
        accepted = []
        for row in rows:
            if not isinstance(row, Mapping):
                rejected["invalid_chain_row"] += 1
                continue
            if (_expiry(row.get("expiry", expiry_text)) != expiry or
                    _symbol(row.get("underlying_symbol") or symbol) != symbol or
                    str(row.get("exchange") or "NSE").upper() != "NSE"):
                rejected["chain_row_identity_mismatch"] += 1
                continue
            strike = planner._finite(row.get("strike"))
            copied = {key: deepcopy(value) for key, value in row.items()
                      if key not in {"CE", "PE", "lot_size", "lotSize"}}
            copied.update(underlying_symbol=symbol, exchange="NSE", expiry=expiry_text)
            for kind in ("CE", "PE"):
                quote = row.get(kind)
                if not quote:
                    continue
                metadata = contracts.get((symbol, expiry, strike, kind))
                if (not isinstance(quote, Mapping) or metadata is None or
                        _symbol(quote.get("underlying_symbol") or quote.get("underlying") or symbol) != symbol or
                        (quote.get("expiry") and _expiry(quote["expiry"]) != expiry) or
                        (quote.get("option_type") and quote["option_type"] != kind) or
                        (quote.get("strike") is not None and planner._finite(quote["strike"]) != strike) or
                        str(quote.get("exchange") or "NSE").upper() != "NSE"):
                    rejected["quote_without_exact_current_metadata"] += 1
                    continue
                copied[kind] = {**deepcopy(dict(quote)), **deepcopy(metadata),
                                "expiry": expiry_text, "lotSize": metadata["lot_size"]}
            if copied.get("CE") or copied.get("PE"):
                accepted.append(copied)
        if accepted:
            result["expiries"].append(expiry_text)
            result["by_expiry"][expiry_text] = accepted
    if not result["by_expiry"]:
        raise ValueError("no_quotes_with_exact_current_metadata")
    return result, rejected


def _contract_key(candidate: dict, symbol: str, contracts: dict) -> tuple:
    identities = [candidate.get(field) for field in
                  ("symbol", "underlying", "signal_underlying", "option_underlying")]
    legs = candidate.get("option_legs") or {}
    identities.append(legs.get("underlying"))
    if not any(identities) or any(_symbol(value) != symbol for value in identities if value):
        raise ValueError("planner_candidate_underlying_mismatch")
    if any(str(value).upper() != "NSE" for value in
           (candidate.get("exchange", "NSE"), legs.get("exchange", "NSE"))):
        raise ValueError("planner_candidate_exchange_mismatch")
    keys = []
    for leg in legs.get("legs", []) or []:
        expiry = _expiry(leg.get("expiry") or legs.get("expiry"))
        strike = planner._finite(leg.get("strike"))
        kind = str(leg.get("kind") or leg.get("option_type") or "").upper()
        metadata = contracts.get((symbol, expiry, strike, kind))
        lot = planner._finite(legs.get("lot_size"))
        if metadata is None or lot != metadata["lot_size"]:
            raise ValueError("planner_candidate_not_exact_current_contract")
        keys.append((expiry.isoformat(), strike, kind, str(leg.get("side") or "BUY")))
    if not keys:
        raise ValueError("planner_candidate_has_no_contract")
    return symbol, tuple(sorted(keys))


def build_live_options_scan(cfg, now=None, instrument_rows=None, market_data=None,
                            chain_provider=None) -> dict:
    """Discover every eligible current-session candidate without execution.

    Inject ``instrument_rows`` as MegaBull/normalized Zerodha rows (or a
    zero-argument callable), ``market_data`` as a mapping keyed by market or
    underlying symbol (or a batch callable), and ``chain_provider`` as a
    ``fetch_chain(symbol, use_cache=False)`` callable/object. Injected failures
    and missing symbols are terminal for that input: there is no fallback to
    a disk plan, another underlying, or another provider.

    The one exception is log-only: when every signal on an underlying belongs
    to a declared ``shadow_strategies`` sleeve and the live chain fails, the
    provider is retried with ``use_cache=True``. A same-session cached chain
    may supply strike/expiry identity for the shadow record, which is marked
    ``chain_source="cache"``. It can never enter the tradable pool, and a
    cache from a previous session is rejected.

    Diagnostic stage counts are per underlying, except ``selected`` (unique
    candidates) and ``fetch_errors`` (provider/missing-data errors). Rejections
    preserve individual reasons, including metadata and planner diagnostics.
    """
    from src.options_intraday import load_options_intraday_config

    effective = load_options_intraday_config(cfg or {})
    current = planner._as_now(now)
    started = time.monotonic()

    def asof():
        # A broad pass can span multiple bar completions. Compare quotes to
        # observation time, not the frozen time when the batch request began.
        return current + timedelta(seconds=time.monotonic() - started)
    universe = _universe(effective)
    diagnostics = {key: 0 for key in ("requested", "data_ok", "no_signal", "not_listed",
                                       "contract_rejection", "fetch_errors", "selected")}
    diagnostics.update(requested=len(universe), rejections=[], per_underlying={})
    result = {"snapshots": [], "diagnostics": diagnostics, "approved_underlyings": [],
              "shadow": []}
    if not universe:
        return result

    def reject(symbol, stage, reason, details=None):
        entry = {"underlying": symbol, "stage": stage, "reason": str(reason)}
        if details is not None:
            entry["details"] = details
        diagnostics["rejections"].append(entry)

    symbols = [row["market_symbol"] for row in universe]
    try:
        data = (fetch_multiple(symbols, timeframe="5m", period="10d") if market_data is None
                else market_data(symbols, timeframe="5m", period="10d") if callable(market_data)
                else market_data)
        if not isinstance(data, Mapping):
            raise ValueError("market_data_not_a_symbol_mapping")
    except Exception as exc:
        diagnostics["fetch_errors"] += 1
        reject(None, "candles", exc)
        data = {}
    try:
        rows = (option_instruments() if instrument_rows is None else
                instrument_rows() if callable(instrument_rows) else instrument_rows)
        contracts = _metadata(rows, universe, current, diagnostics)
    except Exception as exc:
        diagnostics["fetch_errors"] += 1
        reject(None, "metadata", exc)
        contracts = {}
    approved = {key[0] for key in contracts}
    result["approved_underlyings"] = [row["symbol"] for row in universe if row["symbol"] in approved]
    strategies = planner._strategies(effective)
    mode = effective.get("options_intraday", {}) or {}
    shadow_strategies = _shadow_strategy_names(mode)
    try:
        orb_confirm_closes = max(1, int(mode.get("orb_confirm_closes") or 1))
    except (TypeError, ValueError):
        orb_confirm_closes = 1
    shadow: List[Dict[str, Any]] = result["shadow"]
    max_age = planner._cfg_float(mode, ("max_candle_age_minutes", "data_max_age_minutes"), 15.0)
    provider = chain_provider if chain_provider is not None else fetch_chain
    if not callable(provider):
        provider = provider.fetch_chain
    ranked = []
    plans = {}
    for instrument in universe:
        symbol = instrument["symbol"]
        state = {"listed": symbol in approved, "market_symbol": instrument["market_symbol"]}
        diagnostics["per_underlying"][symbol] = state
        if symbol not in approved:
            diagnostics["not_listed"] += 1
            reject(symbol, "metadata", "not_option_listed_with_current_metadata")
        supplied = data.get(instrument["market_symbol"], data.get(symbol))
        if supplied is None:
            diagnostics["fetch_errors"] += 1
            reject(symbol, "candles", "missing_market_data")
            continue
        try:
            candles, candle_diag = planner._normalise_candles(
                _candles(supplied, instrument), asof(), max_age_minutes=max_age,
                require_orb_opening_range="orb" in strategies,
                expected_underlying=symbol,
                expected_instrument_type=instrument["instrument_type"],
            )
        except Exception as exc:
            reject(symbol, "candles", exc)
            continue
        state["data"] = candle_diag
        if not candles or not candle_diag.get("valid"):
            reject(symbol, "candles", "invalid_or_stale_intraday_data", candle_diag)
            continue
        diagnostics["data_ok"] += 1
        if symbol not in approved:
            continue
        symbol_cfg = _symbol_cfg(effective, instrument, contracts, current)
        specs = symbol_cfg["options_intraday"]["contract_specs"].get(symbol, {})
        step = symbol_cfg["options_intraday"].get("strike_step")
        if step is not None:
            specs = {**specs, "strike_step": step, "strike_interval": step}
        signals = []
        signaled_strategies = set()
        state["strategies"] = {}
        for strategy in strategies:
            if strategy == "pullback" and any(row.get("volume") is None for row in candles):
                state["strategies"][strategy] = {"status": "rejected", "reason": "volume_missing"}
                continue
            try:
                generated = CandidatePipeline.evaluate_strategy_signals_pure(
                    candles, strategy, underlying_symbol=symbol, contract_specs=specs,
                    index=len(candles) - 1, orb_confirm_closes=orb_confirm_closes,
                )
                signals.extend(generated)
                if generated:
                    signaled_strategies.add(_strategy_key(strategy))
                state["strategies"][strategy] = {"status": "signal" if generated else "no_signal"}
            except Exception as exc:
                state["strategies"][strategy] = {"status": "rejected", "reason": str(exc)}
                reject(symbol, "signal", exc)
        if not signals:
            diagnostics["no_signal"] += 1
            continue
        # A cached chain may only ever support log-only sleeves.  Require every
        # signal that fired on this underlying to be a declared shadow strategy
        # before the cache fallback below is allowed to run.
        signals_are_shadow_only = bool(shadow_strategies) and (
            signaled_strategies <= shadow_strategies)
        chain = None
        chain_error = None
        chain_source = "live"
        try:
            chain = provider(symbol, use_cache=False)
        except Exception as exc:
            chain_error = exc
        if chain is None and signals_are_shadow_only:
            # Shadow records need contract identity (strike/expiry), not a
            # tradable quote.  Retry the provider's cache path only after the
            # live chain failed, and accept it only if it is same-session.
            try:
                cached = provider(symbol, use_cache=True)
            except Exception as exc:
                cached = None
                chain_error = chain_error or exc
            if cached is not None:
                retrieval = cached.get("retrieval") or {}
                is_cache = bool(retrieval.get("served_from_cache")
                                or retrieval.get("cache_fallback"))
                if is_cache and not _same_session_chain(cached, symbol, current):
                    diagnostics["stale_chain_cache"] = (
                        diagnostics.get("stale_chain_cache", 0) + 1)
                    state["stale_chain_cache"] = True
                else:
                    chain = cached
                    chain_source = "cache" if is_cache else "live"
        if chain is None:
            diagnostics["fetch_errors"] += 1
            reject(symbol, "chain", chain_error or "chain_unavailable")
            continue
        state["chain_source"] = chain_source
        chain_provider = chain.get("provider") or chain.get("source")
        if chain_provider:
            state["chain_provider"] = chain_provider
            retrieval = chain.get("retrieval") or {}
            if retrieval.get("fallback_reason"):
                state["chain_fallback_reason"] = retrieval["fallback_reason"]
        plan_cfg = symbol_cfg
        if chain_source == "cache":
            # The cached payload supplies identity, not a tradable quote.
            # Relax the planner's quote-age gate for this shadow-only call;
            # every candidate is stripped before ranking below.
            plan_cfg = deepcopy(symbol_cfg)
            plan_mode = plan_cfg.setdefault("options_intraday", {})
            plan_mode["max_quote_age_minutes"] = max(
                planner._cfg_float(plan_mode, ("max_quote_age_minutes",), 15.0),
                24 * 60.0,
            )
        try:
            enriched, rejected_quotes = _enrich_chain(
                chain, symbol, contracts, allow_cached=chain_source == "cache")
            state["rejected_quotes"] = dict(rejected_quotes)
            plan = planner.build_intraday_plan(plan_cfg, now=asof(), candles=candles, chain=enriched)
        except Exception as exc:
            diagnostics["contract_rejection"] += 1
            reject(symbol, "contract", exc)
            continue
        state["planner"] = deepcopy(plan.get("option_chain_diagnostic") or {})
        candidates = plan.get("option_candidates", []) or []

        # --- shadow sleeves: log-only, never tradable ---
        tradable = []
        shadow_count = 0
        if shadow_strategies:
            for candidate in candidates:
                if _candidate_strategy_key(candidate) in shadow_strategies:
                    shadow_count += 1
                    shadow.append(_shadow_record(
                        candidate, symbol, candles, asof().isoformat(), contracts,
                        chain_source=chain_source))
                else:
                    tradable.append(candidate)
            candidates = tradable
        if chain_source == "cache" and candidates:
            # Defensive: a cached chain must never reach the ranked pool, even
            # if the planner returned a candidate outside the shadow set.
            diagnostics["cached_chain_tradable_rejected"] = (
                diagnostics.get("cached_chain_tradable_rejected", 0) + len(candidates))
            reject(symbol, "chain", "cached_chain_tradable_rejected")
            continue
        if shadow_count:
            state["shadow_signals"] = shadow_count
            if not candidates:
                # Keep shadow-only signals out of the tradable diagnostics so
                # they cannot look like an ORB contract rejection.
                continue
        # ------------------------------------------------

        # --- VWAP trend filter ---
        kept_candidates = []
        for candidate in candidates:
            if _vwap_scoped_out(mode, candidate):
                # Paper-only scoping: this sleeve's own entry rules already
                # encode trend, so the shared VWAP gate is skipped for it.
                # The shadow record above still computes ``vwap_allows`` so
                # historical comparisons stay intact.
                candidate["vwap_filter"] = "vwap_filter_scoped_out"
                kept_candidates.append(candidate)
                continue
            allowed, why = _vwap_allows(candidate, candles, symbol)
            if allowed:
                kept_candidates.append(candidate)
            else:
                logger.warning(f"CONTRACT REJECTED (VWAP Filter): underlying={symbol} reason={why}")
                reject(symbol, "contract", why)
        candidates = kept_candidates
        # -------------------------

        accepted = 0
        for candidate in candidates:
            try:
                key = _contract_key(candidate, symbol, contracts)
                score = planner._finite(candidate.get("rank_score"))
                if score is None:
                    raise ValueError("invalid_rank_score")
            except (TypeError, ValueError) as exc:
                logger.warning(f"CONTRACT REJECTED (Validation): underlying={symbol} reason={exc}")
                reject(symbol, "contract", exc)
                continue
            ranked.append((score, key, symbol, {
                **deepcopy(candidate),
                "judge_snapshot": _judge_snapshot(candidate, symbol, candles),
            }))
            accepted += 1
        if not accepted:
            diagnostics["contract_rejection"] += 1
            reason = state["planner"].get("failure_reason", "no_eligible_candidate")
            logger.warning(f"CONTRACT REJECTED (Planner/Selection): underlying={symbol} reason={reason} diagnostics={state['planner']}")
            reject(symbol, "contract", reason, state["planner"])
            continue
        plans[symbol] = deepcopy(plan)

    seen = set()
    selected = {}
    for score, key, symbol, candidate in sorted(ranked, key=lambda row: (-row[0], row[1])):
        if key in seen:
            diagnostics["duplicate_contracts"] = diagnostics.get("duplicate_contracts", 0) + 1
            continue
        seen.add(key)
        candidate["rank"] = len(seen)
        candidate["option_legs"]["rank"] = len(seen)
        selected.setdefault(symbol, []).append(candidate)
    # Dict insertion order follows the globally ranked pool, preserving each
    # snapshot as one underlying while carrying global ranks on every candidate.
    for symbol, candidates in selected.items():
        plan = plans[symbol]
        plan.update(option_candidates=candidates, actionable=len(candidates))
        result["snapshots"].append(plan)
    diagnostics["selected"] = len(seen)
    for record in shadow:
        contract_key = record.pop("_contract_key", None)
        record["collides_with_tradable"] = bool(
            contract_key is not None and contract_key in seen)
    diagnostics["shadow_signals"] = len(shadow)
    diagnostics["shadow_vwap_blocked"] = sum(
        1 for record in shadow if record.get("vwap_would_block"))
    diagnostics["shadow_contract_collisions"] = sum(
        1 for record in shadow if record.get("collides_with_tradable"))
    diagnostics["shadow_cached_chains"] = sum(
        1 for record in shadow if record.get("chain_cached"))
    return result


__all__ = ["build_live_options_scan", "resolve_scan_universe"]
