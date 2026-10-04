"""SQLAlchemy ORM models for the trade-record database.

Schema overview (all times UTC, reporting currency USD):

  brokers ─< accounts ─┬─< cash_flows
                       ├─< equity_snapshots ─┬─< snapshot_currency_comp
                       │                     └─< snapshot_assetclass_comp
                       ├─< daily_returns
                       ├─< positions            (current state, upserted)
                       ├─< orders ─< fills
                       └─< trades  (round-trip lifecycle; orders link back via trade_id)
  fx_rates  (standalone, hourly)
"""
from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    Date,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from core.enums import OrderStatus, OrderType, Side
from store.enums import (
    AssetClass,
    CashFlowKind,
    DeploymentStatus,
    TradeDirection,
    TradeStatus,
)
from store.timeutil import UTCDateTime


class Base(DeclarativeBase):
    pass


# ── strategies & deployments ──────────────────────────────────────────
class Strategy(Base):
    """A strategy definition, independent of where it runs."""
    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)   # e.g. "example_noop"
    description: Mapped[str] = mapped_column(String(255), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    deployments: Mapped[List["StrategyDeployment"]] = relationship(
        back_populates="strategy"
    )


class StrategyDeployment(Base):
    """A strategy running in one environment (Roostoo TEST or COMPETITION).

    Promotion sim -> real is a new REAL deployment of the same strategy_id, so
    the sim test history stays linked but separate from live trading. Multiple
    deployments can share the same physical accounts; per-strategy attribution is
    carried on orders/trades via deployment_id (the internal ledger).
    """
    __tablename__ = "strategy_deployments"
    __table_args__ = (
        UniqueConstraint("strategy_id", "trd_env", "label", name="uq_deployment"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"), index=True)
    trd_env: Mapped[str] = mapped_column(String(16))             # TEST / COMPETITION
    label: Mapped[str] = mapped_column(String(64), default="")   # optional variant tag
    status: Mapped[DeploymentStatus] = mapped_column(
        SAEnum(DeploymentStatus), default=DeploymentStatus.TESTING, index=True
    )
    allocated_capital: Mapped[float] = mapped_column(Float, default=0.0)
    reporting_ccy: Mapped[str] = mapped_column(String(8), default="USD")
    params_json: Mapped[str] = mapped_column(String(2000), default="{}")
    started_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)

    strategy: Mapped["Strategy"] = relationship(back_populates="deployments")


# ── reference: brokers & accounts ─────────────────────────────────────
class Broker(Base):
    __tablename__ = "brokers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)   # e.g. "ROOSTOO"
    region: Mapped[str] = mapped_column(String(32), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    accounts: Mapped[List["Account"]] = relationship(back_populates="broker")


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("broker_id", "external_acc_id", "trd_env",
                         name="uq_account_identity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    broker_id: Mapped[int] = mapped_column(ForeignKey("brokers.id"))
    external_acc_id: Mapped[str] = mapped_column(String(64))     # broker account id (Roostoo: key fingerprint)
    name: Mapped[str] = mapped_column(String(64), default="")
    market: Mapped[str] = mapped_column(String(8), default="")   # ROOSTOO (single venue)
    base_currency: Mapped[str] = mapped_column(String(8), default="USD")
    trd_env: Mapped[str] = mapped_column(String(16), default="TEST")
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    broker: Mapped["Broker"] = relationship(back_populates="accounts")


# ── capital flows & FX ────────────────────────────────────────────────
class CashFlow(Base):
    __tablename__ = "cash_flows"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    ts_utc: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    kind: Mapped[CashFlowKind] = mapped_column(SAEnum(CashFlowKind))
    amount: Mapped[float] = mapped_column(Float)                 # positive magnitude
    currency: Mapped[str] = mapped_column(String(8))
    note: Mapped[str] = mapped_column(String(255), default="")


class BenchmarkPrice(Base):
    """Daily close of a benchmark index (real indices via Yahoo Finance).
    Fetched and stored daily by the engine."""
    __tablename__ = "benchmark_prices"
    __table_args__ = (
        UniqueConstraint("symbol", "date", name="uq_benchmark_symbol_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)   # e.g. ^GSPC
    name: Mapped[str] = mapped_column(String(64), default="")     # e.g. S&P 500
    date: Mapped[date] = mapped_column(Date, index=True)
    close: Mapped[float] = mapped_column(Float)


class FxRate(Base):
    """Hourly FX snapshot, captured right before each equity snapshot so that
    equity valuations are reproducible."""
    __tablename__ = "fx_rates"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts_utc: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    base_ccy: Mapped[str] = mapped_column(String(8))            # e.g. USD
    quote_ccy: Mapped[str] = mapped_column(String(8))           # e.g. USD
    rate: Mapped[float] = mapped_column(Float)                  # 1 base = rate quote


# ── equity & returns ──────────────────────────────────────────────────
class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    ts_utc: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    total_equity: Mapped[float] = mapped_column(Float)          # in reporting_ccy
    cash: Mapped[float] = mapped_column(Float)
    market_value: Mapped[float] = mapped_column(Float)
    net_flow_since_prev: Mapped[float] = mapped_column(Float, default=0.0)
    reporting_ccy: Mapped[str] = mapped_column(String(8), default="USD")

    currency_comp: Mapped[List["SnapshotCurrencyComp"]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan"
    )
    assetclass_comp: Mapped[List["SnapshotAssetClassComp"]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan"
    )


class SnapshotCurrencyComp(Base):
    __tablename__ = "snapshot_currency_comp"

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("equity_snapshots.id"), index=True
    )
    currency: Mapped[str] = mapped_column(String(8))
    value: Mapped[float] = mapped_column(Float)                 # in reporting_ccy
    pct: Mapped[float] = mapped_column(Float)                   # 0..100

    snapshot: Mapped["EquitySnapshot"] = relationship(back_populates="currency_comp")


class SnapshotAssetClassComp(Base):
    __tablename__ = "snapshot_assetclass_comp"

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("equity_snapshots.id"), index=True
    )
    asset_class: Mapped[AssetClass] = mapped_column(SAEnum(AssetClass))
    value: Mapped[float] = mapped_column(Float)
    pct: Mapped[float] = mapped_column(Float)

    snapshot: Mapped["EquitySnapshot"] = relationship(back_populates="assetclass_comp")


class DailyReturn(Base):
    __tablename__ = "daily_returns"
    __table_args__ = (
        UniqueConstraint("account_id", "date_utc", name="uq_daily_return"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    date_utc: Mapped[date] = mapped_column(Date, index=True)
    equity_open: Mapped[float] = mapped_column(Float)
    equity_close: Mapped[float] = mapped_column(Float)
    net_flow: Mapped[float] = mapped_column(Float, default=0.0)
    return_pct: Mapped[float] = mapped_column(Float)            # Modified Dietz, %
    pnl_nominal: Mapped[float] = mapped_column(Float)


# ── current positions ─────────────────────────────────────────────────
class PositionRow(Base):
    """Current open positions (upserted per account+symbol on each refresh)."""
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("account_id", "symbol", name="uq_position_symbol"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    ts_utc: Mapped[datetime] = mapped_column(UTCDateTime)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    asset_class: Mapped[AssetClass] = mapped_column(
        SAEnum(AssetClass), default=AssetClass.EQUITY
    )
    currency: Mapped[str] = mapped_column(String(8), default="")
    qty: Mapped[float] = mapped_column(Float)                   # signed
    avg_price: Mapped[float] = mapped_column(Float)
    last_price: Mapped[float] = mapped_column(Float, default=0.0)
    market_value: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pl: Mapped[float] = mapped_column(Float, default=0.0)
    today_pl: Mapped[float] = mapped_column(Float, default=0.0)


# ── order / fill / trade lifecycle ────────────────────────────────────
class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("account_id", "broker_order_id", name="uq_order_broker_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    deployment_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("strategy_deployments.id"), nullable=True, index=True
    )
    trade_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("trades.id"), nullable=True, index=True
    )
    broker_order_id: Mapped[str] = mapped_column(String(64), index=True)
    ts_utc_created: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[Side] = mapped_column(SAEnum(Side))
    order_type: Mapped[OrderType] = mapped_column(SAEnum(OrderType))
    qty: Mapped[float] = mapped_column(Float)
    limit_price: Mapped[float] = mapped_column(Float, default=0.0)
    tp_level: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sl_level: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[OrderStatus] = mapped_column(
        SAEnum(OrderStatus), default=OrderStatus.PENDING
    )
    strategy: Mapped[str] = mapped_column(String(64), default="")
    reason: Mapped[str] = mapped_column(String(255), default="")

    fills: Mapped[List["Fill"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    trade: Mapped[Optional["Trade"]] = relationship(back_populates="orders")


class Fill(Base):
    __tablename__ = "fills"
    __table_args__ = (
        UniqueConstraint("broker_deal_id", name="uq_fill_deal_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    broker_deal_id: Mapped[str] = mapped_column(String(64))     # idempotency key
    ts_utc: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    qty: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(8), default="")

    order: Mapped["Order"] = relationship(back_populates="fills")


class Trade(Base):
    """A round-trip position lifecycle, assembled from one or more fills."""
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    deployment_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("strategy_deployments.id"), nullable=True, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    strategy: Mapped[str] = mapped_column(String(64), default="")
    direction: Mapped[TradeDirection] = mapped_column(SAEnum(TradeDirection))
    status: Mapped[TradeStatus] = mapped_column(
        SAEnum(TradeStatus), default=TradeStatus.OPEN, index=True
    )
    qty: Mapped[float] = mapped_column(Float)
    # Partial-close tracking: qty sold before the final close, and the realized
    # PnL those slices produced (native currency, ex-commission). On close, qty
    # becomes the total round-trip size and realized_pnl the gross PnL.
    closed_qty: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)

    entry_price: Mapped[float] = mapped_column(Float, default=0.0)
    entry_ts_utc: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_ts_utc: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    duration_sec: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    pnl_nominal: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    commission_total: Mapped[float] = mapped_column(Float, default=0.0)

    tp_level: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sl_level: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    order_type: Mapped[Optional[OrderType]] = mapped_column(
        SAEnum(OrderType), nullable=True
    )
    # Strategy-specific metadata as JSON (e.g. entry signal, scheduled exit).
    # Must survive restarts, so it lives on the trade row, not in memory.
    meta_json: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    orders: Mapped[List["Order"]] = relationship(back_populates="trade")


class DeploymentEquitySnapshot(Base):
    """Per-strategy equity curve on a shared account, reconstructed as
    allocated_capital + realized (closed trades) + unrealized (open trades MTM).
    Distinct from account-level EquitySnapshot, which is the physical truth."""
    __tablename__ = "deployment_equity_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    deployment_id: Mapped[int] = mapped_column(
        ForeignKey("strategy_deployments.id"), index=True
    )
    ts_utc: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    allocated_capital: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    equity: Mapped[float] = mapped_column(Float)               # in reporting_ccy
    return_pct: Mapped[float] = mapped_column(Float, default=0.0)
    # Closed-trade hit stats at snapshot time (win = pnl_nominal > 0).
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)  # %, 0 if no trades
    reporting_ccy: Mapped[str] = mapped_column(String(8), default="USD")
