"""Engine-level wiring: run_once drives snapshots + rebalances through fakes."""
from dataclasses import replace

import pytest

from core.config import EngineConfig, RiskConfig, StrategyConfig, load_config
from core.enums import Side
from core.models import MARKET, AccountSnapshot, OrderState, PairRule, Position, Ticker
from engine.fx import ConfigFxProvider
from engine.recorder import PersistenceService
from engine.trading_engine import TradingEngine
from execution.order_manager import OrderManager
from execution.portfolio import Portfolio
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager
from signals.base import Strategy
from signals.example_noop import NoOpStrategy
from store.db import make_engine, make_session_factory
from store.models import Base, Order
from store.repository import UnitOfWork

RULES = {"BTC/USD": PairRule("BTC/USD", 2, 5, 1.0),
         "ETH/USD": PairRule("ETH/USD", 2, 4, 1.0)}
TICKERS = {"BTC/USD": Ticker("BTC/USD", 59990, 60010, 60000),
           "ETH/USD": Ticker("ETH/USD", 2999, 3001, 3000)}


class FakeMarketData:
    def tickers(self, force=False): return TICKERS
    def pair_rules(self, force=False): return RULES
    def bars(self, pair, interval, limit): return []


class FakeBroker:
    def __init__(self, cash=10_000.0):
        self.cash = cash
        self.legs = []
        self.placed = []

    def acc_id(self, market=MARKET): return "roostoo-test-x"

    def account(self, market=MARKET):
        mv = sum(p.market_value for p in self.legs)
        return AccountSnapshot(MARKET, cash=self.cash, total_assets=self.cash + mv,
                               buying_power=self.cash, currency="USD",
                               positions={p.code: p for p in self.legs})

    def position_legs(self, market=MARKET): return list(self.legs)
    def positions(self, market=MARKET): return {p.code: p for p in self.legs}
    def deals(self, market=MARKET): return []
    def open_orders(self, market=MARKET): return []
    def cancel_all(self, pair=None): return []

    def place(self, req):
        self.placed.append(req)
        return OrderState(order_id=f"O{len(self.placed)}", code=req.code, side=req.side,
                          qty=req.qty, price=req.price)


class FixedWeights(Strategy):
    name = "fixed"

    def __init__(self, weights):
        super().__init__()
        self.weights = weights
        self.calls = 0

    def target_weights(self, ctx):
        self.calls += 1
        return self.weights


def _engine(tmp_path, strategy, broker, clock=lambda: 7200.0 + 10):
    base = load_config()
    cfg = replace(base,
                  strategy=StrategyConfig(name=strategy.name, pairs=["BTC/USD", "ETH/USD"],
                                          rebalance_seconds=3600),
                  risk=RiskConfig(max_gross_exposure=1.0, max_weight_per_pair=0.6,
                                  cash_buffer_pct=0.0, min_order_usd=10),
                  engine=EngineConfig(poll_interval_seconds=60))
    db = make_engine(tmp_path / "eng.db")
    Base.metadata.create_all(db)
    sf = make_session_factory(db)
    recorder = PersistenceService(sf, broker, ConfigFxProvider({}), trd_env="TEST")
    eng = TradingEngine(
        cfg=cfg, market_data=FakeMarketData(), strategy=strategy,
        sizer=PositionSizer(cfg.risk.min_order_usd), risk=RiskManager(cfg.risk),
        portfolio=Portfolio(broker), orders=OrderManager(broker),
        recorder=recorder, wall_clock=clock,
    )
    return eng, sf


def test_run_once_writes_snapshot(tmp_path):
    broker = FakeBroker()
    eng, sf = _engine(tmp_path, NoOpStrategy(), broker)
    eng.run_once()
    with UnitOfWork(sf) as uow:
        acc = uow.accounts.get_or_create("ROOSTOO", "roostoo-test-x", "TEST")
        assert uow.equity.latest(acc.id).total_equity == pytest.approx(10_000)
    assert broker.placed == []                    # no-op strategy never trades


def test_rebalance_buys_to_target_and_records_orders(tmp_path):
    broker = FakeBroker()
    eng, sf = _engine(tmp_path, FixedWeights({"BTC/USD": 0.5}), broker)
    eng.run_once()
    assert len(broker.placed) == 1
    o = broker.placed[0]
    assert o.side == Side.BUY and o.code == "BTC/USD"
    assert o.qty == pytest.approx(5000 / 60000, abs=1e-5)     # 50% of $10k at mid
    with UnitOfWork(sf) as uow:
        row = uow.session.query(Order).one()
        assert row.strategy == "fixed" and row.broker_order_id == "O1"


def test_rebalance_runs_once_per_period(tmp_path):
    t = {"now": 7200.0 + 10}
    strat = FixedWeights({"BTC/USD": 0.5})
    eng, _ = _engine(tmp_path, strat, FakeBroker(), clock=lambda: t["now"])
    eng.run_once()
    t["now"] += 60
    eng.run_once()                    # same hourly bucket -> no second call
    assert strat.calls == 1
    t["now"] += 3600
    eng.run_once()
    assert strat.calls == 2


def test_omitted_pair_is_flattened(tmp_path):
    broker = FakeBroker(cash=4000)
    broker.legs = [Position("ETH/USD", 2.0, 0, market_value=6000)]
    eng, _ = _engine(tmp_path, FixedWeights({"BTC/USD": 0.0}), broker)
    eng.run_once()
    assert [(o.side, o.code, o.qty) for o in broker.placed] == [(Side.SELL, "ETH/USD", 2.0)]


def test_none_means_hold(tmp_path):
    broker = FakeBroker(cash=4000)
    broker.legs = [Position("ETH/USD", 2.0, 0, market_value=6000)]
    eng, _ = _engine(tmp_path, FixedWeights(None), broker)
    eng.run_once()
    assert broker.placed == []
