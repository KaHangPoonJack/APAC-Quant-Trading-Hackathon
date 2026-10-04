"""RoostooBroker translation, offline: legs/equity from balance + shorts,
order routing per side, fills derived from query_order history."""
import pytest

from core.enums import OrderStatus, OrderType, Side
from core.models import OrderRequest, PairRule, Ticker
from execution.broker import BrokerError, RoostooBroker

RULES = {"BTC/USD": PairRule("BTC/USD", 2, 5, 1.0),
         "ETH/USD": PairRule("ETH/USD", 2, 4, 1.0)}


class FakeMD:
    def tickers(self, force=False):
        return {"BTC/USD": Ticker("BTC/USD", 59990, 60010, 60000),
                "ETH/USD": Ticker("ETH/USD", 2999, 3001, 3000)}

    def rule(self, pair):
        return RULES.get(pair)


class FakeClient:
    _key = "KEY"

    def __init__(self):
        self.wallet = {"USD": {"Free": 4000, "Lock": 0},
                       "BTC": {"Free": 0.05, "Lock": 0.0},
                       "ETH": {"Free": 0, "Lock": 0},          # empty -> no leg
                       "DOGE": {"Free": 0.0001, "Lock": 0},    # no ticker -> skipped
                       "SOL": {"Free": -0.01, "Lock": 0}}      # rounding negative
        self.shorts = [{"Pair": "ETH/USD", "EntryPrice": 3100, "ShortQty": 1.0,
                        "Collateral": 3100, "CurrentPrice": 3001,
                        "UnrealizedPNL": 99, "PositionValue": 3199}]
        self.calls = []
        self.orders = []

    def balance(self):
        self.calls.append("balance")
        return self.wallet

    def short_positions(self):
        self.calls.append("short_positions")
        return self.shorts

    def place_order(self, pair, side, quantity, order_type="MARKET", price=None):
        self.calls.append(("place_order", pair, side, quantity, order_type, price))
        return {"OrderID": 81, "Status": "FILLED", "FilledQuantity": float(quantity),
                "FilledAverPrice": 60010}

    def short_open(self, pair, collateral, price=None):
        self.calls.append(("short_open", pair, collateral, price))
        return {"ID": 412, "Status": "OPEN", "ShortQty": 0.0166, "EntryPrice": 59990}

    def short_close(self, pair, close_qty=None, close_pct=None):
        self.calls.append(("short_close", pair, close_qty))
        return {"ClosePrice": 3001, "ClosedQty": float(close_qty), "FullyClosed": True}

    def query_order(self, **kw):
        self.calls.append(("query_order", kw))
        return self.orders


@pytest.fixture()
def broker():
    return RoostooBroker(FakeClient(), FakeMD(), env="TEST", clock=lambda: 0.0)


def test_legs_from_spot_and_shorts(broker):
    legs = {(p.code, p.qty > 0): p for p in broker.position_legs()}
    assert set(legs) == {("BTC/USD", True), ("ETH/USD", False)}
    btc = legs[("BTC/USD", True)]
    assert btc.qty == 0.05 and btc.market_value == pytest.approx(3000)
    eth = legs[("ETH/USD", False)]
    assert eth.qty == -1.0 and eth.market_value == pytest.approx(-3001)
    assert eth.avg_price == 3100 and eth.unrealized_pl == 99


def test_equity_is_wallet_usd_plus_longs_plus_short_position_value(broker):
    acc = broker.account()
    assert acc.total_assets == pytest.approx(4000 + 3000 + 3199)
    assert acc.cash == 4000 and acc.buying_power == 4000 and acc.currency == "USD"


def test_reads_are_cached_within_a_poll(broker):
    broker.account(); broker.positions(); broker.position_legs()
    assert broker._c.calls.count("balance") == 1
    assert broker._c.calls.count("short_positions") == 1


def test_net_positions_combine_legs():
    c = FakeClient()
    c.wallet["ETH"] = {"Free": 0.5, "Lock": 0}
    b = RoostooBroker(c, FakeMD(), clock=lambda: 0.0)
    eth = b.positions()["ETH/USD"]
    assert eth.qty == pytest.approx(-0.5)
    assert len([p for p in b.position_legs() if p.code == "ETH/USD"]) == 2


