"""RoostooClient protocol rules, offline: signing, Success flag, rate limit,
retries. A fake transport records every request."""
import json
import urllib.parse

import pytest

from execution.roostoo_client import (
    RateLimiter, RoostooClient, RoostooError, fmt_decimal, num, sign, total_params,
)


# ── signing (vector from the official API README) ─────────────────────
def test_signature_matches_official_example():
    params = {"pair": "BNB/USD", "quantity": "2000", "side": "BUY",
              "timestamp": "1580774512000", "type": "MARKET"}
    secret = "S1XP1e3UZj6A7H5fATj0jNhqPxxdSJYdInClVN65XAbvqqMKjVHjA7PZj4W12oep"
    assert total_params(params) == \
        "pair=BNB/USD&quantity=2000&side=BUY&timestamp=1580774512000&type=MARKET"
    assert sign(params, secret) == \
        "20b7fd5550b67b3bf0c1684ed0f04885261db8fdabd38611e9e6af23c19b7fff"


def test_total_params_is_key_sorted_regardless_of_insertion_order():
    assert total_params({"b": 2, "a": 1}) == "a=1&b=2"


# ── fake transport ─────────────────────────────────────────────────────
class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": body.decode() if body else None})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        status, payload = r
        return status, payload if isinstance(payload, str) else json.dumps(payload)


class NoLimit(RateLimiter):
    def __init__(self):
        super().__init__(10_000)


def _client(responses, **kw):
    t = FakeTransport(responses)
    c = RoostooClient("KEY", "SECRET", transport=t, limiter=NoLimit(),
                      clock_ms=lambda: 1_700_000_000_000, sleep=lambda s: None, **kw)
    return c, t


def test_post_is_signed_form_body_with_headers():
    c, t = _client([(200, {"Success": True, "OrderDetail": {"OrderID": 7}})])
    d = c.place_order("BTC/USD", "BUY", "0.01")
    assert d["OrderID"] == 7
    call = t.calls[0]
    assert call["method"] == "POST" and call["url"].endswith("/v3/place_order")
    assert call["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert call["headers"]["RST-API-KEY"] == "KEY"
    params = dict(urllib.parse.parse_qsl(call["body"]))
    assert params == {"pair": "BTC/USD", "quantity": "0.01", "side": "BUY",
                      "timestamp": "1700000000000", "type": "MARKET"}
    assert call["headers"]["MSG-SIGNATURE"] == sign(params, "SECRET")


def test_get_signed_uses_query_string_and_never_sends_none_params():
    c, t = _client([(200, {"Success": True, "Wallet": {"USD": {"Free": 5}}})])
    assert c.balance() == {"USD": {"Free": 5}}
    url = t.calls[0]["url"]
    assert "timestamp=1700000000000" in url and t.calls[0]["body"] is None
    c2, t2 = _client([(200, {"Success": True, "OrderMatched": []})])
    c2.query_order(pair="BTC/USD")
    sent = dict(urllib.parse.parse_qsl(t2.calls[0]["body"]))
    assert set(sent) == {"pair", "timestamp"}       # no None-valued extras signed


def test_server_time_offset_applied_to_timestamp():
    c, t = _client([(200, {"ServerTime": 1_700_000_005_000}),
                    (200, {"Success": True, "Wallet": {}})])
    c.sync_time()
    assert c.offset_ms == 5_000
    c.balance()
    assert "timestamp=1700000005000" in t.calls[1]["url"]


def test_success_false_raises():
    c, _ = _client([(200, {"Success": False, "ErrMsg": "insufficient balance"})])
    with pytest.raises(RoostooError, match="insufficient balance"):
        c.place_order("BTC/USD", "BUY", "1")


def test_benign_empty_results_are_not_errors():
    c, _ = _client([(200, {"Success": False, "ErrMsg": "no order matched"})])
    assert c.query_order() == []
    c, _ = _client([(200, {"Success": False,
                           "ErrMsg": "no pending order under this account",
                           "TotalPending": 0})])
    assert c.pending_count()["TotalPending"] == 0


def test_signed_call_without_keys_fails_fast():
    c = RoostooClient(transport=FakeTransport([]), limiter=NoLimit())
    with pytest.raises(RoostooError, match="no Roostoo API key"):
        c.balance()


def test_idempotent_calls_retry_on_5xx_and_network_errors():
    c, t = _client([(502, "bad gateway"), OSError("reset"),
                    (200, {"Success": True, "Wallet": {}})])
    assert c.balance() == {}
    assert len(t.calls) == 3


def test_order_placement_is_not_retried_after_a_timeout():
    c, t = _client([TimeoutError("read timed out"), (200, {"Success": True})])
    with pytest.raises(RoostooError) as exc:
        c.place_order("BTC/USD", "BUY", "1")
    assert exc.value.ambiguous and len(t.calls) == 1


def test_limit_order_requires_price():
    c, _ = _client([])
    with pytest.raises(ValueError):
        c.place_order("BTC/USD", "BUY", "1", order_type="LIMIT")


def test_short_close_sends_qty_over_pct():
    c, t = _client([(200, {"Success": True, "ClosedQty": 0.1})])
    c.short_close("BTC/USD", close_qty="0.1", close_pct="50")
    sent = dict(urllib.parse.parse_qsl(t.calls[0]["body"]))
    assert sent["close_qty"] == "0.1" and "close_pct" not in sent


# ── rate limiter ───────────────────────────────────────────────────────
def test_rate_limiter_blocks_the_call_over_the_limit():
    clock = {"t": 0.0}
    slept = []

    def sleep(s):
        slept.append(s)
        clock["t"] += s

    lim = RateLimiter(25, 60.0, clock=lambda: clock["t"], sleep=sleep)
    for _ in range(25):
        assert lim.acquire() == 0.0
    assert lim.calls_in_window() == 25
    waited = lim.acquire()                     # 26th call within the minute
    assert waited == pytest.approx(60.01, abs=0.05) and slept
    clock["t"] += 61
    assert lim.calls_in_window() == 0


# ── helpers ────────────────────────────────────────────────────────────
def test_num_reads_omitted_fields_as_zero():
    assert num({}, "OpenFee") == 0.0
    assert num({"x": "1.5"}, "x") == 1.5
    assert num({"x": None}, "x", 7) == 7


def test_fmt_decimal_never_uses_scientific_notation():
    assert fmt_decimal(0.00001, 8) == "0.00001"
    assert fmt_decimal(2.50000, 4) == "2.5"
    assert fmt_decimal(100, 2) == "100"
