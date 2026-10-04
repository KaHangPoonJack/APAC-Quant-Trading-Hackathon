"""Low-level Roostoo REST client: signing, timestamps, rate limit, retries.

Mirrors the official API doc (github.com/roostoo/Roostoo-API-Documents) 1:1 —
one method per endpoint, returning the decoded JSON payload. Higher layers
(execution/broker.py) translate those payloads into core models.

Protocol rules encoded here (each one is easy to get subtly wrong):
  * SIGNED endpoints (RCL_TopLevelCheck) send headers `RST-API-KEY` and
    `MSG-SIGNATURE` = HMAC-SHA256(secret, totalParams), where totalParams is the
    params sorted by key and joined `k=v&k=v`. GET sends them as the query
    string, POST as a form-urlencoded body.
  * Every signed/ts-checked request carries a 13-digit millisecond `timestamp`
    that must be within 60s of server time — we track the server clock offset.
  * Only the documented params may be sent: the server signs only those, so an
    extra param makes the signature mismatch.
  * Failures still come back as HTTP 200 with `Success: false` + `ErrMsg`, so
    the flag is ALWAYS checked. Two "failures" are really empty results
    ("no pending order", "no order matched") and are returned as empty.
  * Zero-valued fields are omitted from responses — read missing as 0 (`num`).
  * The competition allows 30 calls/min across ALL endpoints (FAQ Q22) — a
    single shared limiter gates every request, retries included.
  * Order-creating POSTs are NOT retried after the request may have reached the
    server (a timeout there could mean the order exists); everything else is.
"""
from __future__ import annotations

import collections
import hashlib
import hmac
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

BASE_URL = "https://mock-api.roostoo.com"

_BENIGN_EMPTY = ("no pending order", "no order matched")

Transport = Callable[[str, str, Dict[str, str], Optional[bytes], float], Tuple[int, str]]


class RoostooError(RuntimeError):
    """Roostoo returned Success=false, an HTTP error, or an unusable payload."""

    def __init__(self, message: str, *, ambiguous: bool = False):
        super().__init__(message)
        # True when an order-creating request MAY have been executed even though
        # we got no answer (timeout after send). Callers must reconcile via
        # query_order before re-submitting.
        self.ambiguous = ambiguous


def num(d: dict, key: str, default: float = 0.0) -> float:
    """Numeric field that Roostoo omits when zero."""
    try:
        v = d.get(key)
        return float(v) if v is not None and v != "" else default
    except (TypeError, ValueError):
        return default


def total_params(params: Dict[str, object]) -> str:
    """Key-sorted `k=v&k=v` — the exact string that gets signed."""
    return "&".join(f"{k}={params[k]}" for k in sorted(params))


def sign(params: Dict[str, object], secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), total_params(params).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def fmt_decimal(x: float, places: int = 8) -> str:
    """Plain decimal string (never scientific notation), trailing zeros cut."""
    s = f"{float(x):.{max(0, int(places))}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


