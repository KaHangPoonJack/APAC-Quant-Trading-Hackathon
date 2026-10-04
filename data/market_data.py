"""Market-data facade: Binance bars for signals, Roostoo ticker/rules for trading.

Two sources, one object, so the engine and broker never care where a number
came from:
  * bars       -> Binance spot klines (free: no Roostoo rate-limit cost)
  * tickers    -> Roostoo /v3/ticker — the EXECUTABLE prices (MaxBid/MinAsk).
                  ONE call returns every pair; cached for `ticker_ttl` seconds so
                  the engine and broker share a single call per poll.
  * pair rules -> Roostoo /v3/exchangeInfo (precision, MiniOrder), cached for
                  `rules_ttl` seconds (rules change rarely).

The Roostoo client is injected (duck-typed: `ticker()`, `exchange_info()`), so
this module needs no broker import and is testable with a fake.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Dict, List, Optional

from core.models import Bar, PairRule, Ticker
from data.binance import BinanceSpot

log = logging.getLogger(__name__)


def num(d: dict, key: str, default: float = 0.0) -> float:
    """Roostoo omits zero-valued fields — read missing/blank as `default`."""
    try:
        v = d.get(key)
        return float(v) if v is not None and v != "" else default
    except (TypeError, ValueError):
        return default


def parse_tickers(data: Dict[str, dict]) -> Dict[str, Ticker]:
    out: Dict[str, Ticker] = {}
    for pair, t in (data or {}).items():
        out[pair] = Ticker(
            code=pair, bid=num(t, "MaxBid"), ask=num(t, "MinAsk"),
            last=num(t, "LastPrice"), change_24h=num(t, "Change"),
            quote_volume=num(t, "UnitTradeValue"),
        )
    return out


def parse_pair_rules(info: dict) -> Dict[str, PairRule]:
    out: Dict[str, PairRule] = {}
    for pair, r in (info.get("TradePairs") or {}).items():
        out[pair] = PairRule(
            code=pair,
            price_precision=int(num(r, "PricePrecision")),
            amount_precision=int(num(r, "AmountPrecision")),
            min_notional=num(r, "MiniOrder"),
            can_trade=bool(r.get("CanTrade", True)),
            asset_type=str(r.get("AssetType", "crypto") or "crypto"),
        )
    return out


class MarketData:
    def __init__(self, roostoo, binance: BinanceSpot, ticker_ttl: float = 10.0,
                 rules_ttl: float = 6 * 3600.0,
                 clock: Callable[[], float] = time.monotonic,
                 futures: Optional[BinanceSpot] = None):
        self._roostoo = roostoo
        self.binance = binance
        self.futures = futures
        self._ticker_ttl = ticker_ttl
        self._rules_ttl = rules_ttl
        self._clock = clock
        self._tickers: Dict[str, Ticker] = {}
        self._tickers_at: Optional[float] = None
        self._rules: Dict[str, PairRule] = {}
        self._rules_at: Optional[float] = None

    # -- Roostoo (rate-limited) ---------------------------------------------
    def tickers(self, force: bool = False) -> Dict[str, Ticker]:
        """Every pair's executable prices; one Roostoo call per `ticker_ttl`."""
        now = self._clock()
        if force or self._tickers_at is None or now - self._tickers_at >= self._ticker_ttl:
            self._tickers = parse_tickers(self._roostoo.ticker())
            self._tickers_at = now
        return self._tickers

    def ticker(self, pair: str) -> Optional[Ticker]:
        return self.tickers().get(pair)

    def pair_rules(self, force: bool = False) -> Dict[str, PairRule]:
        now = self._clock()
        if force or self._rules_at is None or now - self._rules_at >= self._rules_ttl:
            try:
                self._rules = parse_pair_rules(self._roostoo.exchange_info())
                self._rules_at = now
            except Exception as exc:  # noqa: BLE001 — keep the last good copy
                if not self._rules:
                    raise
                log.warning("exchangeInfo refresh failed, keeping cached rules: %s", exc)
        return self._rules

    def rule(self, pair: str) -> Optional[PairRule]:
        return self.pair_rules().get(pair)

    # -- Binance (free) -----------------------------------------------------
    def bars(self, pair: str, interval: str, limit: int) -> List[Bar]:
        return self.binance.klines(pair, interval=interval, limit=limit)

    def perp_bars(self, pair: str, interval: str, limit: int) -> List[Bar]:
        if self.futures is None:
            raise RuntimeError("no futures market-data source configured")
        return self.futures.klines(pair, interval=interval, limit=limit)
