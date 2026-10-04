"""Target weights -> the delta orders that get the account there. Pure.

Per pair, with mid price m, equity E and target weight w:
    target_qty = w * E / m        (signed: <0 means a /v6 short)

Legs are handled separately because Roostoo keeps spot holdings and short
positions apart (both can exist on one pair):
  * w >= 0 : close any short leg, then BUY/SELL spot to reach target_qty.
  * w <  0 : sell any spot leg, then SHORT_OPEN/SHORT_CLOSE to reach |target_qty|.

Orders that reduce exposure (SELL, SHORT_CLOSE) are returned BEFORE orders that
add it (BUY, SHORT_OPEN), so cash freed by exits funds the entries.

Deltas below `min_order_usd` (or the pair's MiniOrder) are skipped to avoid
churning fees — except going fully flat, which only has to clear MiniOrder.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List

from core.enums import OrderType, Side
from core.models import OrderRequest, PairRule, Position, Ticker
from risk.position_sizer import PositionSizer


@dataclass
class RebalancePlan:
    orders: List[OrderRequest] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def _order(code: str, side: Side, qty: float, price: float, order_type: OrderType,
           reason: str) -> OrderRequest:
    return OrderRequest(code=code, side=side, qty=qty, price=price,
                        order_type=order_type, reason=reason)


def plan_rebalance(targets: Dict[str, float], equity: float,
                   legs: Iterable[Position], tickers: Dict[str, Ticker],
                   rules: Dict[str, PairRule], sizer: PositionSizer,
                   order_type: OrderType = OrderType.MARKET) -> RebalancePlan:
    plan = RebalancePlan()
    if equity <= 0:
        plan.notes.append(f"equity {equity} <= 0 — nothing planned")
        return plan

    long_qty: Dict[str, float] = {}
    short_qty: Dict[str, float] = {}
    for leg in legs:
        if leg.qty > 0:
            long_qty[leg.code] = long_qty.get(leg.code, 0.0) + leg.qty
        elif leg.qty < 0:
            short_qty[leg.code] = short_qty.get(leg.code, 0.0) - leg.qty

    reducing: List[OrderRequest] = []
    adding: List[OrderRequest] = []

    def emit(code: str, side: Side, qty: float, price: float, rule: PairRule,
             reason: str, full_exit: bool = False) -> None:
        qty = rule.round_qty(qty)
        value = qty * price
        if qty <= 0:
            return
        if full_exit:
            ok = not rule.min_notional or value > rule.min_notional
        else:
            ok = sizer.is_tradeable(qty, price, rule)
        if not ok:
            plan.notes.append(f"{code}: {side.value} {qty} (${value:,.2f}) below "
                              "minimum — skipped")
            return
        o = _order(code, side, qty, price, order_type, reason)
        (adding if side.adds_exposure else reducing).append(o)

    for code, w in sorted(targets.items()):
        rule, t = rules.get(code), tickers.get(code)
        if rule is None or t is None or t.mid <= 0:
            plan.notes.append(f"{code}: no pair rule / ticker — skipped")
            continue
        bid = t.bid if t.bid > 0 else t.mid
        ask = t.ask if t.ask > 0 else t.mid
        tgt = w * equity / t.mid
        held_long = long_qty.get(code, 0.0)
        held_short = short_qty.get(code, 0.0)

        if w >= 0:
            if held_short > 0:
                emit(code, Side.SHORT_CLOSE, held_short, ask, rule,
                     f"target {w:+.3f}: close short", full_exit=True)
            delta = tgt - held_long
            if delta > 0:
                emit(code, Side.BUY, delta, ask, rule, f"target {w:+.3f}")
            elif delta < 0:
                flat = w == 0
                emit(code, Side.SELL, held_long if flat else -delta, bid, rule,
                     f"target {w:+.3f}", full_exit=flat)
        else:
            if held_long > 0:
                emit(code, Side.SELL, held_long, bid, rule,
                     f"target {w:+.3f}: exit long before shorting", full_exit=True)
            delta = -tgt - held_short
            if delta > 0:
                emit(code, Side.SHORT_OPEN, delta, bid, rule, f"target {w:+.3f}")
            elif delta < 0:
                emit(code, Side.SHORT_CLOSE, -delta, ask, rule, f"target {w:+.3f}")

    plan.orders = reducing + adding
    return plan
