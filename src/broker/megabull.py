"""
TRIO — MegaBull Paper Trading Broker Adapter

Executes TRIO signals on MegaBull's free paper-trading REST API
(base https://api.megabull.in, auth header `api-key`).

- Auth:   ``MEGABULL_API_KEY`` env var (never in code or committed config).
- Orders: POST /api/order/buysell, duration MIS (intraday; allows
  short-first entries), orderTypes LIMIT / MKT / SL.
- Exits:  opposite MIS order (no OCO/bracket exists); the caller loop
  (guard/stops/targets/EOD square-off) drives them, exactly like the
  local paper broker.
- P&L:    authoritative on MegaBull's side (`/api/user/my` virtualMoney
  + `/api/position/my` open P&L); every local close is also appended to
  `closed_trades` with PASS/FAIL and fires the Telegram exit alert so the
  dashboard and alerts behave identically across providers.

Fail-closed: any network/parse error returns a REJECTED BrokerOrder with
a clear message, and the caller falls back to skipping the trade — a
remote outage never crashes the trading loop.

DISCLAIMER: Educational purposes only. Not financial advice.
"""

import csv
import logging
import time
from time import sleep
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from src.broker.base import BaseBroker, BrokerOrder, Position
from src.utils import get_logger, utc_now

logger = get_logger("broker.megabull")

BASE_URL = "https://api.megabull.in"
DURATION = "MIS"  # intraday product: short selling allowed
TIMEOUT = 15
RETRIES = 2


