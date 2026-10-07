"""
TRIO — Paper Trading Broker Adapter
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

Simulates order placement and position management in memory.
This is the DEFAULT broker — no real orders are placed.

DISCLAIMER: Educational purposes only. Not financial advice.
"""

import logging
import uuid
from typing import Any, Dict, List, Optional

from src.broker.base import BaseBroker, BrokerOrder, Position
from src.utils import get_logger, utc_now

logger = get_logger("broker.paper")


class PaperBroker(BaseBroker):
    """
    Paper trading broker. All orders are simulated in memory.

    Usage:
        broker = PaperBroker(initial_capital=100000)
        order = broker.place_order("RELIANCE.NS", "BUY", 10, price=2845.0)
    Author: Kapil Kuhire
    """

    def __init__(self, initial_capital: float = 100000):
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.orders: Dict[str, BrokerOrder] = {}
        self.positions: Dict[str, Position] = {}
        # Every closed round trip lands here: symbol, side, qty, entry,
        # exit, pnl, pnl_pct, reason, result (PASS/FAIL), closed_at.
        self.closed_trades: List[Dict[str, Any]] = []

        logger.info("Paper broker initialized with capital=%.2f", initial_capital)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _short_notional(self) -> float:
        """Sum of open SHORT notionals at their average entry price."""
        return sum(
            p.avg_price * p.quantity
            for p in self.positions.values()
            if p.side == "SHORT"
        )

    def _entry_time(self, symbol: str) -> str:
        """Earliest FILLED order timestamp for a symbol (the trade's entry).

        Works for both entry legs of either side; returns "" when the
        position was never opened through this broker (e.g. restored).
        Duck-typed via getattr so hand-built test doubles (SimpleNamespace
        orders missing .placed_at) degrade to "" instead of breaking.
        """
        times = [getattr(o, "placed_at", "") for o in self.orders.values()
                 if getattr(o, "symbol", "") == symbol
                 and getattr(o, "status", "") == "FILLED"
                 and getattr(o, "placed_at", "")]
        return min(times) if times else ""

    def _record_close(
        self,
        symbol: str,
        pos: Position,
        qty_closed: int,
        price: float,
        reason: str,
    ) -> Dict[str, Any]:
        """Append a closed-trade record and fire the Telegram exit alert.

        Single recording site for every close (order-driven or
        ``close_position``-driven) so the ledger and alerts can never
        drift apart. Fail-closed: an alert outage never breaks a close.
        """
        from src.utils import minutes_between

        entry = pos.avg_price
        side = pos.side
        if side == "LONG":
            pnl = (price - entry) * qty_closed
        else:
            pnl = (entry - price) * qty_closed
        closed_at = utc_now()
        entry_at = self._entry_time(symbol)
        record = {
            "symbol": symbol,
            "side": side,
            "quantity": qty_closed,
            "entry": round(entry, 2),
            "exit": round(price, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / (entry * qty_closed) * 100, 2)
            if entry and qty_closed else 0.0,
            "reason": reason,
            "result": "PASS" if pnl > 0 else "FAIL",
            "entry_time": entry_at,
            "closed_at": closed_at,
            "holding_minutes": minutes_between(entry_at, closed_at),
        }
        self.closed_trades.append(record)
        try:
            from src.alerts import send_exit_alert
            send_exit_alert(record)
        except Exception as exc:
            logger.warning("Exit alert failed for %s: %s", symbol, exc)
        return record

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def place_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "LIMIT",
        price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        target: Optional[float] = None,
        reason: str = "order",
    ) -> BrokerOrder:
        """Place a paper order (immediately filled at given price).

        Fill logic is *side-aware*: what BUY/SELL means depends on the
        existing position's side.

        - BUY + existing SHORT  -> cover/reduce (records the close)
        - BUY + existing LONG   -> add to long
        - SELL + existing LONG  -> reduce/close (records the close)
        - SELL + existing SHORT -> add to short (never silently deletes)

        Shorts consume a margin pool equal to the initial capital, so a
        runaway scanner cannot open unlimited exposure.
        """
        order_id = f"paper_{uuid.uuid4().hex[:8]}"

        if price is None:
            price = 0.0

        cost = price * quantity
        pos = self.positions.get(symbol)

        # Reducing an existing position in the same direction closes risk;
        # opening/increasing consumes capital or margin.
        reducing = pos is not None and (
            (side == "SELL" and pos.side == "LONG")
            or (side == "BUY" and pos.side == "SHORT")
        )

        def _reject(error: str) -> BrokerOrder:
            order = BrokerOrder(
                order_id=order_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                order_type=order_type,
                price=price,
                stop_loss=stop_loss or 0.0,
                target=target or 0.0,
                status="REJECTED",
                broker="paper",
                placed_at=utc_now(),
                error=error,
            )
            logger.warning("Order rejected: %s (%s %d x %s @ %.2f)",
                           error, side, quantity, symbol, price)
            return order

        if not reducing:
            if side == "BUY" and cost > self.capital:
                return _reject("Insufficient capital")
            if side == "SELL":
                # Opening or adding to a short: cap total short notional at
                # the initial capital (the margin pool).
                others = self._short_notional()
                if pos is not None and pos.side == "SHORT":
                    others -= pos.avg_price * pos.quantity
                    new_qty = pos.quantity + quantity
                    new_avg = (pos.avg_price * pos.quantity + cost) / new_qty
                    prospective = others + new_avg * new_qty
                else:
                    prospective = others + cost
                if prospective > self.initial_capital:
                    return _reject(
                        "Short margin limit (total short notional "
                        f"{prospective:.0f} > capital {self.initial_capital:.0f})"
                    )

        # Simulate fill
        order = BrokerOrder(
            order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            price=price,
            stop_loss=stop_loss or 0.0,
            target=target or 0.0,
            status="FILLED",
            broker="paper",
            placed_at=utc_now(),
            filled_at=utc_now(),
        )
        self.orders[order_id] = order

        if side == "BUY":
            if pos is not None and pos.side == "SHORT":
                # Cover the short.
                qty_closed = min(quantity, pos.quantity)
                if quantity > qty_closed:
                    logger.warning(
                        "BUY %d exceeds SHORT %d on %s — clipping to %d",
                        quantity, pos.quantity, symbol, qty_closed)
                # Buyback is an obligation: always honoured, even if the
                # cash ledger is temporarily short (never strand a short).
                self.capital -= price * qty_closed
                self._record_close(symbol, pos, qty_closed, price, reason)
                pos.quantity -= qty_closed
                if pos.quantity <= 0:
                    del self.positions[symbol]
            else:
                # Add to (or open) a long.
                self.capital -= cost
                if pos is not None:
                    total_qty = pos.quantity + quantity
                    pos.avg_price = (pos.avg_price * pos.quantity + cost) / total_qty
                    pos.quantity = total_qty
                    pos.current_price = price
                else:
                    self.positions[symbol] = Position(
                        symbol=symbol,
                        side="LONG",
                        quantity=quantity,
                        avg_price=price,
                        current_price=price,
                    )
        elif side == "SELL":
            if pos is not None and pos.side == "LONG":
                # Sell out of the long.
                qty_closed = min(quantity, pos.quantity)
                if quantity > qty_closed:
                    logger.warning(
                        "SELL %d exceeds LONG %d on %s — clipping to %d",
                        quantity, pos.quantity, symbol, qty_closed)
                self.capital += price * qty_closed
                self._record_close(symbol, pos, qty_closed, price, reason)
                pos.quantity -= qty_closed
                if pos.quantity <= 0:
                    del self.positions[symbol]
            elif pos is not None and pos.side == "SHORT":
                # Add to the short — never reduce it here (reducing a short
                # is a BUY cover). The old code deleted the position on a
                # repeat SELL, silently destroying it.
                new_qty = pos.quantity + quantity
                pos.avg_price = (pos.avg_price * pos.quantity + cost) / new_qty
                pos.quantity = new_qty
                pos.current_price = price
                self.capital += cost
            else:
                # Open a new short.
                self.capital += cost
                self.positions[symbol] = Position(
                    symbol=symbol,
                    side="SHORT",
                    quantity=quantity,
                    avg_price=price,
                    current_price=price,
                )

        logger.info(
            "Paper order %s: %s %d x %s @ %.2f (capital=%.2f, reason=%s)",
            order_id, side, quantity, symbol, price, self.capital, reason,
        )

        return order

    def update_position(self, symbol: str, current_price: float) -> None:
        """Mark to market."""
        if symbol in self.positions:
            pos = self.positions[symbol]
            pos.current_price = current_price
            if pos.side == "LONG":
                pos.pnl = (current_price - pos.avg_price) * pos.quantity
            else:
                pos.pnl = (pos.avg_price - current_price) * pos.quantity
            if pos.avg_price > 0:
                pos.pnl_pct = (pos.pnl / (pos.avg_price * pos.quantity)) * 100

    def close_position(self, symbol: str, price: float, reason: str = "close", fraction: float = 1.0) -> Dict[str, Any]:
        """Close (part of) an open position.

        Delegates the fill to :meth:`place_order` (which records the exit
        in ``closed_trades`` and fires the Telegram alert) and returns the
        record it created.

        Returns:
            The closed-trade record dict (``{}`` if nothing was closed).
        """
        if symbol not in self.positions:
            return {}
        pos = self.positions[symbol]
        qty_to_close = int(pos.quantity * fraction)
        if qty_to_close <= 0:
            return {}

        logger.info(f"Closing {qty_to_close}/{pos.quantity} paper position {symbol} @ {price:.2f} ({reason})")
        order_side = "SELL" if pos.side == "LONG" else "BUY"
        before = len(self.closed_trades)
        self.place_order(symbol, order_side, qty_to_close,
                         price=price, reason=reason)
        if len(self.closed_trades) > before:
            return self.closed_trades[-1]
        return {}

    def cancel_order(self, order_id: str) -> BrokerOrder:
        """Cancel a paper order."""
        if order_id in self.orders:
            order = self.orders[order_id]
            order.status = "CANCELLED"
            logger.info("Cancelled paper order %s", order_id)
            return order

        return BrokerOrder(
            order_id=order_id,
            status="NOT_FOUND",
            broker="paper",
            error="Order not found",
        )

    def get_positions(self) -> List[Position]:
        """Get all open paper positions."""
        return list(self.positions.values())

    def get_balance(self) -> Dict[str, float]:
        """Get paper account balance.

        Equity math (the old version double-counted SHORT notionals and
        turned a 10k account into an 8.5 lakh phantom):

        - ``total``      = cash + market value of LONGs - market value of
          shares owed on SHORTs. A short's entry proceeds sit in cash, so
          the liability must be subtracted or equity double-counts them.
        - ``used_margin`` = SHORT notionals at entry (margin blocked);
          longs are already paid for out of cash.
        - ``available``  = ``capital`` - ``used_margin``.
        """
        long_value = 0.0
        short_owed = 0.0
        used = 0.0
        for p in self.positions.values():
            cur = p.current_price or p.avg_price
            if p.side == "LONG":
                long_value += cur * p.quantity
            else:
                short_owed += cur * p.quantity
                used += p.avg_price * p.quantity
        total = self.capital + long_value - short_owed
        return {
            "total": total,
            "available": self.capital - used,
            "used_margin": used,
        }

    def get_order_status(self, order_id: str) -> BrokerOrder:
        """Get status of a paper order."""
        if order_id in self.orders:
            return self.orders[order_id]
        return BrokerOrder(
            order_id=order_id,
            status="NOT_FOUND",
            broker="paper",
            error="Order not found",
        )
