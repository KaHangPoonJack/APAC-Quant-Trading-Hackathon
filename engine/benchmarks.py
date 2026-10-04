"""Benchmark prices (Binance spot daily closes), stored in the DB.

The engine calls `update()` on startup (backfill) and once per day. It never
raises into the trading loop. Benchmark symbols are Binance spot symbols
(e.g. BTCUSDT) from `benchmarks.indices` in settings.yaml.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session, sessionmaker

from core.config import BenchmarkConfig
from data.binance import BinanceSpot, to_pair
from store.repository import UnitOfWork

log = logging.getLogger(__name__)


class BenchmarkFetcher:
    def __init__(self, session_factory: sessionmaker[Session], cfg: BenchmarkConfig,
                 binance: BinanceSpot):
        self._sf = session_factory
        self._cfg = cfg
        self._binance = binance

    def update(self) -> int:
        """Fetch each benchmark's daily closes and upsert. Returns rows written."""
        if not self._cfg.enabled or not self._cfg.indices:
            return 0
        start = datetime.now(timezone.utc) - timedelta(days=max(1, self._cfg.history_days))
        written = 0
        for entry in self._cfg.indices:
            symbol, name = entry["symbol"], entry.get("name", entry["symbol"])
            try:
                pair = to_pair(symbol, self._binance.quote_asset)
                bars = self._binance.klines_range(pair, "1d", start)
            except Exception as exc:  # noqa: BLE001 - network/parse issues are non-fatal
                log.error("benchmark fetch failed for %s: %s", symbol, exc)
                continue
            if not bars:
                log.warning("no benchmark data for %s", symbol)
                continue
            with UnitOfWork(self._sf) as uow:
                for b in bars:
                    uow.benchmarks.upsert(symbol, name, b.time.date(), float(b.close))
                    written += 1
        if written:
            log.info("benchmarks updated: %d rows across %d symbols",
                     written, len(self._cfg.indices))
        return written
