"""Turn a USD notional into a tradeable quantity under a pair's rules."""
from __future__ import annotations

from core.models import PairRule


class PositionSizer:
    def __init__(self, min_order_usd: float = 0.0):
        self.min_order_usd = float(min_order_usd)

    def qty_for_notional(self, notional: float, price: float, rule: PairRule) -> float:
        """Largest qty on the amount step whose value does not exceed `notional`.
        Returns 0 for invalid inputs."""
        if notional <= 0 or price <= 0:
            return 0.0
        return rule.round_qty(notional / price)

    def is_tradeable(self, qty: float, price: float, rule: PairRule) -> bool:
        """Clears the exchange minimum (MiniOrder: price*qty must exceed it) and
        our own min_order_usd dust filter."""
        value = qty * price
        if qty <= 0 or value <= 0:
            return False
        if rule.min_notional and value <= rule.min_notional:
            return False
        return value >= self.min_order_usd
