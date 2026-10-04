from datetime import datetime

import pytest

from core.enums import OrderStatus, OrderType, Side
from store.db import make_engine, make_session_factory
from store.enums import AssetClass, CashFlowKind, TradeDirection, TradeStatus
from store.models import Base
from store.repository import UnitOfWork
from store.returns import WeightedFlow, modified_dietz
from store.timeutil import UTC_TZ, now_utc


@pytest.fixture()
def sf(tmp_path):
    engine = make_engine(tmp_path / "test.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


# ── accounts / brokers ────────────────────────────────────────────────
def test_account_get_or_create_is_idempotent(sf):
    with UnitOfWork(sf) as uow:
        a1 = uow.accounts.get_or_create("ROOSTOO", "123", "TEST")
        a2 = uow.accounts.get_or_create("ROOSTOO", "123", "TEST")
        assert a1.id == a2.id
    with UnitOfWork(sf) as uow:
        assert len(uow.accounts.all_active()) == 1


# ── fx / cash flow ────────────────────────────────────────────────────
def test_fx_latest_returns_most_recent(sf):
    with UnitOfWork(sf) as uow:
        uow.fx.record("USDT", "USD", 0.9998, ts_utc=datetime(2026, 7, 14, 9, tzinfo=UTC_TZ))
        uow.fx.record("USDT", "USD", 1.0002, ts_utc=datetime(2026, 7, 14, 10, tzinfo=UTC_TZ))
    with UnitOfWork(sf) as uow:
        assert uow.fx.latest("USDT", "USD").rate == 1.0002


def test_cash_flow_stored_positive(sf):
    with UnitOfWork(sf) as uow:
        acc = uow.accounts.get_or_create("ROOSTOO", "1", "TEST")
        cf = uow.cash_flows.record(acc.id, CashFlowKind.WITHDRAWAL, -500, "USD")
        assert cf.amount == 500


# ── equity snapshot + composition ─────────────────────────────────────
def test_equity_snapshot_computes_pct(sf):
    with UnitOfWork(sf) as uow:
        acc = uow.accounts.get_or_create("ROOSTOO", "1", "TEST")
        snap = uow.equity.write_snapshot(
            acc.id, total_equity=100000, cash=40000, market_value=60000,
            currency_comp={"USDT": 40000, "USD": 60000},
            assetclass_comp={AssetClass.CASH: 40000, AssetClass.EQUITY: 60000},
        )
        pcts = {c.currency: c.pct for c in snap.currency_comp}
        assert pcts["USD"] == pytest.approx(60.0)
        assert pcts["USDT"] == pytest.approx(40.0)


# ── positions ─────────────────────────────────────────────────────────
def test_position_upsert_updates_in_place(sf):
    with UnitOfWork(sf) as uow:
        acc = uow.accounts.get_or_create("ROOSTOO", "1", "TEST")
        uow.positions.upsert(acc.id, "ETH/USD", qty=100, avg_price=400)
        uow.positions.upsert(acc.id, "ETH/USD", qty=200, avg_price=410)
    with UnitOfWork(sf) as uow:
        acc = uow.accounts.get_or_create("ROOSTOO", "1", "TEST")
        cur = uow.positions.current(acc.id)
        assert len(cur) == 1 and cur[0].qty == 200


# ── order / fill idempotency ──────────────────────────────────────────
def test_order_and_fill_idempotent(sf):
    with UnitOfWork(sf) as uow:
        acc = uow.accounts.get_or_create("ROOSTOO", "1", "TEST")
        o1 = uow.orders.record(acc.id, "OID1", "BTC/USD", Side.BUY,
                               OrderType.LIMIT, 10, 150, OrderStatus.SUBMITTED)
        o2 = uow.orders.record(acc.id, "OID1", "BTC/USD", Side.BUY,
                               OrderType.LIMIT, 10, 150, OrderStatus.SUBMITTED)
        assert o1.id == o2.id
        f1 = uow.fills.record(o1.id, "DEAL1", 10, 150.5, commission=1.0)
        f2 = uow.fills.record(o1.id, "DEAL1", 10, 150.5, commission=1.0)
        assert f1.id == f2.id


# ── trade lifecycle + pnl ─────────────────────────────────────────────
def test_trade_open_close_pnl(sf):
    with UnitOfWork(sf) as uow:
        acc = uow.accounts.get_or_create("ROOSTOO", "1", "TEST")
        t = uow.trades.open_trade(
            acc.id, "BTC/USD", TradeDirection.LONG, qty=100, entry_price=150,
            entry_ts_utc=datetime(2026, 7, 14, 10, 0, tzinfo=UTC_TZ),
        )
        uow.trades.close_trade(
            t, exit_price=155, commission_total=2.0,
            exit_ts_utc=datetime(2026, 7, 14, 11, 0, tzinfo=UTC_TZ),
        )
        assert t.status == TradeStatus.CLOSED
        assert t.pnl_nominal == pytest.approx(100 * 5 - 2.0)   # 498
        assert t.duration_sec == 3600
        assert t.pnl_pct == pytest.approx(498 / 15000 * 100)


def test_partial_close_books_realized_pnl(sf):
    """reduce() must realize the sold slice's PnL; close_trade() must combine
    partial slices + the final slice, and report the total round-trip qty."""
    with UnitOfWork(sf) as uow:
        acc = uow.accounts.get_or_create("ROOSTOO", "1", "TEST")
        t = uow.trades.open_trade(
            acc.id, "BTC/USD", TradeDirection.LONG, qty=100, entry_price=150,
            entry_ts_utc=datetime(2026, 7, 14, 10, 0, tzinfo=UTC_TZ),
        )
        uow.trades.reduce(t, 60, price=155)          # +5 x 60 = +300 realized
        assert t.status == TradeStatus.OPEN
        assert t.qty == pytest.approx(40)
        assert t.closed_qty == pytest.approx(60)
        assert t.realized_pnl == pytest.approx(300)

        uow.trades.close_trade(t, exit_price=148, commission_total=2.0)
        # final slice: -2 x 40 = -80; gross = 300 - 80 = 220; net = 218
        assert t.pnl_nominal == pytest.approx(218)
        assert t.qty == pytest.approx(100)           # total round-trip size
        assert t.pnl_pct == pytest.approx(218 / (150 * 100) * 100)


def test_partial_close_to_zero_then_close_keeps_pnl(sf):
    """Even if reduce() zeroes the qty, the final close must not report 0 PnL."""
    with UnitOfWork(sf) as uow:
        acc = uow.accounts.get_or_create("ROOSTOO", "1", "TEST")
        t = uow.trades.open_trade(acc.id, "ETH/USD", TradeDirection.LONG, 100, 400)
        uow.trades.reduce(t, 100, price=410)         # +10 x 100 = +1000
        uow.trades.close_trade(t, exit_price=410, commission_total=0.0)
        assert t.pnl_nominal == pytest.approx(1000)
        assert t.qty == pytest.approx(100)


def test_realized_pnl_sums_closed_trades_of_a_deployment(sf):
    with UnitOfWork(sf) as uow:
        acc = uow.accounts.get_or_create("ROOSTOO", "1", "TEST")
        strat = uow.strategies.get_or_create("s")
        dep = uow.deployments.get_or_create(strat.id, "TEST")
        t1 = uow.trades.open_trade(acc.id, "BTC/USD", TradeDirection.LONG, 0.1, 60000,
                                   deployment_id=dep.id)
        uow.trades.close_trade(t1, exit_price=61000, commission_total=0.0)   # +100
        t2 = uow.trades.open_trade(acc.id, "ETH/USD", TradeDirection.SHORT, 2, 3000,
                                   deployment_id=dep.id)
        uow.trades.close_trade(t2, exit_price=3250, commission_total=0.0)    # -500
        assert uow.trades.realized_pnl(dep.id) == pytest.approx(-400)


def test_deployment_allocation_reconciles_on_startup(sf):
    """A deployment created with 0 allocation must pick up a real allocation on
    a later get_or_create (config reconciliation), but a real value must never
    be clobbered back to 0."""
    with UnitOfWork(sf) as uow:
        strat = uow.strategies.get_or_create("momentum")
        d0 = uow.deployments.get_or_create(strat.id, "TEST", allocated_capital=0.0)
        assert d0.allocated_capital == 0.0
        d1 = uow.deployments.get_or_create(strat.id, "TEST",
                                           allocated_capital=1_000_000)
        assert d1.id == d0.id and d1.allocated_capital == 1_000_000
        # config with no allocation for this strategy (0.0) must not wipe it
        d2 = uow.deployments.get_or_create(strat.id, "TEST", allocated_capital=0.0)
        assert d2.allocated_capital == 1_000_000


def test_win_stats_and_snapshot_win_rate(sf):
    with UnitOfWork(sf) as uow:
        acc = uow.accounts.get_or_create("ROOSTOO", "1", "TEST")
        strat = uow.strategies.get_or_create("s")
        dep = uow.deployments.get_or_create(strat.id, "TEST")
        for pnl_exit in (110, 120, 90):   # 2 wins, 1 loss
            t = uow.trades.open_trade(acc.id, "BTC/USD", TradeDirection.LONG,
                                      10, 100, deployment_id=dep.id)
            uow.trades.close_trade(t, exit_price=pnl_exit, commission_total=0.0)
        wins, losses = uow.trades.win_stats(dep.id)
        assert (wins, losses) == (2, 1)
        snap = uow.deployment_equity.write_snapshot(
            dep.id, 10000, 300, 0, wins=wins, losses=losses)
        assert snap.win_rate == pytest.approx(2 / 3 * 100)
        # no closed trades -> win_rate 0, no division error
        empty = uow.deployment_equity.write_snapshot(dep.id, 10000, 0, 0)
        assert empty.win_rate == 0.0


def test_open_for_symbol(sf):
    with UnitOfWork(sf) as uow:
        acc = uow.accounts.get_or_create("ROOSTOO", "1", "TEST")
        uow.trades.open_trade(acc.id, "ETH/USD", TradeDirection.LONG, 100, 400)
    with UnitOfWork(sf) as uow:
        acc = uow.accounts.get_or_create("ROOSTOO", "1", "TEST")
        assert uow.trades.open_for_symbol(acc.id, "ETH/USD") is not None
        assert uow.trades.open_for_symbol(acc.id, "BTC/USD") is None


# ── UTC datetime round-trip───────────────────────────────────────────
def test_utcdatetime_roundtrip_is_utc(sf):
    with UnitOfWork(sf) as uow:
        acc = uow.accounts.get_or_create("ROOSTOO", "1", "TEST")
        ts = now_utc()
        uow.cash_flows.record(acc.id, CashFlowKind.DEPOSIT, 1000, "USD", ts_utc=ts)
    with UnitOfWork(sf) as uow:
        acc = uow.accounts.get_or_create("ROOSTOO", "1", "TEST")
        rows = uow.cash_flows.between(
            acc.id, datetime(2000, 1, 1, tzinfo=UTC_TZ), datetime(2100, 1, 1, tzinfo=UTC_TZ)
        )
        assert rows[0].ts_utc.utcoffset().total_seconds() == 0


# ── modified dietz ────────────────────────────────────────────────────
def test_modified_dietz_no_flows():
    r, pnl = modified_dietz(10000, 10500, [])
    assert pnl == pytest.approx(500)
    assert r == pytest.approx(5.0)


def test_modified_dietz_excludes_capital():
    # +1000 deposited exactly halfway (weight 0.5); end grew by 1500 total.
    r, pnl = modified_dietz(10000, 11500, [WeightedFlow(weight=0.5, amount=1000)])
    assert pnl == pytest.approx(500)                 # 11500-10000-1000
    assert r == pytest.approx(500 / 10500 * 100)     # denom 10000 + 0.5*1000
