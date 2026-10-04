"""Repository layer — the only SQL surface the rest of the app uses.

Access pattern: open a `UnitOfWork`, use the typed repos on it, and the context
manager commits on success / rolls back on error.

    with UnitOfWork(session_factory) as uow:
        acc = uow.accounts.get_or_create(broker="ROOSTOO", external_acc_id="abc123", ...)
        uow.equity.write_snapshot(acc.id, ...)
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from sqlalchemy import case, func

from core.enums import OrderStatus, OrderType, Side
from store.enums import (
    AssetClass,
    CashFlowKind,
    DeploymentStatus,
    TradeDirection,
    TradeStatus,
)
from store.models import (
    Account,
    BenchmarkPrice,
    Broker,
    CashFlow,
    DailyReturn,
    DeploymentEquitySnapshot,
    EquitySnapshot,
    Fill,
    FxRate,
    Order,
    PositionRow,
    SnapshotAssetClassComp,
    SnapshotCurrencyComp,
    Strategy,
    StrategyDeployment,
    Trade,
)
from store.timeutil import now_utc


# ── individual repositories ───────────────────────────────────────────
class AccountRepo:
    def __init__(self, s: Session):
        self.s = s

    def get_or_create(self, broker: str, external_acc_id: str, trd_env: str,
                      base_currency: str = "USD", name: str = "",
                      market: str = "") -> Account:
        brk = self.s.scalar(select(Broker).where(Broker.name == broker))
        if brk is None:
            brk = Broker(name=broker)
            self.s.add(brk)
            self.s.flush()
        acc = self.s.scalar(
            select(Account).where(
                Account.broker_id == brk.id,
                Account.external_acc_id == external_acc_id,
                Account.trd_env == trd_env,
            )
        )
        if acc is None:
            acc = Account(broker_id=brk.id, external_acc_id=external_acc_id,
                          trd_env=trd_env, base_currency=base_currency, name=name,
                          market=market.upper())
            self.s.add(acc)
            self.s.flush()
        elif market and not acc.market:
            acc.market = market.upper()
            self.s.flush()
        return acc

    def all_active(self) -> List[Account]:
        return list(self.s.scalars(select(Account).where(Account.active.is_(True))))


class StrategyRepo:
    def __init__(self, s: Session):
        self.s = s

    def get_or_create(self, name: str, description: str = "") -> Strategy:
        strat = self.s.scalar(select(Strategy).where(Strategy.name == name))
        if strat is None:
            strat = Strategy(name=name, description=description)
            self.s.add(strat)
            self.s.flush()
        return strat


class DeploymentRepo:
    def __init__(self, s: Session):
        self.s = s

    def get_or_create(self, strategy_id: int, trd_env: str, label: str = "",
                      allocated_capital: float = 0.0, reporting_ccy: str = "USD",
                      status: DeploymentStatus = DeploymentStatus.TESTING) -> StrategyDeployment:
        trd_env = trd_env.upper()
        dep = self.s.scalar(
            select(StrategyDeployment).where(
                StrategyDeployment.strategy_id == strategy_id,
                StrategyDeployment.trd_env == trd_env,
                StrategyDeployment.label == label,
            )
        )
        if dep is None:
            # The COMPETITION deployment defaults to LIVE; TEST defaults to TESTING.
            default_status = (DeploymentStatus.LIVE if trd_env.upper() == "COMPETITION"
                              else status)
            dep = StrategyDeployment(
                strategy_id=strategy_id, trd_env=trd_env, label=label,
                allocated_capital=allocated_capital, reporting_ccy=reporting_ccy,
                status=default_status, started_at=now_utc(),
            )
            self.s.add(dep)
            self.s.flush()
        elif allocated_capital and dep.allocated_capital != allocated_capital:
            # Reconcile the allocation from config on every startup — the row is
            # created once (often before allocations were configured), so without
            # this the denominator for return% stays stale at its create-time value.
            dep.allocated_capital = allocated_capital
            self.s.flush()
        return dep

    def set_status(self, dep: StrategyDeployment, status: DeploymentStatus) -> None:
        dep.status = status
        if status == DeploymentStatus.RETIRED and dep.ended_at is None:
            dep.ended_at = now_utc()
        self.s.flush()

    def active(self) -> List[StrategyDeployment]:
        return list(self.s.scalars(
            select(StrategyDeployment).where(
                StrategyDeployment.status != DeploymentStatus.RETIRED
            )
        ))


class CashFlowRepo:
    def __init__(self, s: Session):
        self.s = s

    def record(self, account_id: int, kind: CashFlowKind, amount: float,
               currency: str, ts_utc: Optional[datetime] = None, note: str = "") -> CashFlow:
        cf = CashFlow(account_id=account_id, ts_utc=ts_utc or now_utc(), kind=kind,
                      amount=abs(amount), currency=currency, note=note)
        self.s.add(cf)
        self.s.flush()
        return cf

    def between(self, account_id: int, start: datetime, end: datetime) -> List[CashFlow]:
        return list(self.s.scalars(
            select(CashFlow).where(
                CashFlow.account_id == account_id,
                CashFlow.ts_utc >= start, CashFlow.ts_utc < end,
            ).order_by(CashFlow.ts_utc)
        ))


class FxRepo:
    def __init__(self, s: Session):
        self.s = s

    def record(self, base_ccy: str, quote_ccy: str, rate: float,
               ts_utc: Optional[datetime] = None) -> FxRate:
        fx = FxRate(ts_utc=ts_utc or now_utc(), base_ccy=base_ccy,
                    quote_ccy=quote_ccy, rate=rate)
        self.s.add(fx)
        self.s.flush()
        return fx

    def latest(self, base_ccy: str, quote_ccy: str) -> Optional[FxRate]:
        return self.s.scalar(
            select(FxRate).where(
                FxRate.base_ccy == base_ccy, FxRate.quote_ccy == quote_ccy
            ).order_by(FxRate.ts_utc.desc()).limit(1)
        )


class EquityRepo:
    def __init__(self, s: Session):
        self.s = s

    def write_snapshot(
        self, account_id: int, total_equity: float, cash: float, market_value: float,
        currency_comp: Dict[str, float], assetclass_comp: Dict[AssetClass, float],
        net_flow_since_prev: float = 0.0, reporting_ccy: str = "USD",
        ts_utc: Optional[datetime] = None,
    ) -> EquitySnapshot:
        snap = EquitySnapshot(
            account_id=account_id, ts_utc=ts_utc or now_utc(), total_equity=total_equity,
            cash=cash, market_value=market_value,
            net_flow_since_prev=net_flow_since_prev, reporting_ccy=reporting_ccy,
        )
        total = total_equity or 1.0
        for ccy, val in currency_comp.items():
            snap.currency_comp.append(
                SnapshotCurrencyComp(currency=ccy, value=val, pct=val / total * 100.0)
            )
        for ac, val in assetclass_comp.items():
            snap.assetclass_comp.append(
                SnapshotAssetClassComp(asset_class=ac, value=val, pct=val / total * 100.0)
            )
        self.s.add(snap)
        self.s.flush()
        return snap

    def latest(self, account_id: int) -> Optional[EquitySnapshot]:
        return self.s.scalar(
            select(EquitySnapshot).where(EquitySnapshot.account_id == account_id)
            .order_by(EquitySnapshot.ts_utc.desc()).limit(1)
        )

    def history(self, account_id: int, limit: int = 500) -> List[EquitySnapshot]:
        rows = list(self.s.scalars(
            select(EquitySnapshot).where(EquitySnapshot.account_id == account_id)
            .order_by(EquitySnapshot.ts_utc.desc()).limit(limit)
        ))
        return list(reversed(rows))

    def last_before(self, account_id: int, ts: datetime) -> Optional[EquitySnapshot]:
        """Most recent snapshot strictly before `ts` (e.g. previous-day close)."""
        return self.s.scalar(
            select(EquitySnapshot).where(
                EquitySnapshot.account_id == account_id,
                EquitySnapshot.ts_utc < ts,
            ).order_by(EquitySnapshot.ts_utc.desc()).limit(1)
        )

    def snapshot_dates(self, account_id: int) -> List[date]:
        """Distinct UTC calendar dates that have at least one snapshot.
        ts_utc is stored as an ISO string, so the date is its first 10 chars."""
        rows = self.s.execute(
            select(func.distinct(func.substr(EquitySnapshot.ts_utc, 1, 10)))
            .where(EquitySnapshot.account_id == account_id)
        ).scalars().all()
        return [date.fromisoformat(str(r)) for r in rows]


class ReturnsRepo:
    def __init__(self, s: Session):
        self.s = s

    def upsert_daily(self, account_id: int, day: date, equity_open: float,
                     equity_close: float, net_flow: float, return_pct: float,
                     pnl_nominal: float) -> DailyReturn:
        row = self.s.scalar(
            select(DailyReturn).where(
                DailyReturn.account_id == account_id, DailyReturn.date_utc == day
            )
        )
        if row is None:
            row = DailyReturn(account_id=account_id, date_utc=day)
            self.s.add(row)
        row.equity_open = equity_open
        row.equity_close = equity_close
        row.net_flow = net_flow
        row.return_pct = return_pct
        row.pnl_nominal = pnl_nominal
        self.s.flush()
        return row

    def get(self, account_id: int, day: date) -> Optional[DailyReturn]:
        return self.s.scalar(
            select(DailyReturn).where(
                DailyReturn.account_id == account_id, DailyReturn.date_utc == day
            )
        )

    def dates(self, account_id: int) -> List[date]:
        """All days that already have a rolled-up return."""
        return list(self.s.execute(
            select(DailyReturn.date_utc).where(DailyReturn.account_id == account_id)
        ).scalars())


class PositionRepo:
    def __init__(self, s: Session):
        self.s = s

    def upsert(self, account_id: int, symbol: str, qty: float, avg_price: float,
               last_price: float = 0.0, market_value: float = 0.0,
               unrealized_pl: float = 0.0, today_pl: float = 0.0,
               asset_class: AssetClass = AssetClass.EQUITY, currency: str = "",
               ts_utc: Optional[datetime] = None) -> PositionRow:
        row = self.s.scalar(
            select(PositionRow).where(
                PositionRow.account_id == account_id, PositionRow.symbol == symbol
            )
        )
        if row is None:
            row = PositionRow(account_id=account_id, symbol=symbol)
            self.s.add(row)
        row.ts_utc = ts_utc or now_utc()
        row.qty = qty
        row.avg_price = avg_price
        row.last_price = last_price
        row.market_value = market_value
        row.unrealized_pl = unrealized_pl
        row.today_pl = today_pl
        row.asset_class = asset_class
        row.currency = currency
        self.s.flush()
        return row

    def current(self, account_id: int) -> List[PositionRow]:
        return list(self.s.scalars(
            select(PositionRow).where(
                PositionRow.account_id == account_id, PositionRow.qty != 0
            )
        ))

    def remove(self, account_id: int, symbol: str) -> None:
        row = self.s.scalar(
            select(PositionRow).where(
                PositionRow.account_id == account_id, PositionRow.symbol == symbol
            )
        )
        if row is not None:
            self.s.delete(row)
            self.s.flush()


class OrderRepo:
    def __init__(self, s: Session):
        self.s = s

    def record(self, account_id: int, broker_order_id: str, symbol: str, side: Side,
               order_type: OrderType, qty: float, limit_price: float = 0.0,
               status: OrderStatus = OrderStatus.SUBMITTED, strategy: str = "",
               reason: str = "", trade_id: Optional[int] = None,
               deployment_id: Optional[int] = None,
               tp_level: Optional[float] = None, sl_level: Optional[float] = None,
               ts_utc: Optional[datetime] = None) -> Order:
        existing = self.get_by_broker_id(account_id, broker_order_id)
        if existing is not None:
            return existing
        order = Order(
            account_id=account_id, broker_order_id=broker_order_id,
            deployment_id=deployment_id,
            ts_utc_created=ts_utc or now_utc(), symbol=symbol, side=side,
            order_type=order_type, qty=qty, limit_price=limit_price, status=status,
            strategy=strategy, reason=reason, trade_id=trade_id,
            tp_level=tp_level, sl_level=sl_level,
        )
        self.s.add(order)
        self.s.flush()
        return order

    def get_by_broker_id(self, account_id: int, broker_order_id: str) -> Optional[Order]:
        return self.s.scalar(
            select(Order).where(
                Order.account_id == account_id,
                Order.broker_order_id == broker_order_id,
            )
        )

    def update_status(self, order: Order, status: OrderStatus) -> None:
        order.status = status
        self.s.flush()


class FillRepo:
    def __init__(self, s: Session):
        self.s = s

    def get(self, broker_deal_id: str) -> Optional[Fill]:
        return self.s.scalar(
            select(Fill).where(Fill.broker_deal_id == broker_deal_id)
        )

    def record(self, order_id: int, broker_deal_id: str, qty: float, price: float,
               commission: float = 0.0, currency: str = "",
               ts_utc: Optional[datetime] = None) -> Fill:
        existing = self.get(broker_deal_id)
        if existing is not None:  # idempotent on broker_deal_id
            return existing
        fill = Fill(order_id=order_id, broker_deal_id=broker_deal_id,
                    ts_utc=ts_utc or now_utc(), qty=qty, price=price,
                    commission=commission, currency=currency)
        self.s.add(fill)
        self.s.flush()
        return fill


class TradeRepo:
    def __init__(self, s: Session):
        self.s = s

    def open_trade(self, account_id: int, symbol: str, direction: TradeDirection,
                   qty: float, entry_price: float, strategy: str = "",
                   deployment_id: Optional[int] = None,
                   order_type: Optional[OrderType] = None,
                   tp_level: Optional[float] = None, sl_level: Optional[float] = None,
                   entry_ts_utc: Optional[datetime] = None) -> Trade:
        trade = Trade(
            account_id=account_id, symbol=symbol, direction=direction,
            deployment_id=deployment_id,
            status=TradeStatus.OPEN, qty=qty, entry_price=entry_price,
            entry_ts_utc=entry_ts_utc or now_utc(), strategy=strategy,
            order_type=order_type, tp_level=tp_level, sl_level=sl_level,
        )
        self.s.add(trade)
        self.s.flush()
        return trade

    def increase(self, trade: Trade, add_qty: float, add_price: float) -> Trade:
        """Grow an open position, recomputing the volume-weighted entry price."""
        total = trade.qty + add_qty
        if total > 0:
            trade.entry_price = (
                trade.entry_price * trade.qty + add_price * add_qty
            ) / total
        trade.qty = total
        self.s.flush()
        return trade

    def reduce(self, trade: Trade, qty: float,
               price: Optional[float] = None) -> Trade:
        """Partially close a position (keeps the trade OPEN).

        When `price` is given, the realized PnL of the sold slice is booked into
        `trade.realized_pnl` so partial exits are never lost from performance.
        """
        take = min(qty, trade.qty)
        if price is not None and take > 0:
            sign = 1.0 if trade.direction == TradeDirection.LONG else -1.0
            trade.realized_pnl = (trade.realized_pnl or 0.0) + \
                (price - trade.entry_price) * take * sign
        trade.closed_qty = (trade.closed_qty or 0.0) + take
        trade.qty = max(0.0, trade.qty - qty)
        self.s.flush()
        return trade

    def close_trade(self, trade: Trade, exit_price: float,
                    commission_total: Optional[float] = None,
                    exit_ts_utc: Optional[datetime] = None) -> Trade:
        """Finalise a trade. If commission_total is None, it is summed from all
        fills linked to this trade's orders. PnL = realized partial slices (from
        reduce()) + the final slice on the remaining qty − commission; on close,
        `qty` becomes the total round-trip size."""
        exit_ts = exit_ts_utc or now_utc()
        if commission_total is None:
            commission_total = sum(
                f.commission for o in trade.orders for f in o.fills
            )
        remaining = max(0.0, trade.qty)
        total_qty = (trade.closed_qty or 0.0) + remaining
        trade.exit_price = exit_price
        trade.exit_ts_utc = exit_ts
        trade.status = TradeStatus.CLOSED
        trade.commission_total = commission_total
        if trade.entry_ts_utc is not None:
            trade.duration_sec = int((exit_ts - trade.entry_ts_utc).total_seconds())
        sign = 1.0 if trade.direction == TradeDirection.LONG else -1.0
        gross = (trade.realized_pnl or 0.0) + \
            (exit_price - trade.entry_price) * remaining * sign
        trade.realized_pnl = gross
        trade.closed_qty = total_qty
        trade.qty = total_qty
        trade.pnl_nominal = gross - commission_total
        cost = trade.entry_price * total_qty
        trade.pnl_pct = (trade.pnl_nominal / cost * 100.0) if cost else 0.0
        self.s.flush()
        return trade

    def open_for_account(self, account_id: int) -> List[Trade]:
        return list(self.s.scalars(
            select(Trade).where(
                Trade.account_id == account_id, Trade.status == TradeStatus.OPEN
            )
        ))

    def open_for_symbol(self, account_id: int, symbol: str,
                        deployment_id: Optional[int] = None,
                        direction: Optional[TradeDirection] = None) -> Optional[Trade]:
        """Open trade for a symbol in an account. When deployment_id is given,
        scope to that strategy (the ledger allows several strategies to hold the
        same symbol concurrently, so callers must disambiguate). `direction`
        separates a spot LONG from a /v6 SHORT on the same pair."""
        conds = [Trade.account_id == account_id, Trade.symbol == symbol,
                 Trade.status == TradeStatus.OPEN]
        if deployment_id is not None:
            conds.append(Trade.deployment_id == deployment_id)
        if direction is not None:
            conds.append(Trade.direction == direction)
        return self.s.scalar(select(Trade).where(*conds).order_by(Trade.id))

    def open_for_deployment(self, deployment_id: int) -> List[Trade]:
        return list(self.s.scalars(
            select(Trade).where(
                Trade.deployment_id == deployment_id, Trade.status == TradeStatus.OPEN
            )
        ))

    def closed_for_deployment(self, deployment_id: int) -> List[Trade]:
        return list(self.s.scalars(
            select(Trade).where(
                Trade.deployment_id == deployment_id, Trade.status == TradeStatus.CLOSED
            )
        ))

    def net_open_qty(self, account_id: int, symbol: str) -> float:
        """Sum of open-trade quantities for a symbol across all deployments in an
        account — the value that must equal the broker's net position (the ledger
        reconciliation checksum)."""
        total = 0.0
        for t in self.s.scalars(select(Trade).where(
            Trade.account_id == account_id, Trade.symbol == symbol,
            Trade.status == TradeStatus.OPEN,
        )):
            sign = 1.0 if t.direction == TradeDirection.LONG else -1.0
            total += t.qty * sign
        return total

    def realized_pnl(self, deployment_id: int) -> float:
        """Sum of realized PnL over a deployment's closed trades."""
        val = self.s.scalar(
            select(func.coalesce(func.sum(Trade.pnl_nominal), 0.0)).where(
                Trade.deployment_id == deployment_id,
                Trade.status == TradeStatus.CLOSED,
            )
        )
        return float(val or 0.0)

    def win_stats(self, deployment_id: int) -> tuple[int, int]:
        """(wins, losses) over a deployment's closed trades.
        Win = pnl_nominal > 0; breakeven/negative counts as a loss."""
        wins = self.s.scalar(
            select(func.count()).where(
                Trade.deployment_id == deployment_id,
                Trade.status == TradeStatus.CLOSED,
                Trade.pnl_nominal > 0,
            )
        ) or 0
        total = self.s.scalar(
            select(func.count()).where(
                Trade.deployment_id == deployment_id,
                Trade.status == TradeStatus.CLOSED,
            )
        ) or 0
        return int(wins), int(total - wins)


