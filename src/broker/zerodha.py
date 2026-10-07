"""
TRIO — Zerodha Kite Broker Adapter (Stub)
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

This is a placeholder for the Zerodha Kite Connect integration.
Real order execution is DISABLED by default and requires explicit
configuration and API credentials.

DISCLAIMER: Educational purposes only. Not financial advice.
"""

import logging
from typing import Any, Dict, List, Optional

from src.broker.base import BaseBroker, BrokerOrder, Position
from src.utils import get_env, get_logger, utc_now

logger = get_logger("broker.zerodha")


class ZerodhaBroker(BaseBroker):
    """
    Zerodha Kite Connect broker adapter.

    Requires:
        - pip install kiteconnect
        - Environment variables: ZERODHA_API_KEY, ZERODHA_API_SECRET, ZERODHA_ACCESS_TOKEN

    WARNING: This adapter can place real orders with real money.
    Only enable live_trading=True after thorough testing.
    Author: Kapil Kuhire
    """

    def __init__(self, live_trading: bool = False):
        self.live_trading = live_trading
        self.kite = None

        if not live_trading:
            logger.warning(
                "Zerodha adapter initialized in READ-ONLY mode. "
                "Set live_trading=True to enable real orders."
            )
            return

        try:
            from kiteconnect import KiteConnect
        except ImportError:
            raise ImportError("Install kiteconnect: pip install kiteconnect")

        api_key = get_env("ZERODHA_API_KEY", required=True)
        access_token = get_env("ZERODHA_ACCESS_TOKEN", required=True)

        self.kite = KiteConnect(api_key=api_key)
        self.kite.set_access_token(access_token)

        logger.info("Zerodha broker connected (LIVE TRADING ENABLED)")

    def place_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "LIMIT",
        price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        target: Optional[float] = None,
    ) -> BrokerOrder:
        """Place an order on Zerodha."""
        if not self.live_trading or self.kite is None:
            logger.warning("Live trading disabled — order NOT placed for %s", symbol)
            return BrokerOrder(
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price or 0.0,
                status="BLOCKED",
                broker="zerodha",
                error="Live trading is disabled",
                placed_at=utc_now(),
            )

        # Map to Kite order params
        transaction_type = "BUY" if side == "BUY" else "SELL"
        kite_order_type = "LIMIT" if order_type == "LIMIT" else "MARKET"

        try:
            order_id = self.kite.place_order(
                variety="regular",
                exchange="NSE",
                tradingsymbol=symbol.replace(".NS", ""),
                transaction_type=transaction_type,
                quantity=quantity,
                order_type=kite_order_type,
                price=price,
                product="CNC",
            )

            logger.info("Zerodha order placed: %s", order_id)

            return BrokerOrder(
                order_id=str(order_id),
                symbol=symbol,
                side=side,
                quantity=quantity,
                order_type=order_type,
                price=price or 0.0,
                stop_loss=stop_loss or 0.0,
                target=target or 0.0,
                status="PLACED",
                broker="zerodha",
                placed_at=utc_now(),
            )

        except Exception as exc:
            logger.error("Zerodha order failed: %s", exc)
            return BrokerOrder(
                symbol=symbol,
                side=side,
                quantity=quantity,
                status="REJECTED",
                broker="zerodha",
                error=str(exc),
                placed_at=utc_now(),
            )

    def cancel_order(self, order_id: str) -> BrokerOrder:
        """Cancel a Zerodha order."""
        if not self.live_trading or self.kite is None:
            return BrokerOrder(order_id=order_id, status="BLOCKED", broker="zerodha", error="Live trading disabled")

        try:
            self.kite.cancel_order(variety="regular", order_id=order_id)
            return BrokerOrder(order_id=order_id, status="CANCELLED", broker="zerodha")
        except Exception as exc:
            return BrokerOrder(order_id=order_id, status="ERROR", broker="zerodha", error=str(exc))

    def get_positions(self) -> List[Position]:
        """Get open positions from Zerodha."""
        if not self.live_trading or self.kite is None:
            return []

        try:
            positions = self.kite.positions()
            result = []
            for pos in positions.get("net", []):
                if pos["quantity"] != 0:
                    result.append(Position(
                        symbol=pos["tradingsymbol"],
                        side="LONG" if pos["quantity"] > 0 else "SHORT",
                        quantity=abs(pos["quantity"]),
                        avg_price=pos["average_price"],
                        current_price=pos["last_price"],
                        pnl=pos["pnl"],
                    ))
            return result
        except Exception as exc:
            logger.error("Failed to get positions: %s", exc)
            return []

    def get_balance(self) -> Dict[str, float]:
        """Get account balance from Zerodha."""
        if not self.live_trading or self.kite is None:
            return {"total": 0, "available": 0, "used_margin": 0}

        try:
            margins = self.kite.margins()
            equity = margins.get("equity", {})
            return {
                "total": equity.get("net", 0),
                "available": equity.get("available", {}).get("live_balance", 0),
                "used_margin": equity.get("utilised", {}).get("debits", 0),
            }
        except Exception as exc:
            logger.error("Failed to get balance: %s", exc)
            return {"total": 0, "available": 0, "used_margin": 0}

    def get_order_status(self, order_id: str) -> BrokerOrder:
        """Get order status from Zerodha."""
        if not self.live_trading or self.kite is None:
            return BrokerOrder(order_id=order_id, status="BLOCKED", broker="zerodha")

        try:
            history = self.kite.order_history(order_id)
            latest = history[-1] if history else {}
            return BrokerOrder(
                order_id=order_id,
                symbol=latest.get("tradingsymbol", ""),
                side=latest.get("transaction_type", ""),
                quantity=latest.get("quantity", 0),
                price=latest.get("price", 0),
                status=latest.get("status", "UNKNOWN"),
                broker="zerodha",
            )
        except Exception as exc:
            return BrokerOrder(order_id=order_id, status="ERROR", broker="zerodha", error=str(exc))
