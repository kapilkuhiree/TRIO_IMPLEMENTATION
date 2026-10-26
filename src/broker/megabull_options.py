"""
TRIO — MegaBull Options Broker (NIFTY spreads on the remote paper sim)
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

Wraps MegaBullBroker so the bot's index spreads can sit on MegaBull paper
instead of the local options_paper sim — the SAME spread interface:
open_spread / mark_spread / close_spread keyed by spread id, legs stored
in ``self.spreads`` for the ledger and Telegram.

Symbol mapping (proved live 2026-10-08 against
/api/marketwatch/instruments, 1070 NIFTY option rows):
    plan expiry "13-Oct-2026", strike 22250, kind "PE"
        -> MegaBull "NIFTY26O1322250PE" (token 11416834)
    i.e. NIFTY + YY + <month-code + DD> + strike + CE/PE, resolved through
    the MegaBull instrument cache (token_for), never hardcoded.

Instruments exist but the buysell endpoint REJECTED every option order we
tried (MIS: "Invalid quantity, Please enter quantity based on lot size"
for lots=1/units=50/75; NRML: server 500). So until MegaBull accepts
option orders this broker SILENT-FALLBACKS: any leg that does not confirm
COMPLETE unwinds any confirmed sister leg and reports REJECTED — equity
flow keeps running, no phantom spreads, no real money ever at stake.
The session only uses this broker when config
``options: {broker: megabull}``; the default "paper" keeps the local sim.

DISCLAIMER: Educational purposes only. Not financial advice.
"""

import logging
import uuid
from typing import Any, Dict, List, Optional

from src.broker.base import BaseBroker, BrokerOrder, Position
from src.utils import get_logger, utc_now

logger = get_logger("broker.megabull_options")

# NSE short month codes as seen in MegaBull option trading symbols
# (probed live: "26O13" = 13-Oct-2026; the long "26OCT..." alias also
# exists but the short form is canonical in their book).
_MONTH_CODES = {
    "JAN": "J", "FEB": "F", "MAR": "M", "APR": "A", "MAY": "M2",
    "JUN": "J2", "JUL": "J3", "AUG": "A2", "SEP": "S", "OCT": "O",
    "NOV": "N", "DEC": "D",
}

# Leg option-type aliases observed across NSE vendors.
_KIND_ALIASES = {"CE": "CE", "CALL": "CE", "C": "CE",
                 "PE": "PE", "PUT": "PE", "P": "PE"}


def option_symbol(underlying: str, expiry: str, strike: float,
                  kind: str) -> str:
    """Map a plan leg to a MegaBull option trading symbol.

    ``expiry`` is the plan/NSE form "13-Oct-2026"; the MegaBull form is
    "NIFTY26O1322250PE" (underlying + 2-digit year + short month code +
    zero-padded day + strike + CE/PE). Day/month ALWAYS padded so the
    symbol matches their book exactly.
    """
    und = (underlying or "NIFTY").strip().upper()
    mon = ""
    day = ""
    year2 = ""
    try:
        parts = str(expiry).strip().split("-")
        if len(parts) == 3:
            day = f"{int(parts[0]):02d}"
            mon = _MONTH_CODES.get(parts[1].strip().upper()[:3], "")
            year2 = str(parts[2])[-2:]
    except (ValueError, TypeError):
        pass
    k = _KIND_ALIASES.get(str(kind or "").strip().upper(), "CE")
    try:
        strike_txt = str(int(float(strike)))
    except (TypeError, ValueError):
        strike_txt = str(strike)
    return f"{und}{year2}{mon}{day}{strike_txt}{k}"


