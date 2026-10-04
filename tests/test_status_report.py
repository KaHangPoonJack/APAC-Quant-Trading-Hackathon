"""Hourly Telegram status report: formatting and engine scheduling."""
from datetime import datetime, timezone

from core.models import MARKET, AccountSnapshot, Position
from engine.status_report import format_status
from signals.example_noop import NoOpStrategy
from tests.test_engine_persistence import FakeBroker, FixedWeights, _engine


class Capture:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return True


def test_format_lists_equity_change_and_both_sides():
    acc = AccountSnapshot(MARKET, cash=500.0, total_assets=51_000.0, buying_power=500.0)
    legs = [Position("SUI/USD", 100, 0, market_value=2_750.0),
            Position("ZEC/USD", -2, 0, market_value=-1_830.0)]
    text = format_status(datetime(2026, 10, 4, 13, tzinfo=timezone.utc), "xs_momentum",
                         "TEST", acc, legs, start_equity=50_000.0, prev_equity=50_500.0)
    assert "2026-10-04 13:00 UTC" in text and "Equity $51,000" in text
    assert "since start +$1,000 (+2.00%)" in text
    assert "since last report +$500 (+0.99%)" in text
    assert "Long $2,750 (1) | Short -$1,830 (1)" in text
    assert "SUI" in text and "+5.4%" in text and "ZEC" in text and "-3.6%" in text


def test_format_without_positions_or_history():
    acc = AccountSnapshot(MARKET, cash=50_000.0, total_assets=50_000.0, buying_power=50_000.0)
    text = format_status(datetime(2026, 10, 4, tzinfo=timezone.utc), "s", "TEST", acc, [],
                         start_equity=None, prev_equity=None)
    assert "since start n/a" in text and "No open positions." in text


def test_engine_reports_once_per_hour(tmp_path):
    now = {"t": 7200.0 + 10}
    eng, _ = _engine(tmp_path, NoOpStrategy(), FakeBroker(), clock=lambda: now["t"])
    eng.notifier = Capture()
    eng.run_once()                     # startup: first report
    now["t"] += 60
    eng.run_once()                     # same hour: nothing
    now["t"] += 3600
    eng.run_once()                     # next hour
    assert len(eng.notifier.sent) == 2
    assert "since last report +$0 (+0.00%)" in eng.notifier.sent[1]


def test_report_after_rebalance_shows_new_holdings(tmp_path):
    class FillingBroker(FakeBroker):
        def place(self, req):
            self.cash -= req.notional
            self.legs.append(Position(req.code, req.qty, req.price,
                                      market_value=req.notional))
            return super().place(req)

    eng, _ = _engine(tmp_path, FixedWeights({"BTC/USD": 0.5}), FillingBroker())
    eng.notifier = Capture()
    eng.run_once()
    assert "BTC" in eng.notifier.sent[0] and "Long $" in eng.notifier.sent[0]
    assert "No open positions." not in eng.notifier.sent[0]


def test_report_disabled_at_zero(tmp_path):
    from dataclasses import replace
    eng, _ = _engine(tmp_path, NoOpStrategy(), FakeBroker())
    eng.cfg = replace(eng.cfg, engine=replace(eng.cfg.engine, status_report_minutes=0))
    eng.notifier = Capture()
    eng.run_once()
    assert eng.notifier.sent == []