class DeploymentEquityRepo:
    def __init__(self, s: Session):
        self.s = s

    def write_snapshot(self, deployment_id: int, allocated_capital: float,
                       realized_pnl: float, unrealized_pnl: float,
                       reporting_ccy: str = "USD",
                       wins: int = 0, losses: int = 0,
                       ts_utc: Optional[datetime] = None) -> DeploymentEquitySnapshot:
        equity = allocated_capital + realized_pnl + unrealized_pnl
        pnl = realized_pnl + unrealized_pnl
        return_pct = (pnl / allocated_capital * 100.0) if allocated_capital else 0.0
        closed = wins + losses
        win_rate = (wins / closed * 100.0) if closed else 0.0
        snap = DeploymentEquitySnapshot(
            deployment_id=deployment_id, ts_utc=ts_utc or now_utc(),
            allocated_capital=allocated_capital, realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl, equity=equity, return_pct=return_pct,
            wins=wins, losses=losses, win_rate=win_rate,
            reporting_ccy=reporting_ccy,
        )
        self.s.add(snap)
        self.s.flush()
        return snap

    def history(self, deployment_id: int, limit: int = 500) -> List[DeploymentEquitySnapshot]:
        rows = list(self.s.scalars(
            select(DeploymentEquitySnapshot)
            .where(DeploymentEquitySnapshot.deployment_id == deployment_id)
            .order_by(DeploymentEquitySnapshot.ts_utc.desc()).limit(limit)
        ))
        return list(reversed(rows))


