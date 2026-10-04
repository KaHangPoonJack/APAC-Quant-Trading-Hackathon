"""Binance SPOT public market data — the OHLCV source.

Roostoo offers only a ticker snapshot (no candles, FAQ Q18), and its prices are
streamed from Binance (FAQ Q17), so Binance spot klines are the natural bar
source for signals. Every endpoint here is PUBLIC: no key, no account, and no
order endpoint — this module cannot trade by construction.

    /api/v3/exchangeInfo        symbols + filters
    /api/v3/klines              OHLCV candles (max 1000 per request)
    /api/v3/ticker/bookTicker   best bid/ask

Binance requests do NOT count against Roostoo's 30 calls/min budget.

Host: `data-api.binance.vision` is Binance's official market-data-only mirror.
`api.binance.com` answers HTTP 451 from restricted regions (seen from the dev
machine), so the mirror is the default and the other host is tried on 451/403.

Pair mapping: Roostoo `BTC/USD` <-> Binance `BTCUSDT` (quote asset is config,
`binance.quote_asset`). Bars carry the ROOSTOO pair as `code`, so strategies
never see Binance symbols.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from core.models import Bar, coin_of

log = logging.getLogger(__name__)

BASE_URL = "https://data-api.binance.vision/api/v3"
FALLBACK_URLS = ("https://data-api.binance.vision/api/v3", "https://api.binance.com/api/v3")
MAX_KLINES = 1000

# Interval -> milliseconds (for paging and "is this bar closed" checks).
INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000, "3d": 259_200_000,
    "1w": 604_800_000,
}


class BinanceError(RuntimeError):
    """Binance API returned an error, or malformed/empty data."""


def to_binance_symbol(pair: str, quote_asset: str = "USDT") -> str:
    """'BTC/USD' -> 'BTCUSDT'."""
    return f"{coin_of(pair)}{quote_asset.upper()}"


def to_pair(symbol: str, quote_asset: str = "USDT", unit: str = "USD") -> str:
    """'BTCUSDT' -> 'BTC/USD'."""
    sym, q = symbol.upper(), quote_asset.upper()
    if not sym.endswith(q):
        raise ValueError(f"{symbol!r} is not quoted in {quote_asset}")
    return f"{sym[:-len(q)]}/{unit}"


def parse_klines(rows: list, pair: str, now_ms: Optional[int] = None,
                 closed_only: bool = True) -> List[Bar]:
    """Binance kline rows -> Bars (ascending, open time UTC). With
    `closed_only`, the still-forming last candle is dropped so signals only
    ever see completed bars."""
    if not isinstance(rows, list):
        raise BinanceError(f"bad klines payload for {pair}")
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    out: List[Bar] = []
    for r in rows:
        if closed_only and int(r[6]) >= now_ms:      # close time not yet reached
            continue
        out.append(Bar(
            code=pair,
            time=datetime.fromtimestamp(int(r[0]) / 1000, tz=timezone.utc),
            open=float(r[1]), high=float(r[2]), low=float(r[3]),
            close=float(r[4]), volume=float(r[5]), quote_volume=float(r[7]),
        ))
    return out


class BinanceSpot:
    def __init__(self, base_url: str = BASE_URL, quote_asset: str = "USDT",
                 timeout: float = 10.0, min_request_interval_s: float = 0.1,
                 max_retries: int = 3):
        self._base = base_url.rstrip("/")
        self._fallbacks = [u for u in FALLBACK_URLS if u != self._base]
        self.quote_asset = quote_asset.upper()
        self._timeout = timeout
        self._throttle = min_request_interval_s
        self._retries = max_retries
        self._last_call = 0.0

    # -- HTTP -------------------------------------------------------------
    def _get(self, path: str, **params):
        try:
            return self._get_from(self._base, path, **params)
        except BinanceError as exc:
            if not getattr(exc, "geo_blocked", False):
                raise
            for alt in self._fallbacks:
                log.warning("Binance %s geo-blocked (%s) — switching to %s",
                            self._base, exc, alt)
                self._base = alt
                self._fallbacks = [u for u in self._fallbacks if u != alt]
                return self._get_from(alt, path, **params)
            raise

    def _get_from(self, base: str, path: str, **params):
        url = f"{base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        last_exc: Optional[Exception] = None
        for attempt in range(self._retries):
            wait = self._throttle - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            try:
                req = urllib.request.Request(url, headers={"accept": "application/json"})
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                # 418/429 = rate limited; back off and retry. Other 4xx are fatal.
                if exc.code in (418, 429) and attempt < self._retries - 1:
                    time.sleep(2.0 * (attempt + 1))
                    last_exc = exc
                    continue
                if exc.code >= 500 and attempt < self._retries - 1:
                    time.sleep(1.0 * (attempt + 1))
                    last_exc = exc
                    continue
                err = BinanceError(f"HTTP {exc.code} for {path}: {exc.reason}")
                err.geo_blocked = exc.code in (403, 451)
                raise err from exc
            except Exception as exc:  # noqa: BLE001 — network flakiness
                last_exc = exc
                if attempt == self._retries - 1:
                    break
                time.sleep(1.0 * (attempt + 1))
        raise BinanceError(f"request failed {path}: {last_exc}")

    # -- endpoints --------------------------------------------------------
    def symbol(self, pair: str) -> str:
        return to_binance_symbol(pair, self.quote_asset)

    def klines(self, pair: str, interval: str = "1h", limit: int = 200,
               closed_only: bool = True) -> List[Bar]:
        """The most recent `limit` bars for a ROOSTOO pair, ascending."""
        if interval not in INTERVAL_MS:
            raise ValueError(f"unsupported interval {interval!r}")
        # +1 because the forming bar is usually dropped.
        n = min(int(limit) + (1 if closed_only else 0), MAX_KLINES)
        rows = self._get("/klines", symbol=self.symbol(pair), interval=interval, limit=n)
        bars = parse_klines(rows, pair, closed_only=closed_only)
        return bars[-int(limit):]

    def klines_range(self, pair: str, interval: str, start: datetime,
                     end: Optional[datetime] = None) -> List[Bar]:
        """Closed bars in [start, end), paging through Binance's 1000-row cap.
        For research / backfills — the live loop uses `klines`."""
        if interval not in INTERVAL_MS:
            raise ValueError(f"unsupported interval {interval!r}")
        step = INTERVAL_MS[interval]
        cur = int(start.timestamp() * 1000)
        stop = int((end or datetime.now(timezone.utc)).timestamp() * 1000)
        out: List[Bar] = []
        while cur < stop:
            rows = self._get("/klines", symbol=self.symbol(pair), interval=interval,
                             startTime=cur, endTime=stop - 1, limit=MAX_KLINES)
            if not rows:
                break
            out.extend(parse_klines(rows, pair))
            cur = int(rows[-1][0]) + step
            if len(rows) < MAX_KLINES:
                break
        return out

    def book_ticker(self, pairs: Optional[Sequence[str]] = None
                    ) -> Dict[str, Tuple[float, float]]:
        """{pair: (best_bid, best_ask)} — one request for all pairs."""
        rows = self._get("/ticker/bookTicker")
        if isinstance(rows, dict):
            rows = [rows]
        wanted = {self.symbol(p): p for p in pairs} if pairs else None
        out: Dict[str, Tuple[float, float]] = {}
        for r in rows:
            sym = r.get("symbol", "")
            if wanted is not None:
                if sym not in wanted:
                    continue
                pair = wanted[sym]
            else:
                try:
                    pair = to_pair(sym, self.quote_asset)
                except ValueError:
                    continue
            try:
                bid, ask = float(r["bidPrice"]), float(r["askPrice"])
            except (KeyError, TypeError, ValueError):
                continue
            if bid > 0 and ask > 0:
                out[pair] = (bid, ask)
        return out

    def trading_symbols(self) -> List[str]:
        """Binance spot symbols currently TRADING in our quote asset."""
        body = self._get("/exchangeInfo")
        syms = body.get("symbols") if isinstance(body, dict) else None
        if not isinstance(syms, list) or not syms:
            raise BinanceError("exchangeInfo returned no symbols")
        return [s["symbol"] for s in syms
                if s.get("status") == "TRADING" and s.get("quoteAsset") == self.quote_asset]


FUTURES_URL = "https://fapi.binance.com/fapi/v1"


class BinanceFutures(BinanceSpot):
    """Binance USDS-M perpetuals, PUBLIC klines only. Used for liquidity (dollar
    volume), which is several times spot volume and is what the momentum
    research screened on. fapi has no market-data mirror, so it is reachable
    only where binance.com is (HTTP 451 from e.g. the US) — callers must
    tolerate failure.

    Some coins list as 1000-unit contracts (BONK/USD -> 1000BONKUSDT), so the
    symbol is resolved once against the contract list."""

    def __init__(self, base_url: str = FUTURES_URL, **kwargs):
        super().__init__(base_url, **kwargs)
        self._fallbacks = []
        self._symbols: Optional[set] = None

    def symbol(self, pair: str) -> str:
        if self._symbols is None:
            self._symbols = set(self.trading_symbols())
        coin, q = coin_of(pair), self.quote_asset
        for sym in (coin + q, coin.replace("1000", "") + q, "1000" + coin + q):
            if sym in self._symbols:
                return sym
        raise BinanceError(f"no USDS-M perpetual for {pair}")
