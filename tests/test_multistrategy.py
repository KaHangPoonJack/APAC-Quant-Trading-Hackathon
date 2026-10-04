"""Multi-strategy DB foundation: strategies, deployments, per-strategy ledger,
attribution, and per-deployment performance."""
import pytest

from core.enums import Side
from core.models import OrderState
from engine.fx import ConfigFxProvider
from engine.recorder import PersistenceService
from store.db import make_engine, make_session_factory
from store.enums import DeploymentStatus, TradeDirection
from store.models import Base
from store.repository import UnitOfWork


@pytest.fixture()
def sf(tmp_path):
    engine = make_engine(tmp_path / "ms.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


# ── strategy / deployment lifecycle ───────────────────────────────────
def test_deployment_get_or_create_idempotent(sf):
    with UnitOfWork(sf) as uow:
        s = uow.strategies.get_or_create("momentum")
        d1 = uow.deployments.get_or_create(s.id, "TEST")
        d2 = uow.deployments.get_or_create(s.id, "TEST")
        assert d1.id == d2.id


def test_test_and_competition_are_distinct_deployments(sf):
    with UnitOfWork(sf) as uow:
        s = uow.strategies.get_or_create("momentum")
        sim = uow.deployments.get_or_create(s.id, "TEST")
        real = uow.deployments.get_or_create(s.id, "COMPETITION")
        # Same strategy, two environments -> two deployments, statuses differ.
        assert sim.id != real.id
        assert sim.strategy_id == real.strategy_id == s.id
        assert sim.status == DeploymentStatus.TESTING
        assert real.status == DeploymentStatus.LIVE


# ── internal per-strategy ledger on a shared account ──────────────────
def test_two_strategies_hold_same_symbol_same_account(sf):
    with UnitOfWork(sf) as uow:
        acc = uow.accounts.get_or_create("ROOSTOO", "1001", "TEST")
        a = uow.deployments.get_or_create(uow.strategies.get_or_create("A").id, "TEST")
        b = uow.deployments.get_or_create(uow.strategies.get_or_create("B").id, "TEST")
        uow.trades.open_trade(acc.id, "ETH/USD", TradeDirection.LONG, 100, 400, deployment_id=a.id)
        uow.trades.open_trade(acc.id, "ETH/USD", TradeDirection.LONG, 50, 402, deployment_id=b.id)

        # Ledger keeps them separate; broker net (checksum) is the sum.
        assert uow.trades.open_for_symbol(acc.id, "ETH/USD", deployment_id=a.id).qty == 100
        assert uow.trades.open_for_symbol(acc.id, "ETH/USD", deployment_id=b.id).qty == 50
        assert uow.trades.net_open_qty(acc.id, "ETH/USD") == 150


def test_realized_pnl_per_deployment(sf):
    with UnitOfWork(sf) as uow:
        acc = uow.accounts.get_or_create("ROOSTOO", "1001", "TEST")
        dep = uow.deployments.get_or_create(uow.strategies.get_or_create("A").id, "TEST")
        t = uow.trades.open_trade(acc.id, "BTC/USD", TradeDirection.LONG, 100, 150,
                                  deployment_id=dep.id)
        uow.trades.close_trade(t, exit_price=155, commission_total=2.0)
        assert uow.trades.realized_pnl(dep.id) == pytest.approx(498.0)


# ── per-deployment equity ─────────────────────────────────────────────
def test_deployment_equity_snapshot(sf):
    with UnitOfWork(sf) as uow:
        dep = uow.deployments.get_or_create(
            uow.strategies.get_or_create("A").id, "TEST", allocated_capital=100000)
        snap = uow.deployment_equity.write_snapshot(
            dep.id, allocated_capital=100000, realized_pnl=500, unrealized_pnl=200)
        assert snap.equity == pytest.approx(100700)
        assert snap.return_pct == pytest.approx(0.7)


# ── recorder attributes orders/trades to the right deployment ─────────
class FakeBroker:
    def __init__(self):
        self._deals = []
    def acc_id(self, market):
        return 1001
    def deals(self, market):
        return list(self._deals)
    def set_deals(self, d):
        self._deals = d


def test_recorder_tags_trade_with_deployment(sf):
    broker = FakeBroker()
    svc = PersistenceService(sf, broker, ConfigFxProvider({}), trd_env="TEST")
    order = OrderState("O1", "BTC/USD", Side.BUY, 100, 150)
    svc.record_order("US", order, strategy="momentum")
    broker.set_deals([{"deal_id": "D1", "order_id": "O1", "code": "BTC/USD",
                       "side": "BUY", "qty": 100, "price": 150.0, "commission": 0.0,
                       "time": None, "currency": "USD"}])
    svc.sync_fills("US")

    with UnitOfWork(sf) as uow:
        acc = uow.accounts.get_or_create("ROOSTOO", "1001", "TEST")
        dep_id = svc.deployment_id("momentum")
        t = uow.trades.open_for_symbol(acc.id, "BTC/USD", deployment_id=dep_id)
        assert t is not None and t.deployment_id == dep_id
        o = uow.orders.get_by_broker_id(acc.id, "O1")
        assert o.deployment_id == dep_id
