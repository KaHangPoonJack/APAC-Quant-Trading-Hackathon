from datetime import date, datetime

import pytest

from core.enums import Side
from core.models import MARKET, AccountSnapshot, OrderState, Position
from engine.fx import ConfigFxProvider
from engine.recorder import PersistenceService
from store.db import make_engine, make_session_factory
from store.enums import CashFlowKind, TradeDirection, TradeStatus
from store.models import Trade
from store.repository import UnitOfWork
from store.models import Base
from store.timeutil import UTC_TZ

ACC = "roostoo-test-abc"


class FakeBroker:
    """Minimal broker stand-in: account/positions/deals are just data."""

    def __init__(self):
        self._deals = []
        self._account = None

    def acc_id(self, market=MARKET):
        return ACC

    def set_account(self, cash, total_assets, positions):
        self._account = AccountSnapshot(
            market=MARKET, cash=cash, total_assets=total_assets,
            buying_power=cash, currency="USD", positions=positions,
        )

    def account(self, market=MARKET):
        return self._account

    def positions(self, market=MARKET):
        return self._account.positions if self._account else {}

    def set_deals(self, deals):
        self._deals = deals

    def deals(self, market=MARKET):
        return list(self._deals)


@pytest.fixture()
def svc(tmp_path):
    engine = make_engine(tmp_path / "rec.db")
    Base.metadata.create_all(engine)
    sf = make_session_factory(engine)
    broker = FakeBroker()
    service = PersistenceService(sf, broker, ConfigFxProvider({}), trd_env="TEST")
    return service, broker, sf


def _deal(deal_id, order_id, code, side, qty, price, comm=0.0, time=None):
    return {"deal_id": deal_id, "order_id": order_id, "code": code, "side": side,
            "qty": qty, "price": price, "commission": comm, "time": time,
            "currency": "USD"}


def _acc(uow):
    return uow.accounts.get_or_create("ROOSTOO", ACC, "TEST")


# ── order + fill -> trade ─────────────────────────────────────────────
def test_buy_fill_opens_trade(svc):
    service, broker, sf = svc
    order = OrderState(order_id="O1", code="BTC/USD", side=Side.BUY, qty=0.5, price=60000)
    service.record_order(MARKET, order, strategy="my_strat")
    broker.set_deals([_deal("D1", "O1", "BTC/USD", "BUY", 0.5, 60000.0, comm=3.0,
                            time=1757980800000)])

    assert service.sync_fills() == 1
    with UnitOfWork(sf) as uow:
        t = uow.trades.open_for_symbol(_acc(uow).id, "BTC/USD")
        assert t is not None and t.status == TradeStatus.OPEN
        assert t.direction == TradeDirection.LONG
        assert t.qty == 0.5 and t.entry_price == 60000.0
        # Roostoo epoch-ms timestamps are parsed as UTC
        assert t.entry_ts_utc == datetime(2025, 9, 16, 0, 0, tzinfo=UTC_TZ)


def test_sync_fills_is_idempotent(svc):
    service, broker, sf = svc
    order = OrderState(order_id="O1", code="BTC/USD", side=Side.BUY, qty=1, price=60000)
    service.record_order(MARKET, order, strategy="my_strat")
    broker.set_deals([_deal("D1", "O1", "BTC/USD", "BUY", 1, 60000.0)])
    service.sync_fills()
    assert service.sync_fills() == 0  # same deal not reprocessed


def test_sell_fill_closes_trade_with_pnl_and_commission(svc):
    service, broker, sf = svc
    buy = OrderState(order_id="O1", code="ETH/USD", side=Side.BUY, qty=2, price=3000)
    service.record_order(MARKET, buy, strategy="my_strat")
    broker.set_deals([_deal("D1", "O1", "ETH/USD", "BUY", 2, 3000.0, comm=1.0)])
    service.sync_fills()

    sell = OrderState(order_id="O2", code="ETH/USD", side=Side.SELL, qty=2, price=3100)
    service.record_order(MARKET, sell, strategy="my_strat")
    broker.set_deals([
        _deal("D1", "O1", "ETH/USD", "BUY", 2, 3000.0, comm=1.0),
        _deal("D2", "O2", "ETH/USD", "SELL", 2, 3100.0, comm=1.0),
    ])
    service.sync_fills()

    with UnitOfWork(sf) as uow:
        acc = _acc(uow)
        assert uow.trades.open_for_symbol(acc.id, "ETH/USD") is None  # closed
        t = uow.session.query(Trade).filter_by(account_id=acc.id).one()
        assert t.status == TradeStatus.CLOSED
        assert t.pnl_nominal == pytest.approx(200 - 2.0)     # (3100-3000)*2 - 2
        assert t.commission_total == pytest.approx(2.0)      # summed from both fills


