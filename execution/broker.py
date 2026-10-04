"""RoostooBroker — the ONLY translation point between core models and Roostoo.

Exposes the duck-typed surface the rest of the system already uses
(recorder / recovery / order manager / engine):

    acc_id(market)           stable account identity for the DB
    account(market)          AccountSnapshot (USD equity, cash, positions)
    positions(market)        {pair: Position}  — NET per pair
    position_legs(market)    [Position]        — spot LONG and /v6 SHORT separately
    place(OrderRequest)      -> OrderState
    cancel(order_id)
    open_orders(market)      -> [OrderState]
    deals(market)            -> [fill dict]  (derived from query_order history)

`market` is accepted everywhere for interface compatibility but Roostoo is a
single venue (core.models.MARKET).

Valuation (USD):
    equity = wallet USD (Free+Lock) + Σ spot coins × mid + Σ short PositionValue
A short's collateral leaves the wallet when the short opens and comes back on
close (`ReturnAmount`), and PositionValue = collateral + unrealized PnL — so the
three terms don't double count. Verify on the TEST account with
`scripts/check_connection.py`, which prints each term.

Call budget: balance and short positions are cached for `cache_seconds` and
invalidated after every order, so one poll that asks for account(), positions()
and position_legs() costs one balance call (+ one short_positions call when
shorts are enabled), not three.
"""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from core.enums import OrderStatus, OrderType, Side
from core.models import MARKET, AccountSnapshot, OrderRequest, OrderState, Position
from execution.roostoo_client import RoostooClient, RoostooError, fmt_decimal, num

log = logging.getLogger(__name__)

UNIT = "USD"


class BrokerError(RuntimeError):
    """An order/query was rejected or could not be completed."""


def map_status(status: str) -> OrderStatus:
    s = (status or "").upper()
    if s == "FILLED":
        return OrderStatus.FILLED
    if "PARTIAL" in s:
        return OrderStatus.PARTIAL
    if s in ("CANCELED", "CANCELLED"):
        return OrderStatus.CANCELLED
    if s in ("REJECTED", "FAILED"):
        return OrderStatus.REJECTED
    if s in ("PENDING", "OPEN", "NEW"):
        return OrderStatus.SUBMITTED
    return OrderStatus.SUBMITTED