class MegaBullBroker(BaseBroker):
    """Paper fills executed remotely on the MegaBull simulator."""

    provider_name = "megabull"

    def __init__(
        self,
        api_key: str = "",
        initial_capital: float = 500000.0,
        base_url: str = BASE_URL,
        instrument_cache: Optional[Path] = None,
        session: Optional["requests.Session"] = None,
    ):
        self.api_key = (api_key or "").strip()
        self.initial_capital = initial_capital
        self.capital = initial_capital  # local mirror; authoritative on their end
        # Set on the first 401: skips all further network calls until the
        # key is fixed and the process restarts (auth failures are permanent
        # for the lifetime of a bad key — don't burn scan time on retries).
        self._auth_failed = False
        self.base_url = base_url.rstrip("/")
        self.orders: Dict[str, BrokerOrder] = {}
        self.positions: Dict[str, Position] = {}
        self.closed_trades: List[Dict[str, Any]] = []
        self._tokens: Dict[str, str] = {}  # TRADINGSYMBOL -> instrumentToken
        self._session = session or _QuietSession()
        default_cache = Path("output") / "instruments_cache.csv"
        self.instrument_cache = instrument_cache or default_cache
        if not self.api_key:
            logger.warning("MegaBullBroker created WITHOUT an API key — "
                           "all orders will be rejected.")
        logger.info("MegaBull broker initialized (base=%s)", self.base_url)

    # ------------------------------------------------------------------
    # low-level HTTP
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {"api-key": self.api_key, "Content-Type": "application/json"}

    def _get(self, path: str) -> Any:
        url = f"{self.base_url}{path}"
        last: Exception = RuntimeError("unreachable")
        for i in range(RETRIES + 1):
            try:
                resp = self._session.get(url, headers=self._headers(),
                                         timeout=TIMEOUT)
                if resp.status_code == 401:
                    # Auth failure is permanent: set flag + fail fast.
                    # No point hammering 3 retries on every scan.
                    self._auth_failed = True
                    raise PermissionError("MegaBull 401: bad/expired api-key")
                resp.raise_for_status()
                return resp.json()
            except PermissionError:
                raise  # never retried, never slept
            except Exception as exc:  # noqa: BLE001 - retry then surface
                last = exc
                logger.warning("GET %s failed (attempt %d): %s", path, i + 1, exc)
                time.sleep(1 + i)
        raise last

    def _post(self, path: str, body: Dict[str, Any]) -> Any:
        url = f"{self.base_url}{path}"
        last: Exception = RuntimeError("unreachable")
        for i in range(RETRIES + 1):
            try:
                resp = self._session.post(url, headers=self._headers(),
                                          json=body, timeout=TIMEOUT)
                if resp.status_code == 401:
                    self._auth_failed = True
                    raise PermissionError("MegaBull 401: bad/expired api-key")
                resp.raise_for_status()
                return resp.json()
            except PermissionError:
                raise
            except Exception as exc:  # noqa: BLE001 - retry then surface
                last = exc
                logger.warning("POST %s failed (attempt %d): %s", path, i + 1, exc)
                time.sleep(1 + i)
        raise last

    # ------------------------------------------------------------------
    # symbol <-> instrument token
    # ------------------------------------------------------------------

    @staticmethod
    def to_trading_symbol(symbol: str) -> str:
        """TRIO uses 'ONGC.NS'; MegaBull uses 'ONGC' (incl. 'M&M')."""
        return symbol.replace(".NS", "").replace(".BO", "").strip().upper()

    def _last_print(self, symbol: str) -> float:
        """Best known print for required-price fields.

        Prefers the mirrored position's mark (the sim's own feed price);
        falls back to the most recent local order price; 0.0 only when
        nothing is known (caller rejects before sending).
        """
        pos = self.positions.get(symbol)
        if pos is not None and pos.current_price:
            return float(pos.current_price)
        prices = [o.price for o in self.orders.values()
                  if getattr(o, "symbol", "") == symbol
                  and (getattr(o, "price", 0) or 0) > 0]
        return float(prices[-1]) if prices else 0.0

    def token_for(self, symbol: str) -> str:
        """Resolve the MegaBull instrument token, refreshing the CSV cache."""
        tsym = self.to_trading_symbol(symbol)
        if tsym in self._tokens:
            return self._tokens[tsym]
        if self.instrument_cache.exists():
            try:
                with open(self.instrument_cache, encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        if (row.get("tradingSymbol") or "").upper() == tsym:
                            self._tokens[tsym] = row.get("instrumentToken", "")
                            return self._tokens[tsym]
            except (OSError, UnicodeDecodeError) as exc:
                logger.warning("Instrument cache unreadable (%s), refetching", exc)
        if getattr(self, "_auth_failed", False):
            raise PermissionError(
                "MegaBull 401: bad/expired api-key (cached list unavailable)")
        logger.info("Refreshing MegaBull instrument list for %s ...", tsym)
        info = self._get("/api/marketwatch/instruments")
        url = (info or {}).get("downloadUrl", "")
        if not url:
            raise RuntimeError("MegaBull instruments: no downloadUrl")
        resp = self._session.get(url, timeout=60)
        resp.raise_for_status()
        text = resp.text if isinstance(resp.text, str) else resp.content.decode()
        self.instrument_cache.parent.mkdir(parents=True, exist_ok=True)
        with open(self.instrument_cache, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        for row in csv.DictReader(text.splitlines()):
            key = (row.get("tradingSymbol") or "").upper()
            tok = row.get("instrumentToken", "")
            if key and tok:
                self._tokens[key] = tok
        if tsym not in self._tokens:
            raise KeyError(f"No MegaBull instrument for {symbol} ({tsym})")
        return self._tokens[tsym]

    # ------------------------------------------------------------------
    # BaseBroker interface
    # ------------------------------------------------------------------

    def _reject(self, symbol: str, side: str, quantity: int, price: float,
                error: str, order_type: str = "LIMIT") -> BrokerOrder:
        order = BrokerOrder(
            order_id=f"megabull_rej_{int(time.time() * 1000)}",
            symbol=symbol, side=side, quantity=quantity,
            order_type=order_type, price=price,
            status="REJECTED", broker="megabull",
            placed_at=utc_now(), error=error,
        )
        logger.warning("MegaBull REJECT %s %d x %s: %s", side, quantity,
                       symbol, error)
        return order

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
        """Send a MIS order to the MegaBull simulator."""
        if not self.api_key:
            return self._reject(symbol, side, quantity, price or 0.0,
                                "MEGABULL_API_KEY missing")
        try:
            token = self.token_for(symbol)
        except Exception as exc:  # noqa: BLE001 - surfaced as REJECTED
            return self._reject(symbol, side, quantity, price or 0.0,
                                f"instrument resolution failed: {exc}")

        otype = (order_type or "LIMIT").upper()
        if otype == "MARKET":
            otype = "MKT"
        body: Dict[str, Any] = {
            "instrumentToken": str(token),
            "qty": int(quantity),
            "type": side.upper(),
            "duration": DURATION,
            "orderType": otype,
        }
        # MegaBull quirk (proved live 2026-10-06): "Price cannot be Blank"
        # — MKT also requires a price field; the sim fills at ITS price
        # (returns priceAvg from its own feed). Omit only for MODIFY.
        if otype == "MKT" and not price:
            price = self._last_print(symbol)
        if otype == "LIMIT":
            if not price:
                return self._reject(symbol, side, quantity, 0.0,
                                    "LIMIT order needs a price", otype)
            body["price"] = float(price)
        elif otype == "MKT":
            body["price"] = float(price)
        elif otype == "SL":
            trig = stop_loss or price
            if not trig:
                return self._reject(symbol, side, quantity, 0.0,
                                    "SL order needs a trigger price", otype)
            body["triggerPrice"] = float(trig)
            if price:
                body["price"] = float(price)

        try:
            data = self._post("/api/order/buysell", body)
        except Exception as exc:  # noqa: BLE001 - fail-closed
            return self._reject(symbol, side, quantity, price or 0.0,
                                f"order API failed: {exc}", otype)

        remote_id = str((data or {}).get("id", ""))
        order = BrokerOrder(
            order_id=f"megabull_{remote_id or int(time.time() * 1000)}",
            symbol=symbol, side=side.upper(), quantity=int(quantity),
            order_type=otype, price=float(price or 0.0),
            stop_loss=stop_loss or 0.0, target=target or 0.0,
            status="FILLED", broker="megabull",
            placed_at=utc_now(), filled_at=utc_now(),
        )
        self.orders[order.order_id] = order
        logger.info("MegaBull order %s (remote %s): %s %d x %s (reason=%s)",
                    order.order_id, remote_id, order.side, quantity,
                    symbol, reason)
        # After-hours the sim queues accepted orders: /position/my can lag
        # minutes behind an ACCEPTED fill (proved live 2026-10-06: order
        # 771075 COMPLETE but invisible for 60s+). First confirm the fill
        # executed (order book), and ONLY then seed the mirror so the row
        # is closable. An accepted-but-unexecuted order leaves NO seed —
        # the exit path stays honest.
        if self._fill_confirmed(order):
            self._seed_mirror_from_order(symbol, side.upper(),
                                         int(quantity), float(price or 0.0))
        self.refresh_positions()
        pos = self.positions.get(symbol)
        logger.info("Post-order mirror for %s: %s",
                    symbol, (pos.side, pos.quantity, pos.avg_price)
                    if pos else None)
        return order

    def _order_row(self, remote_id: str) -> Optional[Dict[str, Any]]:
        """Fetch one order row by remote id, searching open + executed."""
        try:
            data = self._get("/api/order/my") or {}
        except Exception as exc:  # noqa: BLE001 - caller decides
            logger.warning("Order lookup failed: %s", exc)
            return None
        for o in (data.get("open", []) or []) + \
                (data.get("executed", []) or []):
            if str(o.get("id", "")) == str(remote_id):
                return o
        return None

    def _remote_id(self, order: BrokerOrder) -> str:
        return str(order.order_id).split("megabull_")[-1]

    def _fill_confirmed(self, order: BrokerOrder) -> bool:
        """True when the entry order really executed remotely.

        Same ambiguity problem as exits: match the SPECIFIC remote id,
        not side+qty. The entry is confirmed only when its row reads
        COMPLETE/EXECUTED/FILLED (or the position endpoint already nets
        the leg). A row still PENDING, or missing after propagation
        time, is NOT a fill — the mirror stays empty and the position
        stays unclosable until the exchange catches up (proved live:
        order 771079 accepted but absent from the book minutes later).
        """
        row = self._order_row(self._remote_id(order))
        if row is not None and str(row.get("status", "")).upper() in (
                "COMPLETE", "EXECUTED", "FILLED"):
            return True
        # Fallback: the position book already nets our leg.
        self.refresh_positions()
        pos = self.positions.get(order.symbol)
        return pos is not None and pos.quantity > 0

    def _exit_confirmed(self, order: BrokerOrder, symbol: str,
                        qty: int) -> bool:
        """True when the remote net position actually flattened.

        Uses the same id-specific row check as entries: the exit is
        confirmed when ITS row reads COMPLETE/EXECUTED/FILLED (or the
        position endpoint shows the symbol flat/gone). Retries with
        backoff because the book lags minutes after hours; returns False
        (stay open, record nothing) when the row never confirms.
        """
        want = self._remote_id(order)
        for attempt in range(4):
            rows: List[Dict[str, Any]] = []
            try:
                data = self._get("/api/order/my") or {}
                rows = (data.get("open", []) or []) + \
                    (data.get("executed", []) or [])
            except Exception as exc:  # noqa: BLE001 - retry, then unsafe
                logger.warning("Exit confirmation check failed: %s", exc)
            else:
                for o in rows:
                    if str(o.get("id", "")) == want and \
                            str(o.get("status", "")).upper() in (
                                "COMPLETE", "EXECUTED", "FILLED"):
                        return True
            self.refresh_positions()
            pos = self.positions.get(symbol)
            if pos is None or pos.quantity <= 0:
                return True
            sleep(2 + attempt * 2)
        logger.warning("Exit unconfirmed for %s after retries "
                       "(remote still shows %s)", symbol,
                       self.positions.get(symbol))
        return False

    def _seed_mirror_from_order(self, symbol: str, side: str,
                                quantity: int, price: float) -> None:
        """Optimistic position entry from an accepted order.

        Only fills gaps: never overwrites a row the exchange already
        reported. Keeps entries closable when the remote book lags.
        """
        if symbol in self.positions:
            pos = self.positions[symbol]
            if ((side == "BUY" and pos.side == "SHORT")
                    or (side == "SELL" and pos.side == "LONG")):
                # cross-side flow the refresh will net shortly; seed the
                # intent so close_position never sees a phantom flat book
                pos.quantity = max(pos.quantity, quantity)
                if price:
                    pos.current_price = price
            return
        self.positions[symbol] = Position(
            symbol=symbol,
            side="SHORT" if side == "SELL" else "LONG",
            quantity=quantity, avg_price=price or 0.0,
            current_price=price or 0.0,
        )

    def refresh_positions(self) -> None:
        """Rebuild the local position mirror from /api/position/my."""
        if getattr(self, "_auth_failed", False):
            return  # key is bad: mirror stays, scan loop stays fast
        try:
            data = self._get("/api/position/my")
        except Exception as exc:  # noqa: BLE001 - mirror may go stale
            logger.warning("Position refresh failed: %s", exc)
            return
        if isinstance(data, list):
            items = data  # API sometimes returns a bare list
        else:
            items = (data or {}).get("value", [])
        fresh: Dict[str, Position] = {}
        token_to_symbol = {v: k for k, v in self._tokens.items()}
        for it in items or []:
            try:
                qty = int(float(it.get("qty", 0) or 0))
            except (TypeError, ValueError):
                continue
            if qty == 0:
                continue
            # The entry side is authoritative for direction: a SELL fill
            # opens/adds SHORT, a BUY fill opens/adds LONG, and the
            # position's sign follows the API's own qty — never inferred
            # from side text alone (MIS books both SELL qty-(-n) rows and
            # netted rows depending on the endpoint state). Rows whose
            # type is neither side (e.g. NET summaries, stale ghosts) are
            # kept LONG by default so a real SHORT is never misread.
            side_txt = str(it.get("type", "")).upper()
            if side_txt.startswith(("SELL", "SHORT")):
                is_short = True
            elif side_txt.startswith("BUY"):
                is_short = qty < 0
            else:
                # Unknown row kind: only trust an explicit negative sign
                # for shorts when there is no affirmative BUY marker.
                is_short = qty < 0
            # instrumentName is the COMPANY name (e.g. "HDFC Bank Ltd"),
            # not the tradable symbol — resolve the symbol from
            # tradingSymbol first, falling back to the token map cache.
            tsym = str(it.get("tradingSymbol", "")
                       or token_to_symbol.get(
                           str(it.get("instrumentToken", "")), "")
                       or it.get("instrumentName", ""))
            symbol = (f"{tsym}.NS" if tsym and not tsym.endswith((".NS", " 50", " BANK"))
                      else tsym)
            try:
                avg = float(it.get("priceAvg") or it.get("avgBuyPrice") or 0.0)
            except (TypeError, ValueError):
                avg = 0.0
            try:
                pnl = float(it.get("pl", 0.0) or 0.0)
            except (TypeError, ValueError):
                pnl = 0.0
            fresh[symbol] = Position(
                symbol=symbol, side="SHORT" if is_short else "LONG",
                quantity=abs(qty), avg_price=avg, current_price=avg,
                pnl=pnl,
                pnl_pct=(pnl / (avg * abs(qty)) * 100) if avg and qty else 0.0,
            )
        self.positions = fresh

    def close_position(self, symbol: str, price: float,
                       reason: str = "close",
                       fraction: float = 1.0) -> Dict[str, Any]:
        """Close (part of) a position with an opposite MIS order.

        Position source of truth, in order: live exchange row (when the
        book has it) else the pre-refresh snapshot, which includes the
        entry seed from an ACCEPTED order whose remote leg is still
        propagating (proved live: /position/my lags minutes after hours).
        A seed is only used when the exchange book has NO row for the
        symbol — when the book says flat, it wins and no exit is
        invented. The exit then goes out, and _exit_confirmed decides
        whether the remote book flattened (record) or not (stay open).
        """
        before = self.positions.get(symbol)  # snapshot incl. entry seed
        self.refresh_positions()
        live = self.positions.get(symbol)
        if live is not None and live.quantity > 0:
            pos = live  # exchange truth wins whenever it has the row
        else:
            # No live row: either truly flat, or the remote book lagging
            # behind an accepted entry. A fresh seed (same run, no
            # exchange row yet) authorizes the exit attempt; a genuinely
            # flat book (never seeded) stays flat.
            pos = before if before is not None and before.quantity > 0 else None
            if pos is not None:
                self.positions[symbol] = pos  # restore seed for exit math
        if pos is None or pos.quantity <= 0:
            return {}
        qty = int(pos.quantity * fraction)
        if qty <= 0:
            return {}
        entry = pos.avg_price
        opp = "BUY" if pos.side == "SHORT" else "SELL"
        # Entry/exit shapes (proved live 2026-10-06):
        # - MKT needs a price field too ("Price cannot be Blank"); the sim
        #   fills at ITS print and returns it. Pass our print (stop/target
        #   hit level) so entry alerts name a real level.
        # - LIMIT far from the print sits PENDING after hours (a 699
        #   BUY-LIMIT on a 711 print never filled, then got cancelled
        #   while we had booked it closed). Exits go MKT for this reason.
        order = self.place_order(symbol, opp, qty, order_type="MKT",
                                 price=price, reason=reason)
        if order.status != "FILLED":
            logger.warning("Close failed for %s: %s", symbol, order.error)
            return {}
        # Confirm the exit actually executed remotely before recording
        # (proved live: PENDING/CANCELLED exits while booked as closed).
        if not self._exit_confirmed(order, symbol, qty):
            logger.warning("Close unconfirmed for %s (remote id %s) — "
                           "keeping position open", symbol, order.order_id)
            return {}
        pnl = ((price - entry) if pos.side == "LONG" else (entry - price)) * qty
        from src.utils import minutes_between

        closed_at = utc_now()
        entry_times = [getattr(o, "placed_at", "") for o in self.orders.values()
                       if getattr(o, "symbol", "") == symbol
                       and getattr(o, "status", "") == "FILLED"
                       and getattr(o, "placed_at", "")]
        entry_at = min(entry_times) if entry_times else ""
        record = {
            "symbol": symbol, "side": pos.side, "quantity": qty,
            "entry": round(entry, 2), "exit": round(price, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / (entry * qty) * 100, 2)
            if entry and qty else 0.0,
            "reason": reason,
            "result": "PASS" if pnl > 0 else "FAIL",
            "entry_time": entry_at,
            "closed_at": closed_at,
            "holding_minutes": minutes_between(entry_at, closed_at),
            "order_id": order.order_id,
        }
        self.closed_trades.append(record)
        try:
            from src.alerts import send_exit_alert
            send_exit_alert(record)
        except Exception as exc:  # noqa: BLE001 - alerts never break closes
            logger.warning("Exit alert failed for %s: %s", symbol, exc)
        self.refresh_positions()
        return record

    def get_positions(self) -> List[Position]:
        self.refresh_positions()
        return list(self.positions.values())

    def get_balance(self) -> Dict[str, float]:
        """Authoritative money comes from MegaBull's own ledger.

        total     = virtualMoney + open position P&L
        available = virtualMoneyLeft
        used      = virtualMoneyBlocked (+ fallback to open notionals)
        """
        if getattr(self, "_auth_failed", False):
            return {"total": self.capital, "available": self.capital,
                    "used_margin": 0.0}
        try:
            user = self._get("/api/user/my") or {}
            positions = self._get("/api/position/my") or {}
            if isinstance(positions, list):
                items = positions  # API sometimes returns a bare list
            else:
                items = positions.get("value", [])
            open_pnl = sum(float(it.get("pl", 0.0) or 0.0) for it in items or [])
            vm = float(user.get("virtualMoney", self.initial_capital))
            left = user.get("virtualMoneyLeft")
            blocked = float(user.get("virtualMoneyBlocked", 0.0) or 0.0)
            total = vm + open_pnl
            if blocked <= 0 and items:
                blocked = sum(
                    abs(float(it.get("priceAvg", 0.0) or 0.0))
                    * abs(int(it.get("qty", 0) or 0))
                    for it in items)
            available = float(left) if left is not None else (total - blocked)
            return {"total": total, "available": available,
                    "used_margin": blocked}
        except Exception as exc:  # noqa: BLE001 - dashboard must not die
            logger.warning("Balance fetch failed, using local mirror: %s", exc)
            return {"total": self.capital, "available": self.capital,
                    "used_margin": 0.0}

    def cancel_order(self, order_id: str) -> BrokerOrder:
        return BrokerOrder(order_id=order_id, status="NOT_FOUND",
                           broker="megabull", error="cancel via web UI")

    def update_position(self, symbol: str, current_price: float) -> None:
        """Mark a mirrored position to the caller's latest print.

        The remote ledger stays authoritative for money, but the local
        guard/stop loop prices exits off the data feed it trusts, so the
        mirror must honour mark-to-market calls like PaperBroker does.
        """
        pos = self.positions.get(symbol)
        if pos is None or not current_price:
            return
        pos.current_price = current_price
        if pos.side == "LONG":
            pos.pnl = (current_price - pos.avg_price) * pos.quantity
        else:
            pos.pnl = (pos.avg_price - current_price) * pos.quantity
        if pos.avg_price > 0 and pos.quantity:
            pos.pnl_pct = (pos.pnl / (pos.avg_price * pos.quantity)) * 100

    def get_order_status(self, order_id: str) -> BrokerOrder:
        if order_id in self.orders:
            return self.orders[order_id]
        return BrokerOrder(order_id=order_id, status="NOT_FOUND",
                           broker="megabull", error="Order not found")


class _QuietSession:
    """Thin wrapper so tests can inject a fake without importing requests."""

    def __init__(self) -> None:
        self._s = requests.Session()

    def get(self, url: str, **kwargs: Any) -> Any:
        return self._s.get(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self._s.post(url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> Any:
        return self._s.put(url, **kwargs)


def load_dotenv_key(name: str, env_path: Optional[Path] = None) -> str:
    """Read one key from the local .env without extra dependencies."""
    path = env_path or Path(".env")
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:  # noqa: BLE001 - missing .env is not fatal
        logging.getLogger("broker.megabull").debug(".env not readable")
    return ""
