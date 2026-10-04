"""Immutable domain models passed between layers.

These are plain dataclasses with no behaviour beyond light validation/helpers,
so they serialise cleanly to logs and are trivial to unit-test.

Codes are Roostoo pairs (`BTC/USD`). Quantities are floats — crypto trades in
fractional units, rounded to each pair's `AmountPrecision` by the sizer.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional

from core.enums import Direction, OrderStatus, OrderType, Side

# All Roostoo accounts are one venue; the DB's per-market account key uses this.
MARKET = "ROOSTOO"


def coin_of(pair: str) -> str:
    """'BTC/USD' -> 'BTC'."""
    if "/" not in pair:
        raise ValueError(f"pair must look like BTC/USD, got: {pair!r}")
    return pair.split("/", 1)[0].upper()


@dataclass(frozen=True)
class Bar:
    """A single OHLCV candle for one pair (open time, UTC)."""
    code: str
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float = 0.0       # quote-asset (USDT) volume; 0 if unknown


@dataclass(frozen=True)
class Ticker:
    """Roostoo ticker snapshot (executable prices)."""
    code: str
    bid: float
    ask: float
    last: float
    change_24h: float = 0.0
    quote_volume: float = 0.0

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.last


@dataclass(frozen=True)
class PairRule:
    """Trading rules for one pair, from Roostoo /v3/exchangeInfo."""
    code: str
    price_precision: int
    amount_precision: int
    min_notional: float = 0.0       # Roostoo `MiniOrder`: price*qty must exceed it
    can_trade: bool = True
    asset_type: str = "crypto"      # Roostoo also lists tokenized stocks ("stock")

    def round_qty(self, qty: float) -> float:
        """Round DOWN to the amount step (never over-sell / over-spend)."""
        step = 10 ** -self.amount_precision
        return round(math.floor(qty / step + 1e-9) * step, self.amount_precision)

    def round_price(self, price: float) -> float:
        return round(price, self.price_precision)


@dataclass(frozen=True)
class OrderRequest:
    """An intent to trade, before it reaches the broker."""
    code: str
    side: Side
    qty: float
    price: float                      # limit price; reference price for MARKET
    order_type: OrderType = OrderType.MARKET
    reason: str = ""

    @property
    def market(self) -> str:
        return MARKET

    @property
    def notional(self) -> float:
        return abs(self.qty) * self.price


@dataclass
class OrderState:
    """Mutable view of a live order, updated from broker polls."""
    order_id: str
    code: str
    side: Side
    qty: float
    price: float
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: float = 0.0
    updated_at: Optional[datetime] = None


@dataclass(frozen=True)
class Position:
    """A currently-held position as reported by the broker.

    Spot holdings are LONG (qty > 0); /v6 short positions are SHORT (qty < 0).
    """
    code: str
    qty: float                        # signed: >0 long, <0 short
    avg_price: float
    market_value: float = 0.0         # signed USD value at the current mark
    unrealized_pl: float = 0.0
    today_pl: float = 0.0
    currency: str = "USD"

    @property
    def direction(self) -> Direction:
        if self.qty > 0:
            return Direction.LONG
        if self.qty < 0:
            return Direction.SHORT
        return Direction.FLAT


@dataclass
class AccountSnapshot:
    """Account-level cash/equity (USD)."""
    market: str
    cash: float                       # free USD
    total_assets: float               # equity: cash + locked + longs + shorts' value
    buying_power: float               # free USD minus nothing reserved
    currency: str = "USD"
    positions: Dict[str, Position] = field(default_factory=dict)  # code -> Position