class MegaBullOptionsBroker(BaseBroker):
    """Remote paper fills for defined-risk NIFTY spreads via MegaBull."""

    provider_name = "megabull_options"

    def __init__(self, mega: Any, lot_size: int = 75,
                 initial_capital: float = 500000.0):
        self.mega = mega  # MegaBullBroker (equity-proven buysell path)
        self.lot_size = lot_size
        self.initial_capital = initial_capital
        self.capital = initial_capital  # mirror; MegaBull is authoritative
        self.orders: Dict[str, BrokerOrder] = {}
        self.positions: Dict[str, Position] = {}
        # spread id -> leg detail for mark/close math (same shape as
        # options_paper so session/digest code needs no fork)
        self.spreads: Dict[str, Dict[str, Any]] = {}
        self.closed_trades: List[Dict[str, Any]] = []
        logger.info("MegaBull options broker initialized (lot=%d)", lot_size)

    # ------------------------------------------------------------------
    # spread lifecycle
    # ------------------------------------------------------------------

    def _reject(self, symbol: str, qty: int, price: float,
                error: str) -> BrokerOrder:
        order = BrokerOrder(
            order_id=f"mbopt_rej_{uuid.uuid4().hex[:8]}",
            symbol=symbol, side="BUY", quantity=qty,
            order_type="MKT", price=price, status="REJECTED",
            broker="megabull_options", placed_at=utc_now(), error=error)
        logger.warning("MegaBull options REJECT %s: %s", symbol, error)
        return order

    @staticmethod
    def _spread_id(strategy: str, expiry: str, long_k: float,
                   short_k: float) -> str:
        return (f"OPT_{strategy}_{expiry}_{int(long_k)}x{int(short_k)}"
                .replace(" ", ""))

    def _leg_price(self, mega_symbol: str, ref: float) -> float:
        """Remote fill print for a leg: mirror first, ref premium next."""
        pos = self.mega.positions.get(mega_symbol)
        if pos is not None and pos.current_price:
            return float(pos.current_price)
        return float(ref or 0.0)

    def _close_remote(self, mega_symbol: str, side: str, lots: int,
                      ref_price: float, reason: str) -> bool:
        """Fire one leg's exit; True only when the remote book flattened."""
        try:
            price = (float(ref_price) if getattr(self, "strict_options_only", False)
                     else self._leg_price(mega_symbol, ref_price))
            rec = self.mega.close_position(
                mega_symbol, price,
                reason, fraction=1.0)
            return bool(rec)
        except Exception as exc:
            logger.warning("Remote leg close failed %s: %s",
                           mega_symbol, exc)
            return False

    def _unwind(self, opened: List[Dict[str, Any]], reason: str) -> bool:
        """Close confirmed legs and report whether every leg flattened."""
        all_closed = True
        for leg in opened:
            if leg.get("confirmed"):
                ok = self._close_remote(
                    leg["mega_symbol"], leg["side"], leg["lots"],
                    leg.get("price", 0.0), reason)
                leg["confirmed"] = ok
                all_closed = all_closed and ok
        return all_closed

    def open_spread(self, candidate: Any, lots: int = 1,
                    reason: str = "options-plan") -> BrokerOrder:
        """Open one spread with both legs as remote MegaBull paper orders.

        Units are REMOTE quantities: MegaBull validates option orders against the
        exchange lot size, so 1 lot must be sent as `lot_size` UNITS. Proved
        live 2026-10-12 against NIFTY 13-Oct-2026 22500 CE: qty=1 was
        REJECTED (HTTP 400 "quantity based on lot size") while qty=65
        (config lot_size) FILLED. Passing `lots` here is what produced every
        "Invalid quantity" rejection logged on 2026-10-08.

        Quantity semantics: every leg confirms by remote id; on ANY leg failure
        confirmed sister legs are unwound before returning REJECTED.
        """
        try:
            d = candidate.to_dict() if hasattr(candidate, "to_dict") \
                else dict(candidate)
        except Exception as exc:
            return self._reject("?", lots, 0.0, f"bad candidate: {exc}")
        legs = d.get("legs", []) or []
        expiry = str(d.get("expiry") or "")
        strat = str(d.get("strategy") or "spread")
        single = (len(legs) == 1 and strat in ("long-call", "long-put"))
        strict = bool(getattr(self, "strict_options_only", False) or d.get("strict_options_only"))
        if strict and (not single or legs[0].get("side") != "BUY" or
                       legs[0].get("kind") != ("CE" if strat == "long-call" else "PE")):
            return self._reject(strat, lots, 0.0, "strict mode requires one BUY CE/PE leg")
        if len(legs) < 2 and not single:
            return self._reject(d.get("strategy", "?"), lots, 0.0,
                                "spread needs 2 legs")
        longs = [l for l in legs if l.get("side") == "BUY"]
        shorts = [l for l in legs if l.get("side") == "SELL"]
        if single:
            if not longs:
                return self._reject(d.get("strategy", "?"), lots, 0.0,
                                    "long needs a BUY leg")
        elif not longs or not shorts:
            return self._reject(d.get("strategy", "?"), lots, 0.0,
                                "spread needs long+short")
        underlying = str(d.get("underlying") or "NIFTY")
        long_k = float(longs[0].get("strike") or 0)
        short_k = float(shorts[0].get("strike") or 0) if shorts else 0.0
        net = float(d.get("net_debit") or 0.0)
        contract_lot_size = d.get("lot_size")
        effective_lot = self.lot_size
        if contract_lot_size not in (None, 0):
            if (isinstance(contract_lot_size, bool) or
                    not isinstance(contract_lot_size, int) or contract_lot_size <= 0):
                return self._reject(strat, lots, net, "invalid contract lot size")
            if contract_lot_size != self.lot_size and not strict:
                return self._reject(strat, lots, net,
                                     "contract lot size does not match broker")
            effective_lot = contract_lot_size
        if strict:
            from src.options_intraday import _same_expiry
            contract = d.get("validated_contract") or {}
            symbol = str(d.get("trading_symbol") or "")
            try:
                current = self.mega.instrument_for(symbol)
                valid = (contract_lot_size is not None and isinstance(current, dict) and
                         bool(current.get("instrumentToken") or current.get("instrument_token")) and
                         bool(symbol) and not isinstance(lots, bool) and float(lots) > 0)
            except (AttributeError, TypeError, ValueError, KeyError):
                valid = False
            if not valid:
                return self._reject(strat, lots, net, "exact strict contract metadata is required")
        lots = max(1, int(lots or 1))
        sid = (f"OPT_{d['trading_symbol']}" if strict else
               self._spread_id(strat, expiry, long_k, short_k))
        if sid in self.positions:
            return self._reject(sid, lots, net,
                                "already holding this spread")

        units = max(1, int(lots) * max(1, int(effective_lot)))
        opened: List[Dict[str, Any]] = []
        pending_statuses = {"PENDING", "SUBMITTED", "ACCEPTED", "OPEN", "UNKNOWN"}
        for leg in legs:
            mega_symbol = d["trading_symbol"] if strict else option_symbol(underlying, expiry,
                                        float(leg.get("strike") or 0),
                                        str(leg.get("kind") or ""))
            try:
                self.mega.token_for(mega_symbol)
            except Exception as exc:
                self._unwind(opened, "option-leg-unwind")
                return self._reject(
                    sid, lots, net,
                    f"no MegaBull instrument for {mega_symbol}: {exc}")
            order = self.mega.place_order(
                mega_symbol, str(leg.get("side") or "BUY"), units,
                order_type="MKT",
                price=float(leg.get("premium") or net or 0.0) or None,
                reason=reason)
            status = str(getattr(order, "status", "UNKNOWN") or "UNKNOWN").upper()
            # A remote paper API can accept a request without confirming the
            # fill.  Preserve the real child order id/status so the strict
            # runtime records an unresolved intent instead of treating a
            # delayed/partial leg as a terminal rejection and retrying it.
            if status != "FILLED":
                order_id = str(getattr(order, "order_id", "") or "")
                if order_id:
                    self.orders[order_id] = order
                if status in pending_statuses:
                    if opened:
                        # A multi-leg caller must not leave confirmed sister
                        # legs unmanaged.  If unwinding is not confirmed, the
                        # returned UNKNOWN state keeps the parent blocked.
                        unwound = self._unwind(opened, "option-leg-unwind")
                        pending_status = "PENDING" if unwound else "UNKNOWN"
                    else:
                        pending_status = "PENDING"
                    return BrokerOrder(
                        order_id=order_id or f"mbopt_pending_{uuid.uuid4().hex[:8]}",
                        symbol=sid, side="BUY", quantity=lots,
                        order_type="MKT", price=net,
                        status=pending_status,
                        broker="megabull_options", placed_at=utc_now(),
                        error=(f"leg pending ({mega_symbol}): "
                               f"{getattr(order, 'error', '')}"),
                    )
                self._unwind(opened, "option-leg-unwind")
                return self._reject(
                    sid, lots, net,
                    f"leg rejected ({mega_symbol}): {order.error}")
            entry_px = self._leg_price(mega_symbol,
                                       float(leg.get("premium") or net or 0.0))
            leg_rec = {"mega_symbol": mega_symbol,
                       "side": str(leg.get("side") or "BUY"),
                       "lots": lots, "units": units,
                       "price": entry_px, "confirmed": True,
                       "order_id": order.order_id}
            opened.append(leg_rec)
            self.orders[order.order_id] = order

        self.positions[sid] = Position(
            symbol=sid, side="LONG", quantity=lots,
            avg_price=net, current_price=net)
        self.spreads[sid] = {
            "strategy": strat, "expiry": expiry, "legs": legs,
            "remote_legs": [{"mega_symbol": l["mega_symbol"],
                             "side": l["side"], "lots": l["lots"],
                             "price": l["price"], "closed": False}
                            for l in opened],
            "underlying": underlying,
            "lot_size": effective_lot,
            "net_debit": net, "lots": lots,
            "maxLoss": float(d.get("maxLoss") or net * lots * effective_lot),
            "maxGain": float(d.get("maxGain") or 0.0),
            "breakeven": float(d.get("breakeven") or 0.0),
        }
        logger.info("MegaBull options spread opened %s x%d @ %.2f (%s)",
                    sid, lots, net, reason)
        umbrella = BrokerOrder(
            order_id=f"mbopt_{uuid.uuid4().hex[:8]}",
            symbol=sid, side="BUY", quantity=lots,
            order_type="MKT", price=net, status="FILLED",
            broker="megabull_options", placed_at=utc_now(),
            filled_at=utc_now())
        self.orders[umbrella.order_id] = umbrella
        return umbrella

    def mark_spread(self, spread_id: str, mid: float) -> None:
        """Mark one spread to a fresh mid premium (same as options_paper)."""
        pos = self.positions.get(spread_id)
        if pos is None:
            return
        pos.current_price = float(mid)
        lot_size = self.spreads.get(spread_id, {}).get("lot_size", self.lot_size)
        pos.pnl = (float(mid) - pos.avg_price) * pos.quantity * lot_size
        if pos.avg_price > 0:
            pos.pnl_pct = (pos.pnl / (pos.avg_price * pos.quantity
                                      * lot_size)) * 100

    def close_spread(self, spread_id: str, mid: float,
                     reason: str = "close") -> Dict[str, Any]:
        """Close a spread: both remote legs first, then book the record.

        Records carry asset="options" so the shared ledger/digest splits
        them from equities, identical to options_paper closes.
        """
        from src.utils import minutes_between

        pos = self.positions.get(spread_id)
        if pos is None or pos.quantity <= 0:
            return {}
        detail = self.spreads.get(spread_id, {})
        lot_size = detail.get("lot_size", self.lot_size)
        lots = pos.quantity
        debit = pos.avg_price
        remote = detail.get("remote_legs") or []
        remote_ok = True
        if remote:
            for leg in remote:
                # A previous retry may already have confirmed this leg.  Do
                # not send a second opposite order for it.
                if leg.get("closed"):
                    continue
                exit_side = "SELL" if leg.get("side") == "BUY" else "BUY"
                if self._close_remote(
                        leg.get("mega_symbol", ""), exit_side,
                        int(leg.get("lots") or lots),
                        float(mid) or float(leg.get("price") or 0.0),
                        reason):
                    leg["closed"] = True
                else:
                    remote_ok = False
        if remote and (not remote_ok or
                       any(not leg.get("closed") for leg in remote)):
            # Keep the local spread and its remote-leg state intact.  Booking a
            # close here would hide an orphaned option leg and make the next
            # process believe the account is flat when it is not.
            logger.error("Spread %s close is uncertain; keeping it open for "
                         "retry", spread_id)
            return {}
        gross = (float(mid) - debit) * lots * lot_size
        # Net of round-trip cost (spec §3), mirroring options_paper and the
        # backtest so the shared digest never mixes gross and net P&L.
        cost = 0.0
        try:
            from src.options_pricing import round_trip_cost as _rtc
            cost = _rtc(detail.get("strategy", ""), debit, float(mid),
                        lots, lot_size)
        except Exception:
            cost = 0.0
        pnl = round(gross - cost, 2)
        closed_at = utc_now()
        entry_times = [getattr(o, "placed_at", "")
                       for o in self.orders.values()
                       if getattr(o, "symbol", "") == spread_id
                       and getattr(o, "status", "") == "FILLED"
                       and getattr(o, "placed_at", "")]
        entry_at = min(entry_times) if entry_times else ""
        record = {
            "symbol": spread_id, "side": "LONG", "quantity": lots,
            "entry": round(debit, 2), "exit": round(float(mid), 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / (debit * lots * lot_size) * 100, 2)
            if debit and lots else 0.0,
            "reason": reason, "result": "PASS" if pnl > 0 else "FAIL",
            "entry_time": entry_at, "closed_at": closed_at,
            "holding_minutes": minutes_between(entry_at, closed_at),
            "asset": "options",
            "strategy": detail.get("strategy", ""),
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
        # MegaBull legs are unit-lots; partial closes unsupported.
        if symbol not in self.positions:
            return {}
        return self.close_spread(symbol, price, reason)

    def get_positions(self) -> List[Position]:
        return list(self.positions.values())

    def get_balance(self) -> Dict[str, float]:
        """Money is authoritative on MegaBull's ledger."""
        try:
            return self.mega.get_balance()
        except Exception:
            # A mirror is not evidence of available remote funds.  The
            # validated execution gate deliberately rejects this schema.
            return {}

    def get_order_status(self, order_id: str) -> BrokerOrder:
        if order_id in self.orders:
            return self.orders[order_id]
        return BrokerOrder(order_id=order_id, status="NOT_FOUND",
                           broker="megabull_options", error="Order not found")

    def cancel_order(self, order_id: str) -> BrokerOrder:
        return BrokerOrder(order_id=order_id, status="NOT_FOUND",
                           broker="megabull_options",
                           error="cancel via MegaBull web UI")
