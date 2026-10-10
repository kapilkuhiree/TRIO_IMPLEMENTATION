"""
TRIO — Options Paper Broker (local sim for NIFTY spreads)
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

Mirrors broker/paper.py's API so paper_trader needs no fork: place_order,
close_position (by spread id), get_positions, get_balance, get_order_status,
cancel_order. A "position" is one spread (2 legs) keyed by spread id;
legs are stored for the ledger and Telegram.

Fills: quoted at the selector's premium (live mid / LTP / BS sim). Marks
use the same premium source on each tick. EOD square-off closes at the
current mid (theta captured). Fail-closed like every other broker.

DISCLAIMER: Educational purposes only. Not financial advice.
"""

import logging
import uuid
from typing import Any, Dict, List, Optional

from src.broker.base import BaseBroker, BrokerOrder, Position
from src.utils import get_logger, load_config, utc_now

logger = get_logger("broker.options_paper")


def _spread_id(strategy: str, expiry: str, long_k: float,
               short_k: float) -> str:
    return (f"OPT_{strategy}_{expiry}_{int(long_k)}x{int(short_k)}"
            .replace(" ", ""))


class OptionsPaperBroker(BaseBroker):
    """Local paper fills for defined-risk NIFTY spreads."""

    provider_name = "options_paper"

    def __init__(self, initial_capital: float = 500000.0,
                 lot_size: int = 50):
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.lot_size = lot_size
        self.orders: Dict[str, BrokerOrder] = {}
        self.positions: Dict[str, Position] = {}
        # spread id -> leg detail for mark/close math
        self.spreads: Dict[str, Dict[str, Any]] = {}
        self.closed_trades: List[Dict[str, Any]] = []
        logger.info("Options paper broker initialized (capital=%.2f)",
                    initial_capital)

    # ------------------------------------------------------------------
    # spread lifecycle
    # ------------------------------------------------------------------

    def open_spread(self, candidate: Any, lots: int = 1,
                    reason: str = "options-plan") -> BrokerOrder:
        """Open one spread (or single long) from an OptionTradeCandidate.

        Returns a FILLED umbrella order (legs recorded in the order's
        error field as JSON would be hacky — legs live in self.spreads
        keyed by symbol). Single-leg longs (long-call/long-put) are
        supported: risk = premium, no short leg."""
        try:
            d = candidate.to_dict() if hasattr(candidate, "to_dict") \
                else dict(candidate)
        except Exception as exc:
            return self._reject("?", 0, 0.0, f"bad candidate: {exc}")
        legs = d.get("legs", []) or []
        strat = str(d.get("strategy") or "spread")
        single = (len(legs) == 1 and strat in ("long-call", "long-put"))
        if len(legs) < 2 and not single:
            return self._reject(d.get("strategy", "?"), 0, 0.0,
                                "spread needs 2 legs")
        longs = [l for l in legs if l.get("side") == "BUY"]
        shorts = [l for l in legs if l.get("side") == "SELL"]
        if single:
            if not longs:
                return self._reject(d.get("strategy", "?"), 0, 0.0,
                                    "long needs a BUY leg")
        elif not longs or not shorts:
            return self._reject(d.get("strategy", "?"), 0, 0.0,
                                "spread needs long+short")
        long_k = float(longs[0].get("strike") or 0)
        short_k = float(shorts[0].get("strike") or 0) if shorts else 0.0
        net = float(d.get("net_debit") or 0.0)
        expiry = str(d.get("expiry") or "")
        sid = _spread_id(strat, expiry, long_k, short_k)
        if sid in self.positions:
            return self._reject(sid, lots, net,
                                "already holding this spread")
        cost = net * lots * self.lot_size
        if cost > self.capital:
            return self._reject(sid, lots, net, "Insufficient capital")
        # Fill realism (spec §6): when fill_model is "bidask", reprice every
        # leg to its executable side (BUY at ask + slip, SELL at bid - slip)
        # instead of the selector's mid. Legs come from the candidate, so
        # this works for both a single long and a debit spread. A leg with no
        # bid/ask (sim-priced) is left untouched by fill_leg_premium.
        try:
            bidask = (str(load_config().get("options", {}).get(
                "fill_model", "mid")).strip().lower() == "bidask")
        except Exception:
            bidask = False
        if bidask:
            try:
                from src.options_pricing import fill_leg_premium as _fill
                buy_px = sum(_fill(l, "BUY") for l in longs)
                sell_px = sum(_fill(l, "SELL") for l in shorts)
                net = round(max(buy_px - sell_px, 0.05), 2)
                cost = net * lots * self.lot_size
                if cost > self.capital:
                    return self._reject(sid, lots, net,
                                        "Insufficient capital after slippage")
            except Exception:
                pass  # fallback stays on the selector's mid

        oid = f"opt_{uuid.uuid4().hex[:8]}"
        order = BrokerOrder(
            order_id=oid, symbol=sid, side="BUY", quantity=lots,
            order_type="MKT", price=net, status="FILLED",
            broker="options_paper", placed_at=utc_now(), filled_at=utc_now(),
        )
        self.orders[oid] = order
        self.capital -= cost
        self.positions[sid] = Position(
            symbol=sid, side="LONG", quantity=lots,
            avg_price=net, current_price=net)
        self.spreads[sid] = {
            "strategy": strat, "expiry": expiry, "legs": legs,
            "net_debit": net, "lots": lots,
            "maxLoss": float(d.get("maxLoss") or cost),
            "maxGain": float(d.get("maxGain") or 0.0),
            "breakeven": float(d.get("breakeven") or 0.0),
        }
        logger.info("Options spread opened %s x%d @ %.2f (%s)",
                    sid, lots, net, reason)
        return order

    def mark_spread(self, spread_id: str, mid: float) -> None:
        """Mark one spread to a fresh mid premium."""
        pos = self.positions.get(spread_id)
        if pos is None:
            return
        pos.current_price = float(mid)
        # P&L on a LONG debit spread = (mid - debit) * lots * lot
        pos.pnl = (float(mid) - pos.avg_price) * pos.quantity * self.lot_size
        if pos.avg_price > 0:
            pos.pnl_pct = (pos.pnl / (pos.avg_price * pos.quantity
                                      * self.lot_size)) * 100

    def close_spread(self, spread_id: str, mid: float,
                     reason: str = "close") -> Dict[str, Any]:
        """Close a spread at mid. Returns the closed-trade record."""
        from src.utils import minutes_between

        pos = self.positions.get(spread_id)
        if pos is None or pos.quantity <= 0:
            return {}
        lots = pos.quantity
        debit = pos.avg_price
        strategy = (self.spreads.get(spread_id) or {}).get("strategy", "")
        gross = (float(mid) - debit) * lots * self.lot_size
        # Net of round-trip cost (spec §3) so the paper ledger matches the
        # backtest (scripts/options_backtest._costs_for). Deduct from capital
        # too, so balance and booked P&L never disagree.
        cost = 0.0
        try:
            from src.options_pricing import round_trip_cost as _rtc
            cost = _rtc(strategy, debit, float(mid), lots, self.lot_size)
        except Exception:
            cost = 0.0
        pnl = round(gross - cost, 2)
        self.capital += float(mid) * lots * self.lot_size - cost
        entry_times = [getattr(o, "placed_at", "") for o in self.orders.values()
                       if getattr(o, "symbol", "") == spread_id
                       and getattr(o, "status", "") == "FILLED"
                       and getattr(o, "placed_at", "")]
        entry_at = min(entry_times) if entry_times else ""
        closed_at = utc_now()
        record = {
            "symbol": spread_id, "side": "LONG", "quantity": lots,
            "entry": round(debit, 2), "exit": round(float(mid), 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / (debit * lots * self.lot_size) * 100, 2)
            if debit and lots else 0.0,
            "reason": reason, "result": "PASS" if pnl > 0 else "FAIL",
            "entry_time": entry_at, "closed_at": closed_at,
            "holding_minutes": minutes_between(entry_at, closed_at),
            "asset": "options", "strategy": strategy,
            "cost": cost, "gross_pnl": round(gross, 2),
        }
        self.closed_trades.append(record)
        del self.positions[spread_id]
        self.spreads.pop(spread_id, None)
        try:
            from src.alerts import send_exit_alert
            send_exit_alert(record)
        except Exception as exc:
            logger.warning("Exit alert failed for %s: %s", spread_id, exc)
        return record

    def _reject(self, symbol: str, qty: int, price: float,
                error: str) -> BrokerOrder:
        order = BrokerOrder(
            order_id=f"opt_rej_{uuid.uuid4().hex[:8]}",
            symbol=symbol, side="BUY", quantity=qty,
            order_type="MKT", price=price, status="REJECTED",
            broker="options_paper", placed_at=utc_now(), error=error)
        logger.warning("Options paper REJECT %s: %s", symbol, error)
        return order

    # ------------------------------------------------------------------
    # BaseBroker interface (stock-shaped so paper_trader compiles)
    # ------------------------------------------------------------------

    def place_order(self, symbol: str, side: str, quantity: int,
                    order_type: str = "LIMIT",
                    price: Optional[float] = None,
                    stop_loss: Optional[float] = None,
                    target: Optional[float] = None,
                    reason: str = "order") -> BrokerOrder:
        return self._reject(symbol, quantity, price or 0.0,
                            "use open_spread() for options (legs required)")

    def close_position(self, symbol: str, price: float,
                       reason: str = "close",
                       fraction: float = 1.0) -> Dict[str, Any]:
        # OptionsPaperBroker has no partial closes; full close only.
        if symbol not in self.positions:
            return {}
        return self.close_spread(symbol, price, reason)

    def cancel_order(self, order_id: str) -> BrokerOrder:
        if order_id in self.orders:
            o = self.orders[order_id]
            o.status = "CANCELLED"
            return o
        return BrokerOrder(order_id=order_id, status="NOT_FOUND",
                           broker="options_paper", error="Order not found")

    def get_positions(self) -> List[Position]:
        return list(self.positions.values())

    def get_balance(self) -> Dict[str, float]:
        long_value = sum((p.current_price or p.avg_price) * p.quantity
                         * self.lot_size for p in self.positions.values())
        return {"total": self.capital + long_value,
                "available": self.capital, "used_margin": 0.0}

    def get_order_status(self, order_id: str) -> BrokerOrder:
        if order_id in self.orders:
            return self.orders[order_id]
        return BrokerOrder(order_id=order_id, status="NOT_FOUND",
                           broker="options_paper", error="Order not found")

    def update_position(self, symbol: str, current_price: float) -> None:
        self.mark_spread(symbol, current_price)
