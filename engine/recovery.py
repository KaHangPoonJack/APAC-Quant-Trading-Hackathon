"""Reboot recovery — reconcile the DB's view of open trades against the broker.

Principle:
  * the **broker** is the source of truth for *positions and fills*;
  * the **DB** is the source of truth for *strategy metadata* (which strategy
    opened a trade, TP/SL, entry reason).

Run once on startup, before the trading loop:
  1. `sync_fills` — fold in any executions that happened while we were down
     (this alone closes trades that were exited and opens/adopts new ones).
  2. Reconcile remaining open trades against live position LEGS. A leg is one
     (pair, direction): a spot holding is LONG, a /v6 short is SHORT, and both
     can exist on the same pair at once, so trades are matched per leg:
       - broker flat        -> close the trade (exit unknown; mark reconciled)
       - qty mismatch       -> adjust trade qty to the broker's truth
       - held, qty matches  -> resume as-is
  3. Adopt any broker leg that has no open trade (orphan).
  4. Tell the strategy what is actually held (optional `on_recover` hook).

Lives in `engine/` (not `store/`) because it orchestrates broker + strategy +
persistence together; `store/` stays dependency-free of those layers.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List

from core.models import MARKET
from engine.recorder import PersistenceService
from store.enums import TradeDirection
from store.repository import UnitOfWork

log = logging.getLogger(__name__)


@dataclass
class RecoveryReport:
    resumed: List[str] = field(default_factory=list)
    closed: List[str] = field(default_factory=list)
    adopted: List[str] = field(default_factory=list)
    adjusted: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"resumed={self.resumed} closed={self.closed} "
                f"adopted={self.adopted} adjusted={self.adjusted}")


class RecoveryService:
    def __init__(self, session_factory, broker, recorder: PersistenceService,
                 strategy, markets: List[str] | None = None):
        self._sf = session_factory
        self._broker = broker
        self._recorder = recorder
        self._strategy = strategy
        self._markets = [m.upper() for m in (markets or [MARKET])]

    def recover(self) -> RecoveryReport:
        report = RecoveryReport()
        for market in self._markets:
            try:
                self._recover_market(market, report)
            except Exception as exc:  # noqa: BLE001 - one market must not block others
                log.exception("recovery failed for %s: %s", market, exc)
        log.info("Reboot recovery complete | %s", report.summary())
        return report

    def _recover_market(self, market: str, report: RecoveryReport) -> None:
        # 1. Fold in fills that occurred while we were down.
        self._recorder.sync_fills(market)

        acc_id = self._recorder.account_id(market)
        legs = {}                                        # (code, direction) -> Position
        for pos in self._broker.position_legs(market):
            if abs(pos.qty) > 1e-12:
                d = TradeDirection.LONG if pos.qty > 0 else TradeDirection.SHORT
                legs[(pos.code, d)] = pos

        # Resolve outside the write transaction: deployment_id() opens its own
        # session, and nesting a second writer inside this one locks SQLite.
        strat_name = getattr(self._strategy, "name", "adopted")
        dep_id = self._recorder.deployment_id(strat_name)

        with UnitOfWork(self._sf) as uow:
            open_trades = {(t.symbol, t.direction): t
                           for t in uow.trades.open_for_account(acc_id)}

            # 2. Reconcile each open DB trade against the live leg.
            for key, trade in open_trades.items():
                label = f"{key[0]} {key[1].value}"
                pos = legs.get(key)
                broker_qty = abs(pos.qty) if pos else 0.0
                if broker_qty <= 1e-12:
                    # Broker flat but DB open, and no closing fill was found:
                    # close at the last known entry price (exit is unknown).
                    uow.trades.close_trade(trade, exit_price=trade.entry_price,
                                           commission_total=None)
                    report.closed.append(label)
                    log.warning("[%s] %s: DB open but broker flat -> closed (reconciled)",
                                market, label)
                elif abs(broker_qty - trade.qty) > 1e-9 * max(1.0, broker_qty):
                    log.warning("[%s] %s: qty mismatch DB=%s broker=%s -> adjusted",
                                market, label, trade.qty, broker_qty)
                    trade.qty = broker_qty
                    uow.session.flush()
                    report.adjusted.append(label)
                    report.resumed.append(label)
                else:
                    report.resumed.append(label)

            # 3. Adopt broker legs that have no open trade.
            for key, pos in legs.items():
                if key in open_trades:
                    continue
                uow.trades.open_trade(
                    account_id=acc_id, symbol=key[0], direction=key[1],
                    qty=abs(pos.qty), entry_price=pos.avg_price,
                    strategy=strat_name, deployment_id=dep_id,
                )
                label = f"{key[0]} {key[1].value}"
                report.adopted.append(label)
                log.warning("[%s] %s: broker holds but no DB trade -> adopted",
                            market, label)

        # 4. Let the strategy know what is actually held.
        hook = getattr(self._strategy, "on_recover", None)
        if callable(hook):
            try:
                hook(list(legs.values()))
            except Exception as exc:  # noqa: BLE001
                log.error("strategy.on_recover failed: %s", exc)