def test_shorts_disabled_skips_the_call():
    c = FakeClient()
    b = RoostooBroker(c, FakeMD(), include_shorts=False, clock=lambda: 0.0)
    b.account()
    assert "short_positions" not in c.calls


def test_buy_is_rounded_down_and_formatted(broker):
    st = broker.place(OrderRequest("BTC/USD", Side.BUY, 0.0123456, 60010))
    call = broker._c.calls[-1]
    assert call == ("place_order", "BTC/USD", "BUY", "0.01234", "MARKET", None)
    assert st.order_id == "81" and st.status == OrderStatus.FILLED


def test_limit_price_rounded_to_precision(broker):
    broker.place(OrderRequest("BTC/USD", Side.SELL, 0.01, 60010.129, OrderType.LIMIT))
    assert broker._c.calls[-1][-1] == "60010.13"


def test_short_open_is_sized_by_collateral(broker):
    st = broker.place(OrderRequest("BTC/USD", Side.SHORT_OPEN, 0.01666, 59990))
    _, pair, collateral, price = broker._c.calls[-1]
    assert pair == "BTC/USD" and collateral == "999.43" and price is None
    assert st.order_id == "412" and st.status == OrderStatus.FILLED


def test_short_close_has_no_order_id(broker):
    st = broker.place(OrderRequest("ETH/USD", Side.SHORT_CLOSE, 1.0, 3001))
    assert broker._c.calls[-1] == ("short_close", "ETH/USD", "1")
    assert st.order_id == "" and st.filled_qty == 1.0


def test_unlisted_pair_and_dust_rejected(broker):
    with pytest.raises(BrokerError, match="not listed"):
        broker.place(OrderRequest("FOO/USD", Side.BUY, 1, 1))
    with pytest.raises(BrokerError, match="rounds to 0"):
        broker.place(OrderRequest("BTC/USD", Side.BUY, 0.000001, 60000))


def test_order_invalidates_cache(broker):
    broker.account()
    broker.place(OrderRequest("BTC/USD", Side.BUY, 0.01, 60010))
    broker.account()
    assert broker._c.calls.count("balance") == 2


def test_deals_from_finished_orders(broker):
    broker._c.orders = [
        {"OrderID": 81, "Pair": "BTC/USD", "Side": "BUY", "Status": "FILLED",
         "Type": "MARKET", "FilledQuantity": 0.01, "FilledAverPrice": 60000,
         "CommissionCoin": "BTC", "CommissionChargeValue": 0.000001,
         "FinishTimestamp": 1757980800000},
        {"OrderID": 82, "Pair": "BTC/USD", "Side": "BUY", "Status": "PENDING",
         "FilledQuantity": 0.005, "FilledAverPrice": 59000},        # still working
        {"OrderID": 83, "Pair": "BTC/USD", "Side": "SELL", "Status": "CANCELED",
         "FilledQuantity": 0, "FilledAverPrice": 0},                # nothing filled
        {"OrderID": 84, "Pair": "ETH/USD", "Side": "SHORT_CLOSE", "Status": "FILLED",
         "Quantity": 1.0, "Price": 3001, "CommissionCoin": "USD",
         "CommissionChargeValue": 3.0, "CreateTimestamp": 1757980900000},
    ]
    deals = {d["order_id"]: d for d in broker.deals()}
    assert set(deals) == {"81", "84"}
    assert deals["81"]["commission"] == pytest.approx(0.06)   # coin fee -> USD
    assert deals["81"]["deal_id"] == "roostoo-81" and deals["81"]["time"] == 1757980800000
    assert deals["84"]["side"] == "SHORT_CLOSE" and deals["84"]["qty"] == 1.0
    assert deals["84"]["price"] == 3001


def test_acc_id_is_stable_and_hides_the_key(broker):
    a = broker.acc_id()
    assert a == broker.acc_id() and a.startswith("roostoo-test-") and "KEY" not in a
