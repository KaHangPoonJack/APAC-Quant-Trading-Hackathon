"""DB-specific enums. Order-level Side/OrderType/OrderStatus are reused from
`core.enums` so the whole app shares one vocabulary."""
from __future__ import annotations

from enum import Enum


class CashFlowKind(str, Enum):
    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"


class AssetClass(str, Enum):
    EQUITY = "EQUITY"
    CASH = "CASH"
    OPTION = "OPTION"
    CRYPTO = "CRYPTO"
    FUTURE = "FUTURE"
    OTHER = "OTHER"


class TradeStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class TradeDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class DeploymentStatus(str, Enum):
    """Lifecycle of a strategy running in one environment."""
    TESTING = "TESTING"    # on the TEST account, being validated
    LIVE = "LIVE"          # actively trading (sim or real)
    PAUSED = "PAUSED"      # temporarily halted, state retained
    RETIRED = "RETIRED"    # decommissioned