class RoostooBroker:
    def __init__(self, client: RoostooClient, market_data, env: str = "TEST",
                 include_shorts: bool = True, dust_usd: float = 1.0,
                 cache_seconds: float = 5.0,
                 clock: Callable[[], float] = time.monotonic):
        self._c = client
        self._md = market_data              # tickers() / rule(pair)
        self._env = env.upper()
        self._include_shorts = include_shorts
        self._dust = dust_usd
        self._cache_s = cache_seconds
        self._clock = clock
        self._wallet: Optional[Dict[str, dict]] = None
        self._wallet_at = 0.0
        self._shorts: Optional[List[dict]] = None
        self._shorts_at = 0.0

    # -- identity -------------------------------------------------------------
    def acc_id(self, market: str = MARKET) -> str:
        """Stable per (env, api key) without storing the key itself."""
        key = getattr(self._c, "_key", "") or ""
        fp = hashlib.sha256(key.encode("utf-8")).hexdigest()[:10] if key else "nokey"
        return f"roostoo-{self._env.lower()}-{fp}"

    # -- cached reads -----------------------------------------------------------
    def invalidate(self) -> None:
        self._wallet = None
        self._shorts = None

    def wallet(self) -> Dict[str, dict]:
        now = self._clock()
        if self._wallet is None or now - self._wallet_at >= self._cache_s:
            self._wallet = self._c.balance()
            self._wallet_at = now
        return self._wallet

    def short_rows(self) -> List[dict]:
        if not self._include_shorts:
            return []
        now = self._clock()
        if self._shorts is None or now - self._shorts_at >= self._cache_s:
            self._shorts = self._c.short_positions()
            self._shorts_at = now
        return self._shorts

    # -- positions ----------------------------------------------------------------
    def position_legs(self, market: str = MARKET) -> List[Position]:
        tickers = self._md.tickers()
        legs: List[Position] = []
        for asset, bal in self.wallet().items():
            if asset.upper() == UNIT:
                continue
            qty = num(bal, "Free") + num(bal, "Lock")
            if qty <= 0:                       # FAQ Q24: tiny negatives are rounding
                continue
            pair = f"{asset.upper()}/{UNIT}"
            t = tickers.get(pair)
            if t is None or t.mid <= 0:
                log.debug("no ticker for held asset %s — not valued", asset)
                continue
            value = qty * t.mid
            if value < self._dust:
                continue
            legs.append(Position(code=pair, qty=qty, avg_price=0.0,
                                 market_value=value, currency=UNIT))
        for r in self.short_rows():
            qty = num(r, "ShortQty")
            if qty <= 0:
                continue
            price = num(r, "CurrentPrice")
            legs.append(Position(
                code=str(r.get("Pair", "")), qty=-qty,
                avg_price=num(r, "EntryPrice"),
                market_value=-qty * price,
                unrealized_pl=num(r, "UnrealizedPNL"), currency=UNIT,
            ))
        return legs

    def positions(self, market: str = MARKET) -> Dict[str, Position]:
        """Net position per pair (spot long + short legs combined)."""
        out: Dict[str, Position] = {}
        for leg in self.position_legs(market):
            prev = out.get(leg.code)
            if prev is None:
                out[leg.code] = leg
            else:
                out[leg.code] = Position(
                    code=leg.code, qty=prev.qty + leg.qty,
                    avg_price=prev.avg_price or leg.avg_price,
                    market_value=prev.market_value + leg.market_value,
                    unrealized_pl=prev.unrealized_pl + leg.unrealized_pl,
                    currency=UNIT,
                )
        return out

    def account(self, market: str = MARKET) -> AccountSnapshot:
        wallet = self.wallet()
        usd = wallet.get(UNIT, {})
        free, lock = num(usd, "Free"), num(usd, "Lock")
        legs = self.position_legs(market)
        longs = sum(p.market_value for p in legs if p.qty > 0)
        short_value = sum(num(r, "PositionValue") for r in self.short_rows())
        equity = free + lock + longs + short_value
        return AccountSnapshot(
            market=MARKET, cash=free, total_assets=equity, buying_power=free,
            currency=UNIT, positions=self.positions(market),
        )

    # -- orders --------------------------------------------------------------------
    def place(self, req: OrderRequest) -> OrderState:
        rule = self._md.rule(req.code)
        if rule is None:
            raise BrokerError(f"{req.code} is not listed on Roostoo")
        if not rule.can_trade:
            raise BrokerError(f"{req.code} is not tradable right now")
        qty = rule.round_qty(req.qty)
        if qty <= 0:
            raise BrokerError(f"{req.code}: qty {req.qty} rounds to 0 "
                              f"(AmountPrecision={rule.amount_precision})")
        qty_s = fmt_decimal(qty, rule.amount_precision)
        price_s = (fmt_decimal(rule.round_price(req.price), rule.price_precision)
                   if req.order_type == OrderType.LIMIT else None)
        try:
            if req.side in (Side.BUY, Side.SELL):
                d = self._c.place_order(req.code, req.side.value, qty_s,
                                        order_type=req.order_type.value, price=price_s)
                state = OrderState(
                    order_id=str(d.get("OrderID", "")), code=req.code, side=req.side,
                    qty=qty, price=num(d, "FilledAverPrice") or num(d, "Price") or req.price,
                    status=map_status(str(d.get("Status", ""))),
                    filled_qty=num(d, "FilledQuantity"),
                )
            elif req.side == Side.SHORT_OPEN:
                # Shorts are sized by USD collateral; qty = collateral / entry.
                collateral = fmt_decimal(qty * req.price, 2)
                d = self._c.short_open(req.code, collateral, price=price_s)
                filled = d.get("Status") == "OPEN"
                state = OrderState(
                    order_id=str(d.get("ID", "")), code=req.code, side=req.side,
                    qty=num(d, "ShortQty") or qty,
                    price=num(d, "EntryPrice") or req.price,
                    status=OrderStatus.FILLED if filled else OrderStatus.SUBMITTED,
                    filled_qty=num(d, "ShortQty") if filled else 0.0,
                )
            elif req.side == Side.SHORT_CLOSE:
                d = self._c.short_close(req.code, close_qty=qty_s)
                # The close response carries no order id; the fill is picked up
                # from query_order history (Side=SHORT_CLOSE) by sync_fills.
                state = OrderState(
                    order_id="", code=req.code, side=req.side,
                    qty=num(d, "ClosedQty") or qty,
                    price=num(d, "ClosePrice") or req.price,
                    status=OrderStatus.FILLED, filled_qty=num(d, "ClosedQty"),
                )
            else:  # pragma: no cover — exhaustive over Side
                raise BrokerError(f"unsupported side {req.side}")
        except RoostooError as exc:
            raise BrokerError(f"{req.side.value} {qty_s} {req.code} rejected: {exc}") from exc
        finally:
            self.invalidate()
        log.info("ORDER %s %s %s @ %s (%s) -> id=%s status=%s", req.side.value, qty_s,
                 req.code, price_s or "MKT", req.reason, state.order_id, state.status.value)
        return state

    def cancel(self, order_id: str) -> None:
        try:
            self._c.cancel_order(order_id=str(order_id))
        except RoostooError as exc:
            raise BrokerError(f"cancel {order_id} failed: {exc}") from exc
        finally:
            self.invalidate()

    def cancel_all(self, pair: Optional[str] = None) -> List[int]:
        try:
            return self._c.cancel_order(pair=pair)
        except RoostooError as exc:
            raise BrokerError(f"cancel_all failed: {exc}") from exc
        finally:
            self.invalidate()

    def open_orders(self, market: str = MARKET) -> List[OrderState]:
        rows = self._c.query_order(pending_only=True)
        out: List[OrderState] = []
        for r in rows:
            if str(r.get("Status", "")).upper() != "PENDING":
                continue
            try:
                side = Side(str(r.get("Side", "")).upper())
            except ValueError:
                continue
            created = num(r, "CreateTimestamp")
            out.append(OrderState(
                order_id=str(r.get("OrderID", "")), code=str(r.get("Pair", "")),
                side=side, qty=num(r, "Quantity"), price=num(r, "Price"),
                status=OrderStatus.SUBMITTED, filled_qty=num(r, "FilledQuantity"),
                updated_at=(datetime.fromtimestamp(created / 1000, tz=timezone.utc)
                            if created else None),
            ))
        return out

    def deals(self, market: str = MARKET, limit: int = 100) -> List[dict]:
        """Fills, derived from finished orders in query_order history.

        Roostoo has no deal feed; an order's fill is final once it is FILLED (or
        CANCELED with a partial fill), so it is keyed by OrderID. PENDING orders
        are skipped until they finish — their filled qty can still change.
        """
        out: List[dict] = []
        for r in self._c.query_order(limit=limit):
            status = str(r.get("Status", "")).upper()
            if status == "PENDING":
                continue
            qty = num(r, "FilledQuantity")
            price = num(r, "FilledAverPrice")
            side = str(r.get("Side", "")).upper()
            if side in ("SHORT_OPEN", "SHORT_CLOSE"):
                qty = qty or num(r, "Quantity")
                price = price or num(r, "Price")
            if qty <= 0 or price <= 0:
                continue
            commission = num(r, "CommissionChargeValue")
            if str(r.get("CommissionCoin", UNIT) or UNIT).upper() != UNIT:
                commission *= price            # charged in coin -> USD
            ts = num(r, "FinishTimestamp") or num(r, "CreateTimestamp")
            out.append({
                "deal_id": f"roostoo-{r.get('OrderID')}",
                "order_id": str(r.get("OrderID", "")),
                "code": str(r.get("Pair", "")),
                "side": side,
                "order_type": str(r.get("Type", "MARKET") or "MARKET").upper(),
                "qty": qty,
                "price": price,
                "commission": commission,
                "currency": UNIT,
                "time": int(ts) if ts else None,
            })
        return out
