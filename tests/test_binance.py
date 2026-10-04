"""Binance spot client parsing + pair mapping (no network)."""
from datetime import datetime, timezone

import pytest

from data.binance import (BinanceError, BinanceFutures, BinanceSpot, parse_klines,
                          to_binance_symbol, to_pair)
from data.market_data import MarketData, parse_pair_rules, parse_tickers


def _row(open_ms, close_ms, o=1, h=2, l=0.5, c=1.5, v=10):
    return [open_ms, str(o), str(h), str(l), str(c), str(v), close_ms, str(c * v), 0, "0", "0", "0"]


def test_pair_mapping_roundtrip():
    assert to_binance_symbol("BTC/USD") == "BTCUSDT"
    assert to_pair("ETHUSDT") == "ETH/USD"
    with pytest.raises(ValueError):
        to_pair("ETHBTC")


def test_parse_klines_drops_forming_bar_and_uses_pair_code():
    rows = [_row(0, 3_599_999), _row(3_600_000, 7_199_999)]
    bars = parse_klines(rows, "BTC/USD", now_ms=5_000_000)
    assert len(bars) == 1
    b = bars[0]
    assert b.code == "BTC/USD" and b.close == 1.5 and b.volume == 10 and b.quote_volume == 15
    assert b.time == datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert len(parse_klines(rows, "BTC/USD", now_ms=5_000_000, closed_only=False)) == 2


def test_parse_klines_rejects_bad_payload():
    with pytest.raises(BinanceError):
        parse_klines({"code": -1121}, "BTC/USD")


def test_klines_requests_binance_symbol_and_trims_to_limit(monkeypatch):
    sent = {}
    client = BinanceSpot()

    def fake_get(path, **params):
        sent.update(params, path=path)
        return [_row(i * 3_600_000, i * 3_600_000 + 3_599_999) for i in range(6)]

    monkeypatch.setattr(client, "_get", fake_get)
    bars = client.klines("ETH/USD", "1h", limit=3)
    assert sent["symbol"] == "ETHUSDT" and sent["interval"] == "1h" and sent["limit"] == 4
    assert len(bars) == 3 and all(b.code == "ETH/USD" for b in bars)


def test_unknown_interval_rejected():
    with pytest.raises(ValueError):
        BinanceSpot().klines("BTC/USD", "7m")


# ── Roostoo payload parsing (data/market_data.py) ─────────────────────
def test_parse_tickers_and_rules():
    t = parse_tickers({"BTC/USD": {"MaxBid": 100, "MinAsk": 102, "LastPrice": 101}})
    assert t["BTC/USD"].mid == 101 and t["BTC/USD"].change_24h == 0.0   # omitted = 0
    rules = parse_pair_rules({"TradePairs": {"NVDAB/USD": {
        "PricePrecision": 2, "AmountPrecision": 3, "MiniOrder": 1, "CanTrade": True,
        "AssetType": "stock"}}})
    r = rules["NVDAB/USD"]
    assert r.amount_precision == 3 and r.min_notional == 1 and r.asset_type == "stock"


def test_market_data_caches_tickers_for_ttl():
    class C:
        n = 0

        def ticker(self):
            C.n += 1
            return {"BTC/USD": {"MaxBid": 1, "MinAsk": 1}}

    now = {"t": 0.0}
    md = MarketData(C(), BinanceSpot(), ticker_ttl=10, clock=lambda: now["t"])
    md.tickers(); md.tickers()
    assert C.n == 1
    now["t"] = 11
    md.tickers()
    assert C.n == 2
    md.tickers(force=True)
    assert C.n == 3


def test_futures_resolves_1000_contracts_and_has_no_spot_fallback(monkeypatch):
    client = BinanceFutures()
    assert client._fallbacks == []
    sent = {}

    def fake_get(path, **params):
        if path == "/exchangeInfo":
            return {"symbols": [{"symbol": s, "status": "TRADING", "quoteAsset": "USDT"}
                                for s in ("BTCUSDT", "1000BONKUSDT", "1000CHEEMSUSDT")]}
        sent.update(params)
        return [_row(0, 86_399_999)]

    monkeypatch.setattr(client, "_get", fake_get)
    assert client.symbol("BTC/USD") == "BTCUSDT"
    assert client.symbol("BONK/USD") == "1000BONKUSDT"
    assert client.symbol("1000CHEEMS/USD") == "1000CHEEMSUSDT"
    with pytest.raises(BinanceError):
        client.symbol("TON/USD")
    client.klines("BONK/USD", "1d", limit=1)
    assert sent["symbol"] == "1000BONKUSDT"
