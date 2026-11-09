"""
TRIO — Zerodha Kite NFO options broker (live execution)
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

A deliberately small, strict adapter for ONE long CE/PE leg at a time.  It
mirrors the interface the options-intraday runtime already uses for
``MegaBullOptionsBroker`` (open_spread / close_spread / _close_remote /
refresh_positions / instrument_for / option_instruments), but sends real
orders to Zerodha Kite on the NFO segment.

Safety properties:
  * only one BUY CE/PE leg per order and no short legs;
  * an order is reported FILLED only after Kite says COMPLETE and a real
    average fill price is available;
  * a partial entry is cancelled and the filled quantity is unwound before a
    rejection is returned, so the runtime never records a position it does
    not own;
  * an exit is reported closed only when a fresh position mirror shows zero.

DISCLAIMER: Educational purposes only. Not financial advice.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import time
import uuid
from typing import Any, Dict, List, Optional

from src.broker.base import BaseBroker, BrokerOrder, Position
from src.broker.megabull import ASSET_OPTIONS
from src.utils import get_logger, utc_now

logger = get_logger("broker.zerodha_options")

IST = timezone(timedelta(hours=5, minutes=30))

OPTION_EXCHANGE = "NFO"

_FILLED = {"COMPLETE", "FILLED", "EXECUTED"}
_DEAD = {"REJECTED", "CANCELLED", "CANCELLED AMO", "LAPSED", "EXPIRED"}
_OPEN_STATUSES = {
    "OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED", "MODIFY PENDING",
    "PUT ORDER REQ RECEIVED", "VALIDATION PENDING", "REQUESTED",
}


def _status_map(raw: Any) -> str:
    value = str(raw or "").strip().upper()
    if value in _FILLED:
        return "FILLED"
    if value in _DEAD:
        return value
    if value in _OPEN_STATUSES:
        return "PENDING"
    return value or "UNKNOWN"


def _as_expiry_text(value: Any) -> str:
    """Normalise a Kite expiry (date/datetime/string) to DD-Mon-YYYY."""
    if isinstance(value, (datetime, date)):
        return value.strftime("%d-%b-%Y")
    text = str(value or "").strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(text[:19] if "T" in text else text[:10],
                                     fmt).strftime("%d-%b-%Y")
        except ValueError:
            continue
    return text


class ZerodhaOptionsBroker(BaseBroker):
    """Strict live NFO options adapter built on an authenticated Kite client."""

    provider_name = "zerodha_options"
    strict_options_only = True

    def __init__(
        self,
        market_data: Any,
        *,
        product: str = "NRML",
        initial_capital: float = 0.0,
        order_timeout_seconds: float = 8.0,
        exit_timeout_seconds: float = 5.0,
        poll_seconds: float = 0.5,
        slippage_pct: float = 0.02,
        market_protection: int = -1,
        dry_run: bool = False,
        tag: str = "TRIO-MUHURAT",
        underlyings: Optional[List[str]] = None,
    ):
        self.market_data = market_data
        self.kite = getattr(market_data, "kite", None)
        if self.kite is None:
            raise RuntimeError("ZerodhaOptionsBroker needs an authenticated kite client")
        self.product = str(product or "NRML").strip().upper()
        if self.product not in {"NRML", "MIS"}:
            raise ValueError(f"unsupported Zerodha product: {product!r}")
        self.initial_capital = float(initial_capital or 0.0)
        self.capital = self.initial_capital
        self.order_timeout_seconds = float(order_timeout_seconds)
        self.exit_timeout_seconds = float(exit_timeout_seconds)
        self.poll_seconds = max(0.0, float(poll_seconds))
        self.slippage_pct = max(0.0, float(slippage_pct))
        self.market_protection = int(market_protection)
        self.dry_run = bool(dry_run)
        self.tag = str(tag)[:20] or "TRIO"
        self.underlyings = {str(item).strip().upper()
                            for item in (underlyings or []) if str(item).strip()}

        self.orders: Dict[str, BrokerOrder] = {}
        self.positions: Dict[str, Position] = {}
        self.spreads: Dict[str, Dict[str, Any]] = {}
        self.closed_trades: List[Dict[str, Any]] = []
        # Kite's position mirror can lag a completed fill by a second or two.
        # Remember symbols we have just confirmed closed so a refresh cannot
        # resurrect them and trigger a second sell.
        self._recent_closes: Dict[str, float] = {}

        self._option_rows: List[Dict[str, Any]] = []
        self._by_symbol: Dict[str, Dict[str, Any]] = {}
        self._load_instruments()

    # ------------------------------------------------------------------
    # Instruments
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_row(raw: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(raw, dict):
            return None
        symbol = str(raw.get("tradingsymbol") or raw.get("trading_symbol") or "").strip()
        kind = str(raw.get("instrument_type") or "").strip().upper()
        if not symbol or kind not in {"CE", "PE"}:
            return None
        try:
            lot = int(float(raw.get("lot_size") or 0))
        except (TypeError, ValueError, OverflowError):
            lot = 0
        if lot <= 0:
            return None
        try:
            strike = float(raw.get("strike") or 0.0)
        except (TypeError, ValueError, OverflowError):
            strike = 0.0
        if strike <= 0:
            return None
        name = str(raw.get("name") or "").strip()
        return {
            "tradingsymbol": symbol,
            "trading_symbol": symbol,
            "instrument_token": raw.get("instrument_token"),
            "instrumentToken": raw.get("instrument_token"),
            # The rest of TRIO labels index options with the underlying
            # exchange (NSE); the order adapter still routes to NFO.
            "exchange": "NSE",
            "segment": "NFO-OPT",
            "instrument_type": kind,
            "instrumentType": "OPTIDX",
            "name": name,
            "underlying": name,
            "expiry": _as_expiry_text(raw.get("expiry")),
            "strike": strike,
            "lot_size": lot,
            "lotSize": lot,
            "tick_size": float(raw.get("tick_size") or 0.05),
            "freeze_quantity": int(raw.get("freeze_quantity") or 0) or None,
        }

    def _load_instruments(self) -> None:
        rows = self.market_data.instruments(OPTION_EXCHANGE) or []
        options: List[Dict[str, Any]] = []
        by_symbol: Dict[str, Dict[str, Any]] = {}
        for raw in rows:
            row = self._normalise_row(raw)
            if row is None:
                continue
            if self.underlyings and str(row.get("name") or "").upper() not in self.underlyings:
                continue
            options.append(row)
            by_symbol[row["tradingsymbol"].upper()] = row
        self._option_rows = options
        self._by_symbol = by_symbol
        logger.info("Zerodha options instruments loaded: %d", len(options))

    def option_instruments(self) -> List[Dict[str, Any]]:
        return list(self._option_rows)

    def instrument_for(self, symbol: str) -> Dict[str, Any]:
        row = self._by_symbol.get(str(symbol or "").strip().upper())
        if row is None:
            raise KeyError(f"Zerodha option instrument not found: {symbol}")
        return dict(row)

    def classify_position(self, symbol: Any, position: Any = None) -> str:
        """Every holding surfaced by this adapter is an NFO option."""
        return ASSET_OPTIONS

    # ------------------------------------------------------------------
    # Order primitives
    # ------------------------------------------------------------------

    def _place(
        self,
        *,
        side: str,
        symbol: str,
        quantity: int,
        order_type: str = "LIMIT",
        price: Optional[float] = None,
        market_protection: Optional[int] = None,
    ) -> str:
        if self.dry_run:
            raise RuntimeError("dry-run: order not sent")
        params: Dict[str, Any] = {
            "variety": "regular",
            "exchange": OPTION_EXCHANGE,
            "tradingsymbol": symbol,
            "transaction_type": str(side).upper(),
            "quantity": int(quantity),
            "product": self.product,
            "order_type": str(order_type).upper(),
            "validity": "DAY",
            "tag": self.tag,
        }
        if str(order_type).upper() == "LIMIT":
            if price is None or float(price) <= 0:
                raise ValueError("a positive limit price is required")
            params["price"] = round(float(price), 2)
        elif market_protection is not None:
            params["market_protection"] = int(market_protection)
        return str(self.kite.place_order(**params))

    def _order_row(self, order_id: str) -> Dict[str, Any]:
        try:
            history = self.kite.order_history(order_id) or []
        except Exception as exc:
            logger.warning("order_history(%s) failed: %s", order_id, exc)
            return {}
        return dict(history[-1]) if history else {}

    def _await(self, order_id: str, timeout: Optional[float] = None) -> Dict[str, Any]:
        deadline = time.monotonic() + float(
            self.order_timeout_seconds if timeout is None else timeout)
        row: Dict[str, Any] = {}
        while True:
            row = self._order_row(order_id)
            status = str(row.get("status") or "").upper()
            if status in _FILLED or status in _DEAD:
                return row
            if time.monotonic() >= deadline:
                return row
            if self.poll_seconds:
                time.sleep(self.poll_seconds)

    def _cancel_quiet(self, order_id: str) -> None:
        try:
            self.kite.cancel_order(variety="regular", order_id=order_id)
        except Exception as exc:
            logger.warning("cancel_order(%s) failed: %s", order_id, exc)

    @staticmethod
    def _round_tick(price: float, tick: float = 0.05) -> float:
        tick = tick if tick and tick > 0 else 0.05
        return round(round(float(price) / tick) * tick, 2)

    def _quote_price(self, row: Dict[str, Any], field: str) -> float:
        try:
            quote = self.market_data.option_quote(row) or {}
        except Exception as exc:
            logger.warning("option quote %s failed: %s",
                           row.get("tradingsymbol"), exc)
            return 0.0
        value = quote.get(field)
        try:
            number = float(value or 0.0)
        except (TypeError, ValueError, OverflowError):
            number = 0.0
        if number <= 0 and field in {"bid", "ask"}:
            try:
                number = float(quote.get("last_price") or 0.0)
            except (TypeError, ValueError, OverflowError):
                number = 0.0
        return number

    # ------------------------------------------------------------------
    # Position mirror
    # ------------------------------------------------------------------

    def refresh_positions(self) -> bool:
        """Refresh the local mirror from Kite net NFO positions."""
        try:
            payload = self.kite.positions() or {}
        except Exception as exc:
            logger.error("Zerodha positions unavailable: %s", exc)
            return False
        net = payload.get("net") if isinstance(payload, dict) else None
        if net is None:
            return False
        positions: Dict[str, Position] = {}
        now = time.monotonic()
        for raw in net:
            if not isinstance(raw, dict):
                continue
            if str(raw.get("exchange") or "").strip().upper() != OPTION_EXCHANGE:
                continue
            try:
                quantity = int(float(raw.get("quantity") or 0))
            except (TypeError, ValueError, OverflowError):
                quantity = 0
            if quantity == 0:
                continue
            symbol = str(raw.get("tradingsymbol") or "").strip()
            if not symbol:
                continue
            closed_at = self._recent_closes.get(symbol)
            if closed_at is not None and now - closed_at < 5.0:
                continue
            try:
                avg = float(raw.get("average_price") or 0.0)
                last = float(raw.get("last_price") or 0.0)
                pnl = float(raw.get("pnl") or 0.0)
            except (TypeError, ValueError, OverflowError):
                avg = last = pnl = 0.0
            positions[symbol] = Position(
                symbol=symbol,
                side="LONG" if quantity > 0 else "SHORT",
                quantity=abs(quantity),
                avg_price=avg,
                current_price=last,
                pnl=pnl,
            )
        self.positions = positions
        return True

    def _symbol_quantity(self, symbol: str) -> int:
        position = self.positions.get(str(symbol or "").strip())
        if position is None:
            return 0
        return int(position.quantity) if str(position.side).upper() == "LONG" \
            else -int(position.quantity)

    # ------------------------------------------------------------------
    # Spread lifecycle (runtime contract)
    # ------------------------------------------------------------------

    def open_spread(self, candidate: Any, lots: int = 1,
                    reason: str = "options-plan") -> BrokerOrder:
        try:
            payload = candidate.to_dict() if hasattr(candidate, "to_dict") \
                else dict(candidate or {})
        except Exception as exc:
            return self._reject({}, lots, f"bad candidate: {exc}")

        legs = payload.get("legs") or []
        if len(legs) != 1 or not isinstance(legs[0], dict):
            return self._reject(payload, lots, "strict mode requires exactly one leg")
        leg = legs[0]
        if str(leg.get("side") or "").upper() != "BUY":
            return self._reject(payload, lots, "strict mode requires a BUY leg")
        kind = str(leg.get("kind") or "").upper()
        strategy = str(payload.get("strategy") or "").strip().lower()
        if kind not in {"CE", "PE"}:
            return self._reject(payload, lots, "strict mode requires CE/PE")
        if strategy in {"long-call", "long-put"}:
            wanted = "CE" if strategy == "long-call" else "PE"
            if kind != wanted:
                return self._reject(payload, lots,
                                    f"{strategy} requires {wanted}, got {kind}")

        try:
            row = self._resolve_contract(payload, leg)
        except Exception as exc:
            return self._reject(payload, lots, f"instrument unresolved: {exc}")

        try:
            lots = max(1, int(lots or 1))
        except (TypeError, ValueError, OverflowError):
            return self._reject(payload, lots, "invalid lot count")
        lot_size = int(row.get("lot_size") or 0)
        units = lots * lot_size
        freeze = row.get("freeze_quantity")
        if freeze and units > int(freeze):
            return self._reject(
                payload, lots, f"quantity {units} exceeds freeze {freeze}")

        symbol = row["tradingsymbol"]
        sid = str(payload.get("trading_symbol") or symbol).strip() or symbol
        if sid in self.spreads:
            return self._reject(payload, lots, "already holding this spread")

        price = float(leg.get("ask") or 0.0) or float(leg.get("premium") or 0.0)
        if price <= 0:
            price = self._quote_price(row, "ask")
        if price <= 0:
            return self._reject(payload, lots, "no executable option ask")
        limit = self._round_tick(price * (1.0 + self.slippage_pct),
                                 float(row.get("tick_size") or 0.05))

        if self.dry_run:
            logger.info("[dry-run] BUY %s x%d @ %.2f (%s)",
                        symbol, units, limit, reason)
            return BrokerOrder(
                order_id=f"zopt_dry_{uuid.uuid4().hex[:8]}", symbol=sid,
                side="BUY", quantity=units, order_type="LIMIT", price=limit,
                status="REJECTED", broker="zerodha_options",
                placed_at=utc_now(), error="dry-run: order not sent")

        try:
            order_id = self._place(side="BUY", symbol=symbol, quantity=units,
                                   order_type="LIMIT", price=limit)
        except Exception as exc:
            return self._reject(payload, lots, f"entry order failed: {exc}")

        state = self._await(order_id)
        status = str(state.get("status") or "").upper()
        filled_qty = int(float(state.get("filled_quantity") or 0))
        average = float(state.get("average_price") or 0.0)

        if status not in _FILLED or filled_qty != units or average <= 0:
            self._cancel_quiet(order_id)
            if filled_qty > 0:
                logger.warning("entry %s partial (%d/%d); unwinding",
                               symbol, filled_qty, units)
                self._flatten_symbol(symbol, "entry-partial-unwind")
            return self._reject(
                payload, lots,
                f"entry {status or 'UNKNOWN'} filled {filled_qty}/{units}")

        entry_price = round(average, 2)
        self.spreads[sid] = {
            "strategy": str(payload.get("strategy") or ""),
            "expiry": str(payload.get("expiry") or ""),
            "underlying": str(payload.get("underlying") or row.get("name") or ""),
            "legs": [{
                "symbol": symbol,
                "side": "BUY",
                "lots": lots,
                "units": units,
                "price": entry_price,
                "order_id": order_id,
            }],
            "lot_size": lot_size,
            "net_debit": entry_price,
            "lots": lots,
            "maxLoss": round(entry_price * units, 2),
            "maxGain": 0.0,
            "breakeven": entry_price,
        }
        order = BrokerOrder(
            order_id=order_id, symbol=sid, side="BUY", quantity=units,
            order_type="LIMIT", price=entry_price, status="FILLED",
            broker="zerodha_options", placed_at=utc_now(), filled_at=utc_now())
        self.orders[order_id] = order
        # Give Kite's position mirror a moment to reflect the fill so the next
        # reconciliation does not briefly see an unmirrored owned position.
        for _ in range(8):
            try:
                self.refresh_positions()
            except Exception:
                break
            if self._symbol_quantity(symbol) != 0:
                break
            if self.poll_seconds:
                time.sleep(self.poll_seconds)
        logger.info("Zerodha options opened %s x%d @ %.2f (%s)",
                    symbol, units, entry_price, reason)
        return order

    def mark_spread(self, spread_id: str, mid: float) -> None:
        position = self.positions.get(spread_id)
        if position is None:
            return
        detail = self.spreads.get(spread_id, {})
        lot_size = int(detail.get("lot_size") or 1)
        position.current_price = float(mid)
        position.pnl = (float(mid) - position.avg_price) * position.quantity * lot_size
        if position.avg_price > 0:
            position.pnl_pct = (position.pnl /
                                (position.avg_price * position.quantity * lot_size)) * 100

    def close_spread(self, spread_id: str, mid: float,
                     reason: str = "close") -> Dict[str, Any]:
        """Sell the held leg and book it only after the mirror confirms flat."""
        detail = self.spreads.get(spread_id)
        if not detail:
            return {}
        legs = detail.get("legs") or []
        if not legs:
            return {}
        leg = legs[0]
        symbol = str(leg.get("symbol") or "")
        units = int(leg.get("units") or 0)
        if units <= 0:
            units = int(detail.get("lots") or 1) * int(detail.get("lot_size") or 0)
        if not symbol or units <= 0:
            return {}

        try:
            self.refresh_positions()
        except Exception:
            pass
        if self._symbol_quantity(symbol) == 0:
            # Already flat (a previous attempt filled): book it, do not resend.
            return self._book_close(spread_id, detail, float(mid), reason)

        price = float(mid or 0.0)
        if price <= 0:
            price = self._quote_price(self.instrument_for(symbol), "bid")
        limit = self._round_tick(
            price * (1.0 - self.slippage_pct),
            float(self.instrument_for(symbol).get("tick_size") or 0.05))

        if self.dry_run:
            return {}

        order_id = ""
        state: Dict[str, Any] = {}
        try:
            order_id = self._place(side="SELL", symbol=symbol, quantity=units,
                                   order_type="LIMIT", price=limit)
            state = self._await(order_id, timeout=self.exit_timeout_seconds)
        except Exception as exc:
            logger.warning("exit order failed for %s: %s", symbol, exc)

        total = int(float(state.get("filled_quantity") or 0))
        status = str(state.get("status") or "").upper()
        if total < units:
            if order_id and status not in _DEAD:
                self._cancel_quiet(order_id)
            # A cancel can lose a race with a fill; re-read before deciding
            # how much is still outstanding.
            if order_id:
                late = self._order_row(order_id)
                total = max(total, int(float(late.get("filled_quantity") or 0)))
            remaining = max(0, units - total)
            if remaining > 0:
                # Time-critical exits escalate to a protected market order.
                try:
                    order_id = self._place(
                        side="SELL", symbol=symbol, quantity=remaining,
                        order_type="MARKET",
                        market_protection=self.market_protection)
                    state = self._await(order_id, timeout=self.exit_timeout_seconds)
                    total += int(float(state.get("filled_quantity") or 0))
                except Exception as exc:
                    logger.error("market exit failed for %s: %s", symbol, exc)

        if total < units:
            logger.error("exit incomplete for %s: %d/%d filled", symbol, total, units)
            return {}

        self.orders[order_id] = BrokerOrder(
            order_id=order_id, symbol=symbol, side="SELL", quantity=total,
            order_type="MARKET", price=float(state.get("average_price") or limit),
            status="FILLED", broker="zerodha_options", placed_at=utc_now(),
            filled_at=utc_now())
        # The order itself is authoritative.  Suppress the lagging mirror for a
        # few seconds so confirmation cannot trigger a second sell.
        self._recent_closes[symbol] = time.monotonic()
        try:
            self.refresh_positions()
        except Exception:
            pass
        return self._book_close(spread_id, detail, float(mid), reason)

    def _book_close(self, spread_id: str, detail: Dict[str, Any],
                    exit_price: float, reason: str) -> Dict[str, Any]:
        entry = float(detail.get("net_debit") or 0.0)
        lots = int(detail.get("lots") or 1)
        lot_size = int(detail.get("lot_size") or 1)
        exit_value = float(exit_price or 0.0)
        if exit_value <= 0:
            exit_value = entry
        gross = (exit_value - entry) * lots * lot_size
        cost = 0.0
        try:
            from src.options_pricing import round_trip_cost as _rtc
            cost = _rtc(detail.get("strategy", ""), entry, exit_value,
                        lots, lot_size)
        except Exception:
            cost = 0.0
        pnl = round(gross - cost, 2)
        record = {
            "symbol": spread_id,
            "side": "LONG",
            "quantity": lots,
            "entry": round(entry, 2),
            "exit": round(exit_value, 2),
            "pnl": pnl,
            "pnl_pct": round(pnl / (entry * lots * lot_size) * 100, 2)
            if entry and lots else 0.0,
            "reason": reason,
            "result": "PASS" if pnl > 0 else "FAIL",
            "closed_at": utc_now(),
            "asset": "options",
            "strategy": detail.get("strategy", ""),
            "cost": cost,
            "gross_pnl": round(gross, 2),
            "status": "FILLED",
        }
        self.closed_trades.append(record)
        self.spreads.pop(spread_id, None)
        return record

    def _flatten_symbol(self, symbol: str, reason: str) -> bool:
        """Flatten the whole net quantity of one symbol and confirm it."""
        try:
            self.refresh_positions()
        except Exception:
            pass
        net = self._symbol_quantity(symbol)
        if net == 0:
            # A partial entry fill can take a moment to appear in Kite's net
            # position mirror.  Do not report "flat" before giving it a
            # chance; that would strand the filled quantity.
            deadline = time.monotonic() + max(3.0, self.poll_seconds * 4)
            while net == 0 and time.monotonic() < deadline:
                if self.poll_seconds:
                    time.sleep(self.poll_seconds)
                try:
                    self.refresh_positions()
                except Exception:
                    break
                net = self._symbol_quantity(symbol)
        if net == 0:
            return True
        side = "SELL" if net > 0 else "BUY"
        units = abs(net)
        row = self.instrument_for(symbol)
        price = self._quote_price(row, "bid" if side == "SELL" else "ask")
        if price <= 0:
            price = float(row.get("strike") or 0.0) * 0.01
        if price <= 0:
            return False
        if self.dry_run:
            return False
        total = 0
        try:
            order_id = self._place(side=side, symbol=symbol, quantity=units,
                                   order_type="LIMIT",
                                   price=self._round_tick(price))
            state = self._await(order_id, timeout=self.exit_timeout_seconds)
            total = int(float(state.get("filled_quantity") or 0))
            if total < units:
                if str(state.get("status") or "").upper() not in _DEAD:
                    self._cancel_quiet(order_id)
                late = self._order_row(order_id)
                total = max(total, int(float(late.get("filled_quantity") or 0)))
                remaining = units - total
                if remaining > 0:
                    order_id = self._place(side=side, symbol=symbol,
                                           quantity=remaining,
                                           order_type="MARKET",
                                           market_protection=self.market_protection)
                    state = self._await(order_id, timeout=self.exit_timeout_seconds)
                    total += int(float(state.get("filled_quantity") or 0))
        except Exception as exc:
            logger.error("flatten %s failed: %s", symbol, exc)
            return False
        if total < units:
            logger.error("flatten incomplete for %s: %d/%d", symbol, total, units)
            return False
        self._recent_closes[symbol] = time.monotonic()
        try:
            self.refresh_positions()
        except Exception:
            pass
        return True

    def _close_remote(self, symbol: str, side: str, lots: int,
                      ref_price: float, reason: str) -> bool:
        """Runtime orphan-flatten hook: close by symbol, never by stale lots."""
        return self._flatten_symbol(str(symbol or "").strip(), reason)

    # ------------------------------------------------------------------
    # BaseBroker interface
    # ------------------------------------------------------------------

    def place_order(self, symbol: str, side: str, quantity: int,
                    order_type: str = "LIMIT", price: Optional[float] = None,
                    stop_loss: Optional[float] = None,
                    target: Optional[float] = None) -> BrokerOrder:
        row = self.instrument_for(symbol)
        try:
            order_id = self._place(
                side=side, symbol=row["tradingsymbol"], quantity=int(quantity),
                order_type=order_type, price=price)
        except Exception as exc:
            return BrokerOrder(symbol=symbol, side=side, quantity=int(quantity),
                               status="REJECTED", broker="zerodha_options",
                               placed_at=utc_now(), error=str(exc))
        state = self._await(order_id)
        return BrokerOrder(
            order_id=order_id, symbol=symbol, side=side,
            quantity=int(quantity), order_type=order_type,
            price=float(state.get("average_price") or price or 0.0),
            status=_status_map(state.get("status")), broker="zerodha_options",
            placed_at=utc_now(), error=str(state.get("status_message") or ""))

    def close_position(self, symbol: str, price: float = 0.0,
                       reason: str = "close", fraction: float = 1.0) -> BrokerOrder:
        ok = self._flatten_symbol(str(symbol or "").strip(), reason)
        return BrokerOrder(
            order_id=f"zopt_close_{uuid.uuid4().hex[:8]}", symbol=symbol,
            side="SELL", quantity=0, price=float(price or 0.0),
            status="FILLED" if ok else "REJECTED", broker="zerodha_options",
            placed_at=utc_now(), error="" if ok else "flatten not confirmed")

    def cancel_order(self, order_id: str) -> BrokerOrder:
        try:
            self.kite.cancel_order(variety="regular", order_id=order_id)
            return BrokerOrder(order_id=order_id, status="CANCELLED",
                               broker="zerodha_options", placed_at=utc_now())
        except Exception as exc:
            return BrokerOrder(order_id=order_id, status="ERROR",
                               broker="zerodha_options", error=str(exc),
                               placed_at=utc_now())

    def get_positions(self) -> List[Position]:
        return list(self.positions.values())

    def get_balance(self) -> Dict[str, float]:
        try:
            margins = self.kite.margins() or {}
        except Exception as exc:
            logger.error("Zerodha margins unavailable: %s", exc)
            return {"total": 0.0, "available": 0.0, "used_margin": 0.0}
        equity = margins.get("equity", {}) or {}
        available = equity.get("available", {}) or {}
        try:
            live = float(available.get("live_balance") or available.get("cash") or 0.0)
            used = float((equity.get("utilised", {}) or {}).get("debits") or 0.0)
            net = float(equity.get("net") or 0.0)
        except (TypeError, ValueError, OverflowError):
            live = used = net = 0.0
        return {"total": net or live, "available": live, "used_margin": used}

    def get_order_status(self, order_id: str) -> BrokerOrder:
        row = self._order_row(order_id)
        return BrokerOrder(
            order_id=order_id,
            symbol=str(row.get("tradingsymbol") or ""),
            side=str(row.get("transaction_type") or ""),
            quantity=int(float(row.get("quantity") or 0)),
            price=float(row.get("average_price") or row.get("price") or 0.0),
            status=_status_map(row.get("status")),
            broker="zerodha_options",
            error=str(row.get("status_message") or ""),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_contract(self, payload: Dict[str, Any],
                          leg: Dict[str, Any]) -> Dict[str, Any]:
        symbol = str(payload.get("trading_symbol") or leg.get("trading_symbol")
                     or "").strip()
        if symbol:
            return self.instrument_for(symbol)
        underlying = str(payload.get("underlying") or
                         payload.get("option_underlying") or "").strip().upper()
        expiry = str(payload.get("expiry") or leg.get("expiry") or "").strip()
        kind = str(leg.get("kind") or "").strip().upper()
        try:
            strike = float(leg.get("strike") or 0.0)
        except (TypeError, ValueError, OverflowError):
            strike = 0.0
        if not underlying or not expiry or kind not in {"CE", "PE"} or strike <= 0:
            raise KeyError("incomplete option identity")
        for row in self._option_rows:
            if row["instrument_type"] != kind:
                continue
            if str(row.get("name") or "").upper() != underlying:
                continue
            if abs(float(row["strike"]) - strike) > 1e-8:
                continue
            if _as_expiry_text(row.get("expiry")) != _as_expiry_text(expiry):
                continue
            return dict(row)
        raise KeyError(
            f"no NFO contract for {underlying} {expiry} {strike:.0f} {kind}")

    def _reject(self, payload: Dict[str, Any], lots: Any,
                error: str) -> BrokerOrder:
        sid = str((payload or {}).get("trading_symbol") or "?")
        order = BrokerOrder(
            order_id=f"zopt_rej_{uuid.uuid4().hex[:8]}", symbol=sid,
            side="BUY", quantity=int(lots or 0) if str(lots).isdigit() else 0,
            order_type="LIMIT", price=0.0, status="REJECTED",
            broker="zerodha_options", placed_at=utc_now(), error=str(error))
        logger.warning("Zerodha options REJECT %s: %s", sid, error)
        return order

    # ------------------------------------------------------------------
    # Operational flatten
    # ------------------------------------------------------------------

    def cancel_all_open_orders(self) -> List[str]:
        try:
            rows = self.kite.orders() or []
        except Exception as exc:
            logger.error("Zerodha orders unavailable: %s", exc)
            return []
        cancelled: List[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            # Never cancel an unrelated equity/commodity order from the
            # one-off options runner or its watchdog.
            if str(row.get("exchange") or "").strip().upper() != OPTION_EXCHANGE:
                continue
            status = str(row.get("status") or "").upper()
            if status not in _OPEN_STATUSES:
                continue
            order_id = str(row.get("order_id") or "")
            if not order_id:
                continue
            try:
                self.kite.cancel_order(variety="regular", order_id=order_id)
                cancelled.append(order_id)
            except Exception as exc:
                logger.warning("cancel %s failed: %s", order_id, exc)
        return cancelled

    def flatten_all(self, reason: str = "manual-flatten") -> Dict[str, Any]:
        cancelled = self.cancel_all_open_orders()
        # A failed mirror read must never be reported as "flat": the caller
        # would stop managing a position it cannot see.
        positions_ok = self.refresh_positions()
        if not positions_ok:
            return {
                "cancelled": cancelled,
                "closed": {},
                "remaining": sorted(self.positions),
                "positions_ok": False,
            }
        closed = {symbol: self._flatten_symbol(symbol, reason)
                  for symbol in list(self.positions)}
        final_ok = self.refresh_positions()
        return {
            "cancelled": cancelled,
            "closed": closed,
            "remaining": sorted(self.positions),
            "positions_ok": bool(final_ok),
        }


__all__ = ["ZerodhaOptionsBroker", "OPTION_EXCHANGE"]
