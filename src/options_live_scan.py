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
import re
import time
from typing import Any

import yaml

from candidate_pipeline import CandidatePipeline
from src import options_intraday_plan as planner


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
    if mode.get("scan_underlyings") is not None:
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


def fetch_multiple(symbols, timeframe="5m", period="5d"):
    """Late-bound batch candle hook; injectable without changing global state."""
    from src.data_fetcher import fetch_multiple as fetch
    return fetch(symbols, timeframe=timeframe, period=period)


def option_instruments():
    """Read-only current MegaBull metadata hook (no token/order fallback)."""
    from src.broker.megabull import MegaBullBroker, load_dotenv_key
    import os
    key = os.environ.get("MEGABULL_API_KEY") or load_dotenv_key("MEGABULL_API_KEY") or "9bfc7b64-2aef-4271-b373-b31cd57e9fe8"
    os.environ["MEGABULL_API_KEY"] = key
    return MegaBullBroker(api_key=key).option_instruments()


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
        if not explicit and named_root in {*expected, *INDEX_MARKET_SYMBOLS}:
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


def _enrich_chain(chain: Any, symbol: str, contracts: dict) -> tuple[dict, Counter]:
    if not isinstance(chain, Mapping):
        raise ValueError("chain_unavailable_or_invalid")
    if _symbol(chain.get("symbol")) != symbol:
        raise ValueError("chain_underlying_mismatch")
    retrieval = {**((chain.get("diagnostics") or {}).get("retrieval") or {}),
                 **(chain.get("retrieval") or {})}
    if retrieval.get("served_from_cache") or retrieval.get("success") is False:
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
    result = {"snapshots": [], "diagnostics": diagnostics, "approved_underlyings": []}
    if not universe:
        return result

    def reject(symbol, stage, reason, details=None):
        entry = {"underlying": symbol, "stage": stage, "reason": str(reason)}
        if details is not None:
            entry["details"] = details
        diagnostics["rejections"].append(entry)

    symbols = [row["market_symbol"] for row in universe]
    try:
        data = (fetch_multiple(symbols, timeframe="5m", period="5d") if market_data is None
                else market_data(symbols, timeframe="5m", period="5d") if callable(market_data)
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
        state["strategies"] = {}
        for strategy in strategies:
            if strategy == "pullback" and any(row.get("volume") is None for row in candles):
                state["strategies"][strategy] = {"status": "rejected", "reason": "volume_missing"}
                continue
            try:
                generated = CandidatePipeline.evaluate_strategy_signals_pure(
                    candles, strategy, underlying_symbol=symbol, contract_specs=specs,
                    index=len(candles) - 1,
                )
                signals.extend(generated)
                state["strategies"][strategy] = {"status": "signal" if generated else "no_signal"}
            except Exception as exc:
                state["strategies"][strategy] = {"status": "rejected", "reason": str(exc)}
                reject(symbol, "signal", exc)
        if not signals:
            diagnostics["no_signal"] += 1
            continue
        try:
            chain = provider(symbol, use_cache=False)
        except Exception as exc:
            diagnostics["fetch_errors"] += 1
            reject(symbol, "chain", exc)
            continue
        if chain is None:
            diagnostics["fetch_errors"] += 1
            reject(symbol, "chain", "chain_unavailable")
            continue
        try:
            enriched, rejected_quotes = _enrich_chain(chain, symbol, contracts)
            state["rejected_quotes"] = dict(rejected_quotes)
            plan = planner.build_intraday_plan(symbol_cfg, now=asof(), candles=candles, chain=enriched)
        except Exception as exc:
            diagnostics["contract_rejection"] += 1
            reject(symbol, "contract", exc)
            continue
        state["planner"] = deepcopy(plan.get("option_chain_diagnostic") or {})
        candidates = plan.get("option_candidates", []) or []
        accepted = 0
        for candidate in candidates:
            try:
                key = _contract_key(candidate, symbol, contracts)
                score = planner._finite(candidate.get("rank_score"))
                if score is None:
                    raise ValueError("invalid_rank_score")
            except (TypeError, ValueError) as exc:
                reject(symbol, "contract", exc)
                continue
            ranked.append((score, key, symbol, deepcopy(candidate)))
            accepted += 1
        if not accepted:
            diagnostics["contract_rejection"] += 1
            reject(symbol, "contract", state["planner"].get("failure_reason", "no_eligible_candidate"),
                   state["planner"])
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
    return result


__all__ = ["build_live_options_scan", "resolve_scan_universe"]
