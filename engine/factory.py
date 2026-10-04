"""Composition root: builds a fully-wired TradingEngine from config.

Keeping construction in one place means entrypoints stay tiny and tests can
build the same object graph with fakes swapped in.

To add a strategy: implement it in signals/, register the class in STRATEGIES
below, and set `strategy.name` in config/settings.yaml.
"""
from __future__ import annotations

import logging
from typing import Dict, Type

from core.config import AppConfig
from core.notifier import build_registry_logged
from data.binance import BinanceFutures, BinanceSpot
from data.market_data import MarketData
from engine.benchmarks import BenchmarkFetcher
from engine.fx import ConfigFxProvider
from engine.recorder import PersistenceService
from engine.recovery import RecoveryService
from engine.trading_engine import TradingEngine
from execution.broker import RoostooBroker
from execution.order_manager import OrderManager
from execution.portfolio import Portfolio
from execution.roostoo_client import RoostooClient
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager
from signals.base import Strategy
from signals.example_noop import NoOpStrategy
from signals.xs_momentum import CrossSectionalMomentum
from store.db import make_engine, make_session_factory

log = logging.getLogger(__name__)

# strategy.name -> class. Register new strategies here.
STRATEGIES: Dict[str, Type[Strategy]] = {
    NoOpStrategy.name: NoOpStrategy,
    CrossSectionalMomentum.name: CrossSectionalMomentum,
}


def build_strategy(cfg: AppConfig) -> Strategy:
    try:
        cls = STRATEGIES[cfg.strategy.name]
    except KeyError as exc:
        raise KeyError(f"unknown strategy {cfg.strategy.name!r}; registered: "
                       f"{sorted(STRATEGIES)}") from exc
    strategy = cls(cfg.strategy.params)
    strategy.name = cfg.strategy.name
    return strategy


def build_client(cfg: AppConfig) -> RoostooClient:
    r = cfg.roostoo
    return RoostooClient(
        api_key=r.api_key, secret_key=r.secret_key, base_url=r.base_url,
        max_calls_per_minute=r.max_calls_per_minute, timeout=r.timeout_seconds,
        max_retries=r.max_retries,
    )


def build_market_data(cfg: AppConfig, client: RoostooClient) -> MarketData:
    binance = BinanceSpot(cfg.binance.base_url, quote_asset=cfg.binance.quote_asset,
                          timeout=cfg.binance.timeout_seconds)
    futures = None
    if cfg.strategy.volume_source == "perp":
        futures = BinanceFutures(cfg.binance.futures_base_url,
                                 quote_asset=cfg.binance.quote_asset,
                                 timeout=cfg.binance.timeout_seconds)
    return MarketData(client, binance, futures=futures)


def build_engine(cfg: AppConfig) -> TradingEngine:
    client = build_client(cfg)
    market_data = build_market_data(cfg, client)
    broker = RoostooBroker(client, market_data, env=cfg.roostoo.env,
                           include_shorts=cfg.risk.allow_short)
    strategy = build_strategy(cfg)
    sizer = PositionSizer(cfg.risk.min_order_usd)
    risk = RiskManager(cfg.risk)
    portfolio = Portfolio(broker)
    orders = OrderManager(broker)

    notifiers = build_registry_logged(cfg.notifications, log)

    recorder = recovery = benchmarks = None
    if cfg.persistence.enabled:
        session_factory = make_session_factory(make_engine(cfg.persistence.db_path))
        recorder = PersistenceService(
            session_factory=session_factory, broker=broker,
            fx=ConfigFxProvider(cfg.fx.rates),
            broker_name="ROOSTOO", reporting_ccy=cfg.fx.reporting_currency,
            trd_env=cfg.roostoo.env, notifiers=notifiers, allocations=cfg.allocations,
            default_strategy=strategy.name,
        )
        recovery = RecoveryService(session_factory=session_factory, broker=broker,
                                   recorder=recorder, strategy=strategy)
        if cfg.benchmarks.enabled:
            benchmarks = BenchmarkFetcher(session_factory, cfg.benchmarks,
                                          market_data.binance)

    return TradingEngine(
        cfg=cfg, market_data=market_data, strategy=strategy, sizer=sizer,
        risk=risk, portfolio=portfolio, orders=orders, client=client,
        recorder=recorder, recovery=recovery, benchmarks=benchmarks,
        notifier=notifiers.system,
    )
