"""Submits risk-approved orders and handles cancellation.

Roostoo has no push feed, so lifecycle is reconciled by polling: `open_orders`
for resting LIMIT orders, and the recorder's `sync_fills` for executions.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from core.models import MARKET, OrderRequest, OrderState
from execution.broker import BrokerError

log = logging.getLogger(__name__)


class OrderManager:
    def __init__(self, broker):
        self._broker = broker
        self._orders: Dict[str, OrderState] = {}

    def submit(self, req: OrderRequest) -> Optional[OrderState]:
        try:
            state = self._broker.place(req)
        except BrokerError as exc:
            log.error("Order rejected: %s", exc)
            return None
        if state.order_id:
            self._orders[state.order_id] = state
        return state

    def open_orders(self, market: str = MARKET) -> List[OrderState]:
        return self._broker.open_orders(market)

    def cancel(self, order_id: str) -> bool:
        try:
            self._broker.cancel(order_id)
            return True
        except BrokerError as exc:
            log.error("Failed to cancel %s: %s", order_id, exc)
            return False

    def cancel_all(self) -> int:
        """Cancel every pending order on the account. Returns count cancelled."""
        try:
            n = len(self._broker.cancel_all())
        except BrokerError as exc:
            log.error("cancel_all failed: %s", exc)
            return 0
        if n:
            log.info("Cancelled %d open order(s)", n)
        return n
