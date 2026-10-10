"""Pure rebalance planner: target weights -> ordered delta orders."""
import pytest

from core.enums import Side
from core.models import OrderRequest, PairRule, Position, Ticker
from engine.rebalance import plan_rebalance, scale_entries
from risk.position_sizer import PositionSizer

RULES = {"BTC/USD": PairRule("BTC/USD", 2, 5, 1.0),
         "ETH/USD": PairRule("ETH/USD", 2, 4, 1.0)}
TICKERS = {"BTC/USD": Ticker("BTC/USD", 59990, 60010, 60000),
           "ETH/USD": Ticker("ETH/USD", 2999, 3001, 3000)}
SIZER = PositionSizer(min_order_usd=10)


def plan(targets, legs=(), equity=10_000.0):
    return plan_rebalance(targets, equity, list(legs), TICKERS, RULES, SIZER)


def _sig(p):
    return [(o.side, o.code, o.qty) for o in p.orders]


def test_buy_from_flat_uses_mid_for_size_and_ask_for_price():
    p = plan({"BTC/USD": 0.3})
    assert _sig(p) == [(Side.BUY, "BTC/USD", pytest.approx(0.05))]
    assert p.orders[0].price == 60010


def test_already_at_target_does_nothing():
    p = plan({"BTC/USD": 0.3}, [Position("BTC/USD", 0.05, 0, 3000)])
    assert p.orders == []


def test_small_delta_below_min_order_is_skipped():
    p = plan({"BTC/USD": 0.3}, [Position("BTC/USD", 0.0499, 0, 2994)])
    assert p.orders == [] and any("below minimum" in n for n in p.notes)


def test_sells_come_before_buys():
    legs = [Position("ETH/USD", 2.0, 0, 6000)]
    p = plan({"BTC/USD": 0.5, "ETH/USD": 0.0}, legs)
    assert [o.side for o in p.orders] == [Side.SELL, Side.BUY]
    assert p.orders[0].qty == 2.0 and p.orders[0].price == 2999    # sell at bid


def test_going_flat_sells_entire_holding_even_if_tiny():
    p = plan({"BTC/USD": 0.0}, [Position("BTC/USD", 0.0001, 0, 6)])   # $6 < $10 min
    assert _sig(p) == [(Side.SELL, "BTC/USD", 0.0001)]


def test_partial_trim():
    p = plan({"BTC/USD": 0.1}, [Position("BTC/USD", 0.05, 0, 3000)])
    assert _sig(p) == [(Side.SELL, "BTC/USD", pytest.approx(0.03333, abs=1e-5))]


def test_long_to_short_flip_sells_then_shorts():
    p = plan({"BTC/USD": -0.2}, [Position("BTC/USD", 0.05, 0, 3000)])
    assert [o.side for o in p.orders] == [Side.SELL, Side.SHORT_OPEN]
    assert p.orders[1].qty == pytest.approx(0.03333, abs=1e-5)


def test_short_to_long_flip_closes_short_first():
    p = plan({"ETH/USD": 0.3}, [Position("ETH/USD", -1.0, 3100, -3001)])
    assert _sig(p) == [(Side.SHORT_CLOSE, "ETH/USD", 1.0),
                       (Side.BUY, "ETH/USD", pytest.approx(1.0))]


def test_short_resize():
    legs = [Position("ETH/USD", -1.0, 3100, -3001)]
    assert _sig(plan({"ETH/USD": -0.6}, legs)) == [(Side.SHORT_OPEN, "ETH/USD", 1.0)]
    assert _sig(plan({"ETH/USD": -0.15}, legs)) == [(Side.SHORT_CLOSE, "ETH/USD", 0.5)]


def test_missing_ticker_or_rule_is_skipped_with_note():
    p = plan({"SOL/USD": 0.2})
    assert p.orders == [] and "SOL/USD" in p.notes[0]


def test_zero_equity_plans_nothing():
    assert plan({"BTC/USD": 0.5}, equity=0).orders == []


# -- scale_entries: share a cash shortfall across every entry ------------------
ENTRIES = [OrderRequest("BTC/USD", Side.BUY, 0.05, 60010),          # ~$3,000
           OrderRequest("ETH/USD", Side.SHORT_OPEN, 1.0, 2999)]     # ~$3,000


def test_entries_that_fit_are_unchanged():
    out, notes = scale_entries(ENTRIES, 10_000, RULES, SIZER, fee_buffer=0.002)
    assert out == ENTRIES and notes == []


def test_shortfall_scales_every_entry_by_the_same_factor():
    out, notes = scale_entries(ENTRIES, 3_000, RULES, SIZER, fee_buffer=0.002)
    k = 3_000 / (sum(o.notional for o in ENTRIES) * 1.002)
    assert [o.code for o in out] == ["BTC/USD", "ETH/USD"]          # nobody skipped
    for o, orig in zip(out, ENTRIES):
        assert o.qty == pytest.approx(orig.qty * k, abs=1e-4)
        assert o.qty <= orig.qty * k                                # rounded DOWN
        assert (o.side, o.price) == (orig.side, orig.price)
    assert sum(o.notional for o in out) * 1.002 <= 3_000
    assert "scaled by" in notes[0]


def test_entry_scaled_below_minimum_is_dropped():
    small = ENTRIES + [OrderRequest("ETH/USD", Side.BUY, 0.01, 3001)]   # ~$30
    out, notes = scale_entries(small, 1_000, RULES, SIZER, fee_buffer=0.002)
    assert len(out) == 2 and any("below minimum" in n for n in notes)


def test_no_free_cash_drops_all_entries():
    out, _ = scale_entries(ENTRIES, -5.0, RULES, SIZER)
    assert out == []