def test_short_open_then_partial_and_full_close(svc):
    """/v6 shorts: SHORT_OPEN opens a SHORT trade; SHORT_CLOSE fills reduce and
    then close it, with PnL = (entry - exit) * qty."""
    service, broker, sf = svc
    so = OrderState(order_id="S1", code="BTC/USD", side=Side.SHORT_OPEN, qty=0.2,
                    price=50000)
    service.record_order(MARKET, so, strategy="my_strat")
    deals = [_deal("D1", "S1", "BTC/USD", "SHORT_OPEN", 0.2, 50000.0, comm=10.0)]
    broker.set_deals(deals)
    service.sync_fills()
    with UnitOfWork(sf) as uow:
        t = uow.trades.open_for_symbol(_acc(uow).id, "BTC/USD",
                                       direction=TradeDirection.SHORT)
        assert t is not None and t.qty == pytest.approx(0.2)

    # Short closes have no order id at placement -> adopted, attributed to the
    # deployment that owns the short only if it's the default strategy.
    service._default_strategy = "my_strat"
    deals += [_deal("D2", "C1", "BTC/USD", "SHORT_CLOSE", 0.1, 48000.0, comm=4.8)]
    broker.set_deals(deals)
    service.sync_fills()
    with UnitOfWork(sf) as uow:
        t = uow.trades.open_for_symbol(_acc(uow).id, "BTC/USD",
                                       direction=TradeDirection.SHORT)
        assert t.qty == pytest.approx(0.1)
        assert t.realized_pnl == pytest.approx(200.0)       # (50000-48000)*0.1

    deals += [_deal("D3", "C2", "BTC/USD", "SHORT_CLOSE", 0.1, 48000.0, comm=4.8)]
    broker.set_deals(deals)
    service.sync_fills()
    with UnitOfWork(sf) as uow:
        t = uow.session.query(Trade).filter_by(direction=TradeDirection.SHORT).one()
        assert t.status == TradeStatus.CLOSED
        assert t.realized_pnl == pytest.approx(400.0)
        assert t.qty == pytest.approx(0.2)                  # total round-trip size


def test_long_and_short_on_same_pair_are_separate_trades(svc):
    service, broker, sf = svc
    service._default_strategy = "my_strat"
    broker.set_deals([
        _deal("D1", "O1", "BTC/USD", "BUY", 0.1, 60000.0),
        _deal("D2", "O2", "BTC/USD", "SHORT_OPEN", 0.05, 60000.0),
    ])
    service.sync_fills()
    with UnitOfWork(sf) as uow:
        acc = _acc(uow)
        assert uow.trades.open_for_symbol(acc.id, "BTC/USD",
                                          direction=TradeDirection.LONG).qty == 0.1
        assert uow.trades.open_for_symbol(acc.id, "BTC/USD",
                                          direction=TradeDirection.SHORT).qty == 0.05
    assert service.open_ledger_qty("my_strat", "BTC/USD") == pytest.approx(0.05)


def test_adopts_order_for_unknown_fill(svc):
    service, broker, sf = svc
    broker.set_deals([_deal("D9", "O99", "BTC/USD", "BUY", 0.01, 59000.0)])
    assert service.sync_fills() == 1
    with UnitOfWork(sf) as uow:
        o = uow.orders.get_by_broker_id(_acc(uow).id, "O99")
        assert o is not None and o.strategy == "adopted"


def test_unknown_fill_goes_to_default_strategy(svc):
    service, broker, sf = svc
    service._default_strategy = "my_strat"
    broker.set_deals([_deal("D9", "O99", "BTC/USD", "BUY", 0.01, 59000.0)])
    service.sync_fills()
    with UnitOfWork(sf) as uow:
        assert uow.orders.get_by_broker_id(_acc(uow).id, "O99").strategy == "my_strat"


# ── equity snapshot ───────────────────────────────────────────────────
def test_snapshot_records_usd_equity_and_upserts_positions(svc):
    service, broker, sf = svc
    broker.set_account(
        cash=10000, total_assets=25000,
        positions={"BTC/USD": Position("BTC/USD", qty=0.25, avg_price=0,
                                       market_value=15000, currency="USD")},
    )
    service.snapshot_market(ts_utc=datetime(2026, 10, 1, 10, 0, tzinfo=UTC_TZ))

    with UnitOfWork(sf) as uow:
        acc = _acc(uow)
        snap = uow.equity.latest(acc.id)
        assert snap.total_equity == pytest.approx(25000)
        assert snap.reporting_ccy == "USD"
        ac = {a.asset_class.value: a.value for a in snap.assetclass_comp}
        assert ac["CASH"] == pytest.approx(10000)
        assert ac["CRYPTO"] == pytest.approx(15000)
        pos = uow.positions.current(acc.id)
        assert len(pos) == 1 and pos[0].symbol == "BTC/USD"
        assert pos[0].last_price == pytest.approx(60000)


# ── daily return rollup ───────────────────────────────────────────────
def test_rollup_daily_modified_dietz(svc):
    service, broker, sf = svc
    day = date(2026, 10, 1)
    broker.set_account(cash=10000, total_assets=10000, positions={})
    service.snapshot_market(ts_utc=datetime(2026, 10, 1, 1, 0, tzinfo=UTC_TZ))
    with UnitOfWork(sf) as uow:
        uow.cash_flows.record(_acc(uow).id, CashFlowKind.DEPOSIT, 1000, "USD",
                              ts_utc=datetime(2026, 10, 1, 12, 0, tzinfo=UTC_TZ))
    broker.set_account(cash=11500, total_assets=11500, positions={})
    service.snapshot_market(ts_utc=datetime(2026, 10, 1, 23, 0, tzinfo=UTC_TZ))

    service.rollup_daily(MARKET, day)
    with UnitOfWork(sf) as uow:
        from store.models import DailyReturn
        dr = uow.session.query(DailyReturn).filter_by(
            account_id=_acc(uow).id, date_utc=day).one()
        assert dr.pnl_nominal == pytest.approx(500, abs=1.0)   # 11500-10000-1000
        assert dr.net_flow == pytest.approx(1000)
