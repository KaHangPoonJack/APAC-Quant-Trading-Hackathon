"""Reboot recovery reconciliation — per (pair, direction) leg, plus the strategy hook."""
import pytest

from core.models import MARKET, AccountSnapshot, Position
from engine.fx import ConfigFxProvider
from engine.recorder import PersistenceService
from engine.recovery import RecoveryService
from store.db import make_engine, make_session_factory
from store.enums import TradeDirection, TradeStatus
from store.models import Base, Trade
from store.repository import UnitOfWork

ACC = "roostoo-test-abc"


class FakeBroker:
    """Legs/deals are injectable data. deals=[] so sync_fills is a no-op."""

    def __init__(self):
        self.legs = []

    def acc_id(self, market=MARKET):
        return ACC

    def account(self, market=MARKET):
        return AccountSnapshot(MARKET, 0, 0, 0)

    def position_legs(self, market=MARKET):
        return list(self.legs)

    def deals(self, market=MARKET):
        return []


class StubStrategy:
    name = "my_strat"

    def __init__(self):
        self.recovered = None

    def on_recover(self, legs):
        self.recovered = legs


@pytest.fixture()
def harness(tmp_path):
    engine = make_engine(tmp_path / "rec.db")
    Base.metadata.create_all(engine)
    sf = make_session_factory(engine)
    broker = FakeBroker()
    recorder = PersistenceService(sf, broker, ConfigFxProvider({}), trd_env="TEST")
    strategy = StubStrategy()
    recovery = RecoveryService(sf, broker, recorder, strategy)
    recorder.account_id()  # ensure the account row exists
    return recovery, broker, strategy, sf


def _seed(sf, symbol, qty, entry, direction=TradeDirection.LONG):
    with UnitOfWork(sf) as uow:
        acc = uow.accounts.get_or_create("ROOSTOO", ACC, "TEST")
        uow.trades.open_trade(acc.id, symbol, direction, qty, entry, strategy="my_strat")


def _open(sf, symbol, direction=None):
    with UnitOfWork(sf) as uow:
        acc = uow.accounts.get_or_create("ROOSTOO", ACC, "TEST")
        return uow.trades.open_for_symbol(acc.id, symbol, direction=direction)


def test_resume_when_broker_still_holds(harness):
    recovery, broker, strategy, sf = harness
    _seed(sf, "BTC/USD", 0.5, 60000)
    broker.legs = [Position("BTC/USD", 0.5, 0, market_value=30000)]
    report = recovery.recover()
    assert "BTC/USD LONG" in report.resumed
    assert _open(sf, "BTC/USD") is not None
    assert strategy.recovered and strategy.recovered[0].code == "BTC/USD"


def test_close_when_broker_flat(harness):
    recovery, broker, strategy, sf = harness
    _seed(sf, "BTC/USD", 0.5, 60000)
    report = recovery.recover()
    assert "BTC/USD LONG" in report.closed
    assert _open(sf, "BTC/USD") is None
    with UnitOfWork(sf) as uow:
        assert uow.session.query(Trade).one().status == TradeStatus.CLOSED


def test_adopt_orphan_legs_long_and_short(harness):
    recovery, broker, strategy, sf = harness
    broker.legs = [Position("ETH/USD", 2.0, 0, market_value=6000),
                   Position("BTC/USD", -0.1, 50000, market_value=-5000)]
    report = recovery.recover()
    assert set(report.adopted) == {"ETH/USD LONG", "BTC/USD SHORT"}
    short = _open(sf, "BTC/USD", TradeDirection.SHORT)
    assert short is not None and short.qty == pytest.approx(0.1)
    assert short.entry_price == 50000
    assert _open(sf, "ETH/USD", TradeDirection.LONG).qty == pytest.approx(2.0)


def test_adjust_qty_mismatch(harness):
    recovery, broker, strategy, sf = harness
    _seed(sf, "BTC/USD", 0.5, 60000)
    broker.legs = [Position("BTC/USD", 0.3, 0, market_value=18000)]
    report = recovery.recover()
    assert "BTC/USD LONG" in report.adjusted
    assert _open(sf, "BTC/USD").qty == pytest.approx(0.3)


def test_long_trade_is_not_matched_against_a_short_leg(harness):
    """A DB LONG with only a broker SHORT on the same pair: the long is gone
    (closed) and the short is adopted — legs never cross-match."""
    recovery, broker, strategy, sf = harness
    _seed(sf, "BTC/USD", 0.5, 60000)
    broker.legs = [Position("BTC/USD", -0.2, 61000, market_value=-12200)]
    report = recovery.recover()
    assert "BTC/USD LONG" in report.closed
    assert "BTC/USD SHORT" in report.adopted
