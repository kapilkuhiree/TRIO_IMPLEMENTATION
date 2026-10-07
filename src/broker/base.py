"""
TRIO — Abstract Broker Interface
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

Defines the contract that all broker adapters must implement.
This ensures any broker (paper, Zerodha, Alpaca, IBKR, etc.) can be
plugged in without changing the rest of the system.

DISCLAIMER: Educational purposes only. Not financial advice.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class BrokerOrder:
    """Represents a broker order.
    Author: Kapil Kuhire
    """
    order_id: str = ""
    symbol: str = ""
    side: str = ""                # BUY or SELL
    quantity: int = 0
    order_type: str = "LIMIT"     # LIMIT, MARKET
    price: float = 0.0
    stop_loss: float = 0.0
    target: float = 0.0
    status: str = "PENDING"       # PENDING, PLACED, FILLED, CANCELLED, REJECTED
    broker: str = ""
    placed_at: str = ""
    filled_at: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Position:
    """Represents an open position.
    Author: Kapil Kuhire
    """
    symbol: str = ""
    side: str = ""
    quantity: int = 0
    avg_price: float = 0.0
    current_price: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    booked_pnl: float = 0.0      # profits locked from partial exits
    halved: bool = False         # flagged after partial profit-taking

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class BaseBroker(ABC):
    """
    Abstract base class for all broker adapters.

    Any broker integration must implement these methods.
    Author: Kapil Kuhire
    """

    @abstractmethod
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
        """
        Place an order.

        Args:
            symbol:     Ticker symbol.
            side:       BUY or SELL.
            quantity:   Number of shares/units.
            order_type: LIMIT or MARKET.
            price:      Limit price (optional for MARKET).
            stop_loss:  Stop-loss price.
            target:     Take-profit price.

        Returns:
            BrokerOrder with status.
        """
        ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> BrokerOrder:
        """Cancel an open order by ID."""
        ...

    @abstractmethod
    def get_positions(self) -> List[Position]:
        """Get all open positions."""
        ...

    @abstractmethod
    def get_balance(self) -> Dict[str, float]:
        """
        Get account balance.

        Returns:
            Dict with keys: total, available, used_margin.
        """
        ...

    @abstractmethod
    def get_order_status(self, order_id: str) -> BrokerOrder:
        """Get the current status of an order."""
        ...
