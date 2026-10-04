import pytest

from core.config import RiskConfig
from core.enums import OrderType, Side
from core.models import AccountSnapshot, OrderRequest, PairRule
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager

BTC = PairRule("BTC/USD", price_precision=2, amount_precision=5, min_notional=1.0)


# ── PairRule rounding ─────────────────────────────────────────────────
def test_round_qty_rounds_down_to_amount_step():
    assert BTC.round_qty(0.1234567) == pytest.approx(0.12345)
    assert BTC.round_qty(0.000009) == 0.0
    # float noise at an exact step must not lose a whole step
    assert BTC.round_qty(0.3) == pytest.approx(0.3)


def test_round_price_uses_price_precision():
    assert BTC.round_price(60000.129) == pytest.approx(60000.13)


# ── PositionSizer ─────────────────────────────────────────────────────
def test_sizer_never_exceeds_notional():
    s = PositionSizer(min_order_usd=10)
    qty = s.qty_for_notional(1000, 60000, BTC)
    assert qty == pytest.approx(0.01666)
    assert qty * 60000 <= 1000


def test_sizer_zero_on_bad_inputs():
    s = PositionSizer()
    assert s.qty_for_notional(0, 60000, BTC) == 0
    assert s.qty_for_notional(1000, 0, BTC) == 0


def test_tradeable_respects_miniorder_and_min_order_usd():
    s = PositionSizer(min_order_usd=10)
    assert not s.is_tradeable(0.00001, 60000, BTC)       # $0.60 < MiniOrder $1
    assert not s.is_tradeable(0.0001, 60000, BTC)        # $6 < min_order_usd $10
    assert s.is_tradeable(0.001, 60000, BTC)             # $60


# ── clamp_weights ─────────────────────────────────────────────────────
def _rm(**kw):
    base = dict(max_gross_exposure=1.0, max_net_exposure=1.0, max_weight_per_pair=0.5,
                min_order_usd=10, allow_short=False, cash_buffer_pct=0.0)
    base.update(kw)
    return RiskManager(RiskConfig(**base))


def test_shorts_zeroed_when_disabled():
    w, notes = _rm().clamp_weights({"BTC/USD": -0.3, "ETH/USD": 0.2})
    assert w == {"BTC/USD": 0.0, "ETH/USD": 0.2}
    assert any("allow_short" in n for n in notes)


def test_per_pair_cap():
    w, _ = _rm(max_weight_per_pair=0.4).clamp_weights({"BTC/USD": 0.9})
    assert w["BTC/USD"] == pytest.approx(0.4)


def test_gross_cap_scales_proportionally():
    w, _ = _rm(max_weight_per_pair=1.0, max_gross_exposure=1.0).clamp_weights(
        {"BTC/USD": 0.9, "ETH/USD": 0.6})
    assert sum(w.values()) == pytest.approx(1.0)
    assert w["BTC/USD"] / w["ETH/USD"] == pytest.approx(1.5)   # ratio preserved


def test_cash_buffer_reduces_gross():
    w, _ = _rm(max_weight_per_pair=1.0, cash_buffer_pct=0.02).clamp_weights(
        {"BTC/USD": 1.0})
    assert w["BTC/USD"] == pytest.approx(0.98)


def test_net_cap_with_shorts():
    w, _ = _rm(allow_short=True, max_weight_per_pair=1.0, max_gross_exposure=2.0,
               max_net_exposure=0.5).clamp_weights({"BTC/USD": 0.8, "ETH/USD": 0.2})
    assert sum(w.values()) == pytest.approx(0.5)


def test_untradable_and_nan_dropped():
    w, notes = _rm().clamp_weights({"BTC/USD": 0.2, "FOO/USD": 0.2,
                                    "ETH/USD": float("nan")}, tradable=["BTC/USD", "ETH/USD"])
    assert w == {"BTC/USD": 0.2}
    assert len(notes) == 2


# ── order check ───────────────────────────────────────────────────────
ACCOUNT = AccountSnapshot("ROOSTOO", cash=1000, total_assets=5000, buying_power=1000)


def _req(side, qty=0.01, price=60000):
    return OrderRequest("BTC/USD", side, qty, price, OrderType.MARKET)


def test_exposure_reducing_orders_always_approved():
    rm = _rm()
    poor = AccountSnapshot("ROOSTOO", 0, 0, 0)
    assert rm.check(_req(Side.SELL), poor).approved
    assert rm.check(_req(Side.SHORT_CLOSE), poor).approved


def test_buy_needs_free_usd_including_fee_buffer():
    rm = _rm()
    assert rm.check(_req(Side.BUY, qty=0.016), ACCOUNT).approved          # $960
    assert not rm.check(_req(Side.BUY, qty=0.0167), ACCOUNT).approved     # $1002


def test_reservation_blocks_second_entry_same_cycle():
    rm = _rm()
    assert rm.check(_req(Side.BUY, qty=0.01), ACCOUNT).approved           # $600
    assert not rm.check(_req(Side.SHORT_OPEN, qty=0.01), ACCOUNT,
                        reserved_usd=600).approved
