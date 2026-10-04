"""Domain enums, decoupled from any broker's own vocabulary.

Strategy/risk code only ever sees these; the execution layer
(execution/broker.py) is the single place that translates them to Roostoo
request parameters.
"""
from __future__ import annotations

from enum import Enum


class Side(str, Enum):
    """Order direction. Roostoo spot uses BUY/SELL; its /v6 short endpoints
    appear in order history as SHORT_OPEN / SHORT_CLOSE."""
    BUY = "BUY"
    SELL = "SELL"
    SHORT_OPEN = "SHORT_OPEN"
    SHORT_CLOSE = "SHORT_CLOSE"

    @property
    def adds_exposure(self) -> bool:
        return self in (Side.BUY, Side.SHORT_OPEN)


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class Direction(int, Enum):
    """Sign of a held position."""
    SHORT = -1
    FLAT = 0
    LONG = 1


class OrderStatus(str, Enum):
    """Normalised lifecycle states (superset of what we care about)."""
    PENDING = "PENDING"        # submitted locally / resting at the exchange
    SUBMITTED = "SUBMITTED"    # accepted by broker
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED)