class RateLimiter:
    """Sliding-window limiter: at most `max_calls` per `period` seconds, shared
    by every thread. `acquire()` blocks until a slot is free. Clock and sleep
    are injectable so tests run instantly."""

    def __init__(self, max_calls: int, period: float = 60.0,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        if max_calls <= 0:
            raise ValueError("max_calls must be positive")
        self.max_calls = int(max_calls)
        self.period = float(period)
        self._clock = clock
        self._sleep = sleep
        self._calls: collections.deque = collections.deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        while self._calls and now - self._calls[0] >= self.period:
            self._calls.popleft()

    def wait_time(self) -> float:
        """Seconds until the next call would be allowed (0 = now)."""
        with self._lock:
            now = self._clock()
            self._prune(now)
            if len(self._calls) < self.max_calls:
                return 0.0
            return max(0.0, self.period - (now - self._calls[0]))

    def acquire(self) -> float:
        """Take a slot, sleeping as needed. Returns seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                self._prune(now)
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return waited
                delay = self.period - (now - self._calls[0]) + 0.01
            if delay > 1.0:
                log.info("Roostoo rate limit: waiting %.1fs for a call slot", delay)
            self._sleep(delay)
            waited += delay

    def calls_in_window(self) -> int:
        with self._lock:
            self._prune(self._clock())
            return len(self._calls)


def _urllib_transport(method: str, url: str, headers: Dict[str, str],
                      body: Optional[bytes], timeout: float) -> Tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


class RoostooClient:
    def __init__(self, api_key: str = "", secret_key: str = "",
                 base_url: str = BASE_URL, max_calls_per_minute: int = 25,
                 timeout: float = 10.0, max_retries: int = 3,
                 limiter: Optional[RateLimiter] = None,
                 transport: Optional[Transport] = None,
                 clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
                 sleep: Callable[[float], None] = time.sleep):
        self._key = api_key
        self._secret = secret_key
        self._base = base_url.rstrip("/")
        self.limiter = limiter or RateLimiter(max_calls_per_minute)
        self._timeout = timeout
        self._retries = max(1, int(max_retries))
        self._transport = transport or _urllib_transport
        self._clock_ms = clock_ms
        self._sleep = sleep
        self.offset_ms = 0          # server_time - local_time
        self.calls_total = 0

    @property
    def has_keys(self) -> bool:
        return bool(self._key and self._secret)

    # -- plumbing ----------------------------------------------------------
    def timestamp(self) -> str:
        return str(int(self._clock_ms() + self.offset_ms))

    def sync_time(self) -> int:
        """Measure the server clock offset (signed requests are rejected beyond
        ±60s). Returns the offset in ms."""
        before = self._clock_ms()
        server = int(self.server_time())
        after = self._clock_ms()
        self.offset_ms = server - (before + after) // 2
        if abs(self.offset_ms) > 5_000:
            log.warning("local clock is %+.1fs off Roostoo server time — correcting",
                        self.offset_ms / 1000)
        return self.offset_ms

    def _request(self, method: str, path: str, params: Optional[Dict[str, object]] = None,
                 *, signed: bool = False, timestamped: bool = False,
                 idempotent: bool = True) -> dict:
        if signed and not self.has_keys:
            raise RoostooError(f"{path}: no Roostoo API key/secret configured "
                               "(settings.local.yaml roostoo.keys.<ENV> or env vars)")
        last_err: Optional[str] = None
        for attempt in range(self._retries):
            p = {k: v for k, v in (params or {}).items() if v is not None}
            if signed or timestamped:
                p["timestamp"] = self.timestamp()     # fresh per attempt
            raw = total_params(p)
            headers: Dict[str, str] = {}
            if signed:
                headers["RST-API-KEY"] = self._key
                headers["MSG-SIGNATURE"] = sign(p, self._secret)
            url = f"{self._base}{path}"
            body: Optional[bytes] = None
            if method == "GET":
                if p:
                    url += "?" + urllib.parse.urlencode(sorted(p.items()))
            else:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                body = raw.encode("utf-8")

            self.limiter.acquire()
            self.calls_total += 1
            try:
                status, text = self._transport(method, url, headers, body, self._timeout)
            except Exception as exc:  # noqa: BLE001 — network failure
                if not idempotent:
                    # We cannot know whether the order reached the server.
                    raise RoostooError(f"{path}: no response ({exc}) — order state "
                                       "unknown, reconcile before retrying",
                                       ambiguous=True) from exc
                last_err = f"network error: {exc}"
                self._backoff(attempt, path, last_err)
                continue

            if status == 429 or status >= 500:
                last_err = f"HTTP {status}: {text[:200]}"
                if not idempotent and status >= 500:
                    raise RoostooError(f"{path}: {last_err} — order state unknown",
                                       ambiguous=True)
                self._backoff(attempt, path, last_err)
                continue
            if status >= 400:
                raise RoostooError(f"{path}: HTTP {status}: {text[:300]}")
            try:
                data = json.loads(text)
            except ValueError as exc:
                raise RoostooError(f"{path}: non-JSON response: {text[:200]}") from exc
            if isinstance(data, dict) and data.get("Success") is False:
                msg = str(data.get("ErrMsg", "") or "")
                if any(b in msg.lower() for b in _BENIGN_EMPTY):
                    data["_empty"] = True
                    return data
                raise RoostooError(f"{path}: {msg or 'Success=false'}")
            return data
        raise RoostooError(f"{path}: failed after {self._retries} attempts ({last_err})")

    def _backoff(self, attempt: int, path: str, err: str) -> None:
        if attempt < self._retries - 1:
            delay = min(30.0, 2.0 ** attempt)
            log.warning("Roostoo %s failed (%s) — retry %d in %.0fs",
                        path, err, attempt + 1, delay)
            self._sleep(delay)

    # -- public endpoints ----------------------------------------------------
    def server_time(self) -> int:
        return int(self._request("GET", "/v3/serverTime").get("ServerTime", 0))

    def exchange_info(self) -> dict:
        return self._request("GET", "/v3/exchangeInfo")

    def ticker(self, pair: Optional[str] = None) -> Dict[str, dict]:
        """{pair: {MaxBid, MinAsk, LastPrice, Change, ...}} — omit `pair` to get
        every listed pair in ONE call (preferred under the rate limit)."""
        data = self._request("GET", "/v3/ticker", {"pair": pair}, timestamped=True)
        return data.get("Data") or {}

    # -- signed: account -------------------------------------------------------
    def balance(self) -> Dict[str, dict]:
        """{asset: {Free, Lock}}."""
        data = self._request("GET", "/v3/balance", signed=True)
        return data.get("Wallet") or data.get("SpotWallet") or {}

    def pending_count(self) -> dict:
        return self._request("GET", "/v3/pending_count", signed=True)

    # -- signed: spot orders -----------------------------------------------------
    def place_order(self, pair: str, side: str, quantity: str, order_type: str = "MARKET",
                    price: Optional[str] = None) -> dict:
        """Returns OrderDetail. `quantity`/`price` are pre-formatted strings."""
        order_type = order_type.upper()
        if order_type == "LIMIT" and price is None:
            raise ValueError("LIMIT orders require a price")
        params = {"pair": pair, "side": side.upper(), "type": order_type,
                  "quantity": quantity}
        if order_type == "LIMIT":
            params["price"] = price
        data = self._request("POST", "/v3/place_order", params, signed=True,
                             idempotent=False)
        return data.get("OrderDetail") or {}

    def query_order(self, order_id: Optional[str] = None, pair: Optional[str] = None,
                    pending_only: Optional[bool] = None, offset: Optional[int] = None,
                    limit: Optional[int] = None) -> List[dict]:
        """Order history (newest first per Roostoo). `order_id` excludes every
        other filter (API rule)."""
        if order_id is not None:
            params: Dict[str, object] = {"order_id": str(order_id)}
        else:
            params = {"pair": pair,
                      "pending_only": None if pending_only is None
                      else ("TRUE" if pending_only else "FALSE"),
                      "offset": None if offset is None else str(int(offset)),
                      "limit": None if limit is None else str(int(limit))}
        data = self._request("POST", "/v3/query_order", params, signed=True)
        if data.get("_empty"):
            return []
        return list(data.get("OrderMatched") or [])

    def cancel_order(self, order_id: Optional[str] = None,
                     pair: Optional[str] = None) -> List[int]:
        """Cancel one order, all on a pair, or (neither given) ALL pending."""
        if order_id is not None and pair is not None:
            raise ValueError("send order_id OR pair, not both")
        params = {"order_id": None if order_id is None else str(order_id), "pair": pair}
        data = self._request("POST", "/v3/cancel_order", params, signed=True)
        return list(data.get("CanceledList") or [])

    # -- signed: shorts (/v6) -----------------------------------------------------
    def short_open(self, pair: str, collateral: str, price: Optional[str] = None) -> dict:
        """Sized by USD collateral (qty = collateral / entry). `price` -> LIMIT."""
        params = {"pair": pair, "collateral": collateral}
        if price is not None:
            params["order_type"] = "LIMIT"
            params["price"] = price
        return self._request("POST", "/v6/short_open", params, signed=True,
                             idempotent=False)

    def short_close(self, pair: str, close_qty: Optional[str] = None,
                    close_pct: Optional[str] = None) -> dict:
        """Reduce-only close at the best ask. Neither size given = close all."""
        params = {"pair": pair, "close_qty": close_qty,
                  "close_pct": None if close_qty is not None else close_pct}
        return self._request("POST", "/v6/short_close", params, signed=True,
                             idempotent=False)

    def short_positions(self) -> List[dict]:
        data = self._request("GET", "/v6/short_positions", signed=True)
        return list(data.get("Positions") or [])
