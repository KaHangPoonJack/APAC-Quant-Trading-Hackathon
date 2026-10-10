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

`scale_entries` runs after the exits have filled: if the adding orders need more
free USD than the account has, every one of them is shrunk by the same factor,
so a cash shortfall is shared by longs and shorts instead of falling on
whichever pairs sort last.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, Iterable, List, Sequence, Tuple

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


def scale_entries(orders: Sequence[OrderRequest], available_usd: float,
                  rules: Dict[str, PairRule], sizer: PositionSizer,
                  fee_buffer: float = 0.0) -> Tuple[List[OrderRequest], List[str]]:
    """Fit exposure-adding orders into `available_usd` by one common factor.

    Returns (orders, notes). Orders are returned unchanged when they already fit.
    Otherwise each qty is multiplied by k = available / (Σ notional × (1 +
    fee_buffer)) and rounded DOWN to the pair's step, so the total never
    exceeds what is free; an order that drops below the minimum is skipped.
    """
    need = sum(o.notional for o in orders) * (1.0 + fee_buffer)
    if need <= 0 or need <= available_usd:
        return list(orders), []
    k = max(0.0, available_usd) / need
    notes = [f"entries need ${need:,.2f} but ${max(0.0, available_usd):,.2f} is free "
             f"— every entry scaled by {k:.3f}"]
    out: List[OrderRequest] = []
    for o in orders:
        rule = rules.get(o.code)
        if rule is None:
            notes.append(f"{o.code}: no pair rule — entry skipped")
            continue
        qty = rule.round_qty(o.qty * k)
        if not sizer.is_tradeable(qty, o.price, rule):
            notes.append(f"{o.code}: {o.side.value} scaled to {qty} (${qty * o.price:,.2f}) "
                         "below minimum — skipped")
            continue
        out.append(replace(o, qty=qty))
    return out, notes
