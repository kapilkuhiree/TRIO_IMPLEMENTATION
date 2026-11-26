"""TRIO — Shared base for long-volatility option structures.

The booklet defines payoff structures but not entry timing.  This module makes
that split explicit:

* trigger: a causal directional signal from ORB / pullback / momentum;
* structure: direction-neutral (bias ``ANY``) long-volatility legs;
* premium: never fabricated.  Signals are emitted from underlying candles
  alone, but ``execution_ready`` stays false until bid/ask quotes (or an
  explicitly supplied premium) are provided in ``contract_specs``.

Used by ``long_straddle_strategy.py`` and ``long_strangle_strategy.py``.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------------------
# SHARED DATA OBJECTS
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StructureLeg:
    """One option leg of a multi-leg structure."""

    kind: str  # "CE" | "PE"
    side: str = "BUY"  # long-volatility structures buy every leg
    offset: int = 0  # strike steps from ATM
    strike: float = 0.0
    premium: float = 0.0
    premium_source: str = "unavailable"  # quote_ask | provided_premium | unavailable
    expiry: str = "weekly"


@dataclass
class LongVolatilitySignalResult:
    """Signal result for a two-leg, direction-neutral option structure."""

    allowed: bool = False
    strategy: str = ""
    display_name: str = ""
    page: str = ""
    bias: str = "ANY"
    variant: str = "direction_neutral"
    signal_type: str = "none"  # "long_volatility" | "none"

    trigger_source: str = ""  # orb | pullback | momentum
    trigger_variant: str = ""
    trigger_signal_type: str = ""

    # Underlying spot at signal-bar close.  This is not an option premium.
    entry_price: float = 0.0
    trigger_price: float = 0.0
    trigger_time: str = ""

    expiry: str = "weekly"
    # Explicit multi-leg marker.  Do not treat this as a single-leg CE/PE type.
    option_type: str = "CE+PE"

    # A straddle/strangle has no directional stop/target on the underlying.
    # Its structural risk is the net debit paid for both legs.
    stop_loss: float = 0.0
    target: float = 0.0
    net_debit: float = 0.0
    max_loss: float = 0.0
    execution_ready: bool = False
    premium_source: str = "unavailable"

    legs: List[StructureLeg] = field(default_factory=list)
    option_legs: Dict[str, Any] = field(default_factory=dict)

    reasoning: str = ""
    bar_index: int = -1
    underlying_symbol: str = "NIFTY"


# ---------------------------------------------------------------------------
# SHARED STRUCTURE ENGINE
# ---------------------------------------------------------------------------


class LongVolatilityStructure:
    """Deterministic structure builder for ATM/OTM long-volatility plays.

    Subclasses only declare their identity and their legs.  Signal timing is
    delegated to the repository's existing causal signal engines so this
    module never re-implements their indicators or invents trade outcomes.
    """

    NAME = ""
    KEY = ""
    PAGE = ""
    DESCRIPTION = ""
    BIAS = "ANY"
    LEGS: Tuple[StructureLeg, ...] = ()

    ENTRY_CUTOFF = dtime(14, 30)
    SESSION_EXIT = dtime(15, 30)
    TRIGGER_STRATEGIES = ("orb", "pullback", "momentum")

    def __init__(
        self,
        lot_size: int = 65,
        fill_model: str = "bidask",
        trigger_strategies: Optional[Tuple[str, ...]] = None,
    ):
        self.lot_size = int(lot_size)
        self.fill_model = fill_model  # "mid" | "bidask"
        self.trigger_strategies = tuple(trigger_strategies or self.TRIGGER_STRATEGIES)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_timestamp(ts_str: str) -> Optional[datetime]:
        if not ts_str:
            return None
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                return ts.replace(tzinfo=IST)
            return ts.astimezone(IST)
        except (ValueError, AttributeError, TypeError):
            return None

    @staticmethod
    def _as_float(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _quote_lookup(
        quotes: Any,
        kind: str,
        strike: float,
        expiry: str,
    ) -> Any:
        """Find a quote without assuming a single chain representation."""
        if isinstance(quotes, list):
            for row in quotes:
                if not isinstance(row, dict):
                    continue
                row_kind = str(row.get("option_type") or row.get("kind") or "").upper()
                row_strike = LongVolatilityStructure._as_float(row.get("strike"))
                if row_kind != kind or row_strike is None:
                    continue
                if abs(row_strike - float(strike)) > 1e-6:
                    continue
                row_expiry = row.get("expiry")
                if row_expiry and expiry and str(row_expiry) != str(expiry):
                    continue
                return row
            return None

        if isinstance(quotes, dict):
            keys = (
                (kind, float(strike)),
                (kind, float(strike), str(expiry)),
                f"{kind}:{strike:g}",
                f"{kind}_{strike:g}",
                f"{kind}:{strike:g}:{expiry}",
                f"{kind}_{strike:g}_{expiry}",
            )
            for key in keys:
                if key in quotes:
                    return quotes[key]

            by_kind = quotes.get(kind)
            if isinstance(by_kind, dict):
                for key in (float(strike), f"{strike:g}", str(expiry)):
                    if key in by_kind:
                        return by_kind[key]
        return None

    @staticmethod
    def _premium_from_quote(quote: Any) -> Tuple[float, str]:
        """Use an executable ask first; accept an explicitly supplied premium.

        Closing premium is deliberately ignored: it is not an executable
        entry fill and must never be silently substituted for a quote.
        """
        if quote is None:
            return 0.0, "unavailable"
        if isinstance(quote, (int, float)) and not isinstance(quote, bool):
            premium = LongVolatilityStructure._as_float(quote)
            return (premium, "provided_premium") if premium is not None and premium >= 0 else (0.0, "unavailable")
        if not isinstance(quote, dict):
            return 0.0, "unavailable"

        ask = LongVolatilityStructure._as_float(quote.get("ask"))
        if ask is not None and ask >= 0:
            return ask, "quote_ask"

        premium = LongVolatilityStructure._as_float(quote.get("premium"))
        if premium is not None and premium >= 0:
            return premium, "provided_premium"

        return 0.0, "unavailable"

    def _breakout_trigger(
        self,
        candles_5min: List[Dict[str, Any]],
        index: int,
        underlying_symbol: str,
    ) -> Dict[str, Any]:
        """ORB-equivalent breakout computed with the safe IST parser.

        This mirrors the repo's 09:15-09:30 opening-range rule without relying
        on ``zoneinfo`` (tzdata is not available on every Windows install).
        """
        if index < 3 or index >= len(candles_5min):
            return {}

        current = candles_5min[index]
        ts = self._parse_timestamp(current.get("timestamp", ""))
        if ts is None or ts.time() < dtime(9, 30) or ts.time() > self.ENTRY_CUTOFF:
            return {}

        opening_times = {dtime(9, 15), dtime(9, 20), dtime(9, 25)}
        highs: List[float] = []
        lows: List[float] = []
        for candle in candles_5min[:index]:
            if (
                candle.get("underlying_symbol") != underlying_symbol
                or candle.get("exchange") != "NSE"
                or candle.get("instrument_type") != current.get("instrument_type")
            ):
                continue
            bar_ts = self._parse_timestamp(candle.get("timestamp", ""))
            if bar_ts is None or bar_ts.date() != ts.date():
                continue
            if bar_ts.time() not in opening_times:
                continue
            high = self._as_float(candle.get("high"))
            low = self._as_float(candle.get("low"))
            if high is not None and low is not None:
                highs.append(high)
                lows.append(low)

        if len(highs) < 3 or len(lows) < 3:
            return {}

        orb_high, orb_low = max(highs), min(lows)
        close = self._as_float(current.get("close"))
        if close is None:
            return {}

        if close > orb_high:
            return {
                "variant": "bullish",
                "signal_type": "breakout",
                "trigger_price": orb_high,
                "trigger_time": current.get("timestamp", ""),
                "expiry": "weekly",
            }
        if close < orb_low:
            return {
                "variant": "bearish",
                "signal_type": "breakdown",
                "trigger_price": orb_low,
                "trigger_time": current.get("timestamp", ""),
                "expiry": "weekly",
            }
        return {}

    def _causal_trigger(
        self,
        candles_5min: List[Dict[str, Any]],
        index: int,
        contract_specs: Dict[str, Any],
        underlying_symbol: str,
    ) -> Tuple[str, Dict[str, Any], List[str]]:
        """Return the first allowed causal signal from the configured engines."""
        from strategies.momentum_continuation_strategy import MomentumContinuationStrategy
        from strategies.trend_pullback_strategy import TrendPullbackStrategy

        factories = {
            "pullback": TrendPullbackStrategy,
            "momentum": MomentumContinuationStrategy,
        }
        lot_size = int(contract_specs.get("lot_size") or self.lot_size)
        errors: List[str] = []

        for name in self.trigger_strategies:
            key = str(name).strip().lower()
            if key == "orb":
                data = self._breakout_trigger(candles_5min, index, underlying_symbol)
                if data:
                    return "orb", data, errors
                continue

            factory = factories.get(key)
            if factory is None:
                errors.append(f"unknown trigger: {name}")
                continue
            try:
                engine = factory(lot_size=lot_size)
                signal = engine.evaluate_signal(
                    candles_5min,
                    index,
                    contract_specs,
                    underlying_symbol=underlying_symbol,
                )
            except Exception as exc:  # keep one bad engine from blocking the others
                errors.append(f"{name}: {exc}")
                continue

            data = signal if isinstance(signal, dict) else dict(getattr(signal, "__dict__", {}) or {})
            if bool(data.get("allowed")):
                return key, data, errors

        return "", {}, errors

    def _build_legs(
        self,
        spot: float,
        expiry: str,
        contract_specs: Dict[str, Any],
    ) -> Tuple[List[StructureLeg], float, bool, str]:
        step = self._as_float(
            contract_specs.get("strike_interval")
            if contract_specs.get("strike_interval") is not None
            else contract_specs.get("strike_step")
        )
        if step is None or step <= 0:
            return [], 0.0, False, "strike_interval unavailable"

        atm = math.floor((spot / step) + 0.5) * step
        quotes = contract_specs.get("option_quotes") or contract_specs.get("option_chain") or {}

        legs: List[StructureLeg] = []
        for template in self.LEGS:
            kind = str(template.kind).upper()
            strike = float(atm + int(template.offset) * step)
            quote = self._quote_lookup(quotes, kind, strike, expiry)
            premium, source = self._premium_from_quote(quote)
            legs.append(
                StructureLeg(
                    kind=kind,
                    side=str(template.side or "BUY").upper(),
                    offset=int(template.offset),
                    strike=strike,
                    premium=premium,
                    premium_source=source,
                    expiry=str(expiry),
                )
            )

        net_debit = float(sum(leg.premium for leg in legs))
        sources = sorted({leg.premium_source for leg in legs})
        execution_ready = bool(legs) and all(
            leg.premium_source in {"quote_ask", "provided_premium"} for leg in legs
        )
        return legs, net_debit, execution_ready, ",".join(sources)

    def _option_legs_payload(
        self,
        result: LongVolatilitySignalResult,
        contract_specs: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "strategy": self.KEY.replace("_", "-"),
            "structure": self.NAME,
            "page": self.PAGE,
            "bias": self.BIAS,
            "legs": [asdict(leg) for leg in result.legs],
            "net_debit": result.net_debit,
            "maxLoss": result.max_loss,
            "lot_size": int(contract_specs.get("lot_size") or self.lot_size),
            "premium_source": result.premium_source,
            "execution_ready": result.execution_ready,
            "signal_timestamp": result.trigger_time,
            "underlying_symbol": result.underlying_symbol,
        }

    # ------------------------------------------------------------------
    # Public signal boundary
    # ------------------------------------------------------------------

    def evaluate_signal(
        self,
        candles_5min: List[Dict[str, Any]],
        index: int,
        contract_specs: Dict[str, Any],
        underlying_symbol: str = "NIFTY",
    ) -> LongVolatilitySignalResult:
        """Evaluate one completed 5-minute bar.

        Returns a two-leg research signal.  ``entry_price`` is the underlying
        spot, not an option premium.  Option quotes are optional: without them
        the signal is emitted but ``execution_ready`` is false.
        """
        result = LongVolatilitySignalResult(
            strategy=self.KEY,
            display_name=self.NAME,
            page=self.PAGE,
            bias=self.BIAS,
            underlying_symbol=underlying_symbol,
        )

        try:
            if index < 0 or index >= len(candles_5min):
                return result

            candle = candles_5min[index]
            instrument_type = candle.get("instrument_type")
            if (
                not underlying_symbol
                or candle.get("underlying_symbol") != underlying_symbol
                or candle.get("exchange") != "NSE"
                or instrument_type not in {"index", "stock"}
            ):
                result.reasoning = "Candle does not belong to the requested NSE underlying"
                return result

            ts = self._parse_timestamp(candle.get("timestamp", ""))
            if ts is None:
                result.reasoning = "Malformed signal timestamp; entry rejected"
                return result
            if ts.time() > self.ENTRY_CUTOFF:
                result.reasoning = "Entry past configured cutoff; no new structure entry"
                return result

            spot = self._as_float(candle.get("close"))
            if spot is None or spot <= 0:
                result.reasoning = "Invalid underlying close; cannot derive ATM strike"
                return result

            specs = dict(contract_specs or {})
            trigger_name, trigger, errors = self._causal_trigger(
                candles_5min, index, specs, underlying_symbol
            )
            if not trigger_name:
                detail = "; ".join(errors) if errors else "no causal signal allowed"
                result.reasoning = f"{self.NAME}: {detail}"
                return result

            expiry = str(
                specs.get("expiry")
                or trigger.get("expiry")
                or specs.get("expiry_preference")
                or "weekly"
            )
            legs, net_debit, execution_ready, sources = self._build_legs(spot, expiry, specs)
            if not legs:
                result.reasoning = f"{self.NAME}: {sources}"
                return result

            lot_size = int(specs.get("lot_size") or self.lot_size)
            quantity = max(1, int(specs.get("quantity_per_leg") or 1))

            result.allowed = True
            result.signal_type = "long_volatility"
            result.trigger_source = trigger_name
            result.trigger_variant = str(trigger.get("variant") or "")
            result.trigger_signal_type = str(trigger.get("signal_type") or "")
            result.entry_price = spot
            result.trigger_price = self._as_float(trigger.get("trigger_price")) or spot
            result.trigger_time = str(
                trigger.get("trigger_time") or candle.get("timestamp") or ""
            )
            result.expiry = expiry
            result.net_debit = net_debit
            result.max_loss = net_debit * lot_size * quantity
            result.execution_ready = execution_ready
            result.premium_source = sources
            result.legs = legs
            result.option_legs = self._option_legs_payload(result, specs)
            result.bar_index = index

            step_value = self._as_float(specs.get("strike_interval"))
            if step_value is None:
                step_value = self._as_float(specs.get("strike_step")) or 0.0
            atm_strike = legs[0].strike - legs[0].offset * step_value
            result.reasoning = (
                f"{self.NAME}: {self.DESCRIPTION} Trigger={trigger_name} "
                f"({result.trigger_signal_type}/{result.trigger_variant}). "
                f"Spot {spot:.2f}, ATM {atm_strike:.2f}, "
                f"net debit {net_debit:.2f}, premium_source={sources or 'unavailable'}."
            )
        except (KeyError, ValueError, TypeError) as exc:
            result.reasoning = f"{self.NAME or 'Structure'} evaluation error: {exc}"

        return result


# ---------------------------------------------------------------------------
# RESEARCH BACKTEST HELPER
# ---------------------------------------------------------------------------


def run_structure_backtest(
    strategy_cls: Any,
    csv_path: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    lot_size: int = 65,
    initial_equity: float = 500000.0,
    contract_specs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Collect structure signals from underlying candles.

    Underlying OHLC alone is insufficient for option P&L.  This helper
    deliberately returns signals with a NOT_READY readiness marker instead of
    modelling premiums or inventing trade outcomes.
    """
    from data_importer import import_5min_index_candles
    from option_data_schema import NOT_READY_FOR_OPTIONS_PNL

    candles = import_5min_index_candles(
        csv_path, underlying_symbol="NIFTY", start_date=start_date, end_date=end_date
    )
    if not candles:
        return {"error": "No candles imported - check CSV path and format"}

    def _in_session(candle: Dict[str, Any]) -> bool:
        ts = LongVolatilityStructure._parse_timestamp(candle.get("timestamp", ""))
        return bool(ts and dtime(9, 15) <= ts.time() <= dtime(15, 30))

    candles = [c for c in candles if _in_session(c)]
    symbol = candles[0].get("underlying_symbol") or "NIFTY"

    specs = {
        "lot_size": int(lot_size),
        "expiry_preference": "weekly",
        "strike_interval": 50,
    }
    specs.update(contract_specs or {})

    strategy = strategy_cls(lot_size=lot_size)
    signals: List[Dict[str, Any]] = []
    for index, candle in enumerate(candles):
        signal = strategy.evaluate_signal(
            candles, index, specs, underlying_symbol=symbol
        )
        if signal.allowed:
            signals.append(asdict(signal))

    return {
        "strategy": getattr(strategy_cls, "NAME", ""),
        "underlying_symbol": symbol,
        "initial_equity": float(initial_equity),
        "total_signals": len(signals),
        "signals": signals,
        # Research signal extraction only; no P&L is claimed.
        "total_trades": 0,
        "total_pnl": 0.0,
        "option_pnl_readiness": NOT_READY_FOR_OPTIONS_PNL,
        "ready_for_options_pnl": False,
        "reason": (
            "Underlying candles only. Validated option-premium history with "
            "bid/ask is required before structure P&L can be computed."
        ),
    }