class BenchmarkRepo:
    def __init__(self, s: Session):
        self.s = s

    def upsert(self, symbol: str, name: str, day: date, close: float) -> None:
        row = self.s.scalar(select(BenchmarkPrice).where(
            BenchmarkPrice.symbol == symbol, BenchmarkPrice.date == day))
        if row is None:
            self.s.add(BenchmarkPrice(symbol=symbol, name=name, date=day, close=close))
        else:
            row.close = close
            row.name = name

    def latest_date(self, symbol: str) -> Optional[date]:
        return self.s.scalar(
            select(BenchmarkPrice.date).where(BenchmarkPrice.symbol == symbol)
            .order_by(BenchmarkPrice.date.desc()).limit(1)
        )


class UnitOfWork:
    """Transactional scope bundling all repositories over one session."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self._factory = session_factory
        self.session: Optional[Session] = None

    def __enter__(self) -> "UnitOfWork":
        self.session = self._factory()
        s = self.session
        self.accounts = AccountRepo(s)
        self.strategies = StrategyRepo(s)
        self.deployments = DeploymentRepo(s)
        self.cash_flows = CashFlowRepo(s)
        self.fx = FxRepo(s)
        self.equity = EquityRepo(s)
        self.deployment_equity = DeploymentEquityRepo(s)
        self.returns = ReturnsRepo(s)
        self.positions = PositionRepo(s)
        self.orders = OrderRepo(s)
        self.fills = FillRepo(s)
        self.trades = TradeRepo(s)
        self.benchmarks = BenchmarkRepo(s)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        assert self.session is not None
        try:
            if exc_type is None:
                self.session.commit()
            else:
                self.session.rollback()
        finally:
            self.session.close()

    def commit(self) -> None:
        assert self.session is not None
        self.session.commit()
