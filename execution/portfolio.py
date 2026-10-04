"""Per-poll view of the account: equity, cash, and position legs.

Call `refresh()` once per loop iteration; strategy/risk then read the cached
view without spending more of the Roostoo rate-limit budget.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from core.models import MARKET, AccountSnapshot, Position

log = logging.getLogger(__name__)


class Portfolio:
    def __init__(self, broker):
        self._broker = broker
        self._account: Optional[AccountSnapshot] = None
        self._legs: List[Position] = []

    def refresh(self) -> AccountSnapshot:
        self._account = self._broker.account(MARKET)
        self._legs = self._broker.position_legs(MARKET)
        return self._account

    @property
    def ready(self) -> bool:
        """True once a snapshot has been taken (no broker call)."""
        return self._account is not None

    @property
    def account(self) -> AccountSnapshot:
        return self._account or self.refresh()

    @property
    def equity(self) -> float:
        return self.account.total_assets

    @property
    def legs(self) -> List[Position]:
        if self._account is None:
            self.refresh()
        return self._legs

    def positions(self) -> Dict[str, Position]:
        return self.account.positions

    def current_weights(self) -> Dict[str, float]:
        """Signed weight of equity per pair (+long / -short)."""
        eq = self.equity
        if eq <= 0:
            return {}
        out: Dict[str, float] = {}
        for leg in self.legs:
            out[leg.code] = out.get(leg.code, 0.0) + leg.market_value / eq
        return out
