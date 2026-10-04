"""PersistenceService — bridges the broker/execution layer to the trade DB.

Responsibilities:
  * ensure a DB account row exists per (broker, market, env) — on Roostoo there
    is one market, core.models.MARKET,
  * record orders on submission,
  * turn broker fills into round-trip trades (idempotent) — spot BUY/SELL build
    LONG trades, /v6 SHORT_OPEN/SHORT_CLOSE build SHORT trades,
  * write hourly equity snapshots (FX captured first) + upsert positions,
  * roll up daily returns with Modified Dietz.

It depends only on a small broker interface (acc_id / account / deals), so it is
fully testable offline with a fake broker.
"""
from __future__ import annotations

import logging
from datetime import datetime, time as dtime, timedelta
from typing import Dict, Optional

from sqlalchemy.orm import Session, sessionmaker

from core.enums import OrderStatus, OrderType, Side
from core.models import MARKET, OrderState
from core.notifier import NotifierRegistry, NullNotifier
from engine.fx import FxProvider
from store.enums import AssetClass, CashFlowKind, TradeDirection
from store.repository import UnitOfWork
from store.returns import WeightedFlow, modified_dietz
from store.timeutil import UTC_TZ, from_epoch_ms, now_utc, to_utc

log = logging.getLogger(__name__)

# Which trade direction each fill side opens / closes.
_OPENS = {"BUY": TradeDirection.LONG, "SHORT_OPEN": TradeDirection.SHORT}
_CLOSES = {"SELL": TradeDirection.LONG, "SHORT_CLOSE": TradeDirection.SHORT}


def _num(v) -> str:
    """Trim trailing zeros so 100.0 -> '100', 0.000136 stays precise."""
    if v is None:
        return "-"
    f = float(v)
    return str(int(f)) if f == int(f) else f"{f:g}"


def _duration(seconds) -> str:
    if not seconds:
        return "-"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {sec}s"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


class PersistenceService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker,                       # duck-typed: acc_id/account/deals
        fx: FxProvider,
        broker_name: str = "ROOSTOO",
        reporting_ccy: str = "USD",
        trd_env: str = "TEST",
        notifiers: Optional[NotifierRegistry] = None,
        allocations: Optional[Dict[str, float]] = None,
        default_strategy: str = "adopted",
    ):
        self._sf = session_factory
        self._broker = broker
        self._fx = fx
        self._broker_name = broker_name
        self._reporting = reporting_ccy.upper()
        self._trd_env = trd_env.upper()
        self._notifiers = notifiers or NotifierRegistry({}, NullNotifier())
        self._allocations = allocations or {}
        # Fills for orders we never recorded are attributed here. The competition
        # forbids manual trading, so on a bot-only account the running strategy
        # is the right owner (and /v6 short closes have no order id to record).
        self._default_strategy = default_strategy
        self._acc_cache: Dict[str, int] = {}   # market -> db account id
        self._dep_cache: Dict[str, int] = {}   # strategy name -> db deployment id

    # -- account mapping ------------------------------------------------
    def account_id(self, market: str = MARKET) -> int:
        market = market.upper()
        if market in self._acc_cache:
            return self._acc_cache[market]
        external = str(self._broker.acc_id(market))
        with UnitOfWork(self._sf) as uow:
            acc = uow.accounts.get_or_create(
                broker=self._broker_name, external_acc_id=external,
                trd_env=self._trd_env, base_currency=self._reporting,
                market=market,
            )
            self._acc_cache[market] = acc.id
        return self._acc_cache[market]

    def deployment_id(self, strategy: str) -> int:
        """Resolve (create) the deployment for a strategy in the current env.

        One deployment per (strategy, trd_env); promotion sim->real is naturally a
        separate deployment because trd_env differs.
        """
        if strategy in self._dep_cache:
            return self._dep_cache[strategy]
        with UnitOfWork(self._sf) as uow:
            strat = uow.strategies.get_or_create(strategy)
            dep = uow.deployments.get_or_create(
                strategy_id=strat.id, trd_env=self._trd_env,
                reporting_ccy=self._reporting,
                allocated_capital=self._allocations.get(strategy, 0.0),
            )
            self._dep_cache[strategy] = dep.id
        return self._dep_cache[strategy]

    # -- orders ---------------------------------------------------------
    def record_order(self, market: str, order: OrderState, strategy: str,
                     reason: str = "", tp_level: Optional[float] = None,
                     sl_level: Optional[float] = None,
                     order_type: OrderType = OrderType.MARKET) -> None:
        acc_id = self.account_id(market)
        dep_id = self.deployment_id(strategy)
        with UnitOfWork(self._sf) as uow:
            uow.orders.record(
                account_id=acc_id, broker_order_id=order.order_id, symbol=order.code,
                side=order.side, order_type=order_type, qty=order.qty,
                limit_price=order.price, status=OrderStatus.SUBMITTED,
                strategy=strategy, reason=reason, deployment_id=dep_id,
                tp_level=tp_level, sl_level=sl_level,
            )

    # -- fills -> trades ------------------------------------------------
    def sync_fills(self, market: str = MARKET) -> int:
        """Pull broker fills and fold new ones into orders/trades. Returns the
        number of newly-recorded fills."""
        acc_id = self.account_id(market)
        deals = self._broker.deals(market)
        new = 0
        events: list[tuple[str, dict]] = []  # (OPEN|CLOSE, details) — sent post-commit
        with UnitOfWork(self._sf) as uow:
            for d in deals:
                if uow.fills.get(d["deal_id"]) is not None:
                    continue  # already processed
                order = uow.orders.get_by_broker_id(acc_id, d["order_id"])
                if order is None:
                    # Fill for an order we never recorded (a short close, an order
                    # placed before persistence was on) — adopt a minimal order.
                    order = uow.orders.record(
                        account_id=acc_id, broker_order_id=d["order_id"],
                        symbol=d["code"], side=Side(d["side"]),
                        order_type=OrderType(d.get("order_type", "MARKET")),
                        qty=d["qty"], limit_price=d["price"],
                        status=OrderStatus.FILLED, strategy=self._default_strategy,
                        deployment_id=self.deployment_id(self._default_strategy),
                    )
                uow.fills.record(
                    order_id=order.id, broker_deal_id=d["deal_id"], qty=d["qty"],
                    price=d["price"], commission=d.get("commission", 0.0),
                    currency=d.get("currency", ""),
                    ts_utc=self._parse_ts(d.get("time")),
                )
                self._apply_fill_to_trade(uow, acc_id, order, d, events)
                new += 1
        if new:
            log.info("[%s] recorded %d new fill(s)", market, new)
        # Notify only after the transaction has committed.
        for kind, details in events:
            self._notify_trade(kind, details)
        return new

    def _apply_fill_to_trade(self, uow: UnitOfWork, acc_id: int, order, d: dict,
                             events: list) -> None:
        symbol, side = d["code"], str(d["side"])
        qty, price = d["qty"], d["price"]
        ts = self._parse_ts(d.get("time"))

        if side in _OPENS:
            direction = _OPENS[side]
            # Scope to the order's deployment so several strategies can hold the
            # same pair concurrently (the internal per-strategy ledger).
            open_trade = uow.trades.open_for_symbol(
                acc_id, symbol, deployment_id=order.deployment_id, direction=direction)
            if open_trade is None:
                trade = uow.trades.open_trade(
                    account_id=acc_id, symbol=symbol, direction=direction,
                    qty=qty, entry_price=price, strategy=order.strategy,
                    deployment_id=order.deployment_id,
                    order_type=order.order_type, tp_level=order.tp_level,
                    sl_level=order.sl_level, entry_ts_utc=ts,
                )
                order.trade_id = trade.id
                events.append(("OPEN", {
                    "strategy": trade.strategy, "symbol": symbol,
                    "direction": trade.direction.value, "qty": trade.qty,
                    "entry_price": trade.entry_price, "entry_ts": ts,
                    "tp": trade.tp_level, "sl": trade.sl_level,
                }))
            else:
                uow.trades.increase(open_trade, qty, price)
                order.trade_id = open_trade.id
            return

        if side not in _CLOSES:
            log.warning("[%s] fill with unknown side %r — skipping trade update",
                        symbol, side)
            return
        open_trade = uow.trades.open_for_symbol(
            acc_id, symbol, deployment_id=order.deployment_id, direction=_CLOSES[side])
        if open_trade is None:
            log.warning("[%s] %s fill with no open trade — skipping trade update",
                        symbol, side)
            return
        order.trade_id = open_trade.id
        if qty >= open_trade.qty - 1e-9:            # fully closes
            uow.trades.close_trade(open_trade, exit_price=price, exit_ts_utc=ts)
            events.append(("CLOSE", {
                "strategy": open_trade.strategy, "symbol": symbol,
                "direction": open_trade.direction.value, "qty": open_trade.qty,
                "entry_price": open_trade.entry_price, "exit_price": price,
                "pnl_nominal": open_trade.pnl_nominal, "pnl_pct": open_trade.pnl_pct,
                "commission": open_trade.commission_total,
                "duration_sec": open_trade.duration_sec, "exit_ts": ts,
            }))
        else:                                        # partial
            uow.trades.reduce(open_trade, qty, price)  # books slice PnL
            log.info("[%s] partial close x%s @ %s (realized so far %.2f)",
                     symbol, qty, price, open_trade.realized_pnl or 0.0)

    # -- equity snapshots ----------------------------------------------
    def snapshot_market(self, market: str = MARKET,
                        ts_utc: Optional[datetime] = None) -> None:
        acc_id = self.account_id(market)
        ts = ts_utc or now_utc()
        acc = self._broker.account(market)
        base = acc.currency or self._reporting
        rate = self._fx.rate(base, self._reporting)

        cash_r = acc.cash * rate
        mv_r = sum(p.market_value for p in acc.positions.values()) * rate
        total_r = (acc.total_assets * rate) if acc.total_assets else (cash_r + mv_r)

        with UnitOfWork(self._sf) as uow:
            if base != self._reporting:
                uow.fx.record(base, self._reporting, rate, ts_utc=ts)

            prev = uow.equity.latest(acc_id)
            since = prev.ts_utc if prev else datetime(1970, 1, 1, tzinfo=UTC_TZ)
            net_flow = self._net_flow(uow, acc_id, since, ts)

            uow.equity.write_snapshot(
                account_id=acc_id, total_equity=total_r, cash=cash_r, market_value=mv_r,
                currency_comp={base: total_r},
                assetclass_comp={AssetClass.CASH: cash_r, AssetClass.CRYPTO: mv_r},
                net_flow_since_prev=net_flow, reporting_ccy=self._reporting, ts_utc=ts,
            )

            held = set(acc.positions)
            for code, p in acc.positions.items():
                last = (p.market_value / p.qty) if p.qty else 0.0
                uow.positions.upsert(
                    account_id=acc_id, symbol=code, qty=p.qty, avg_price=p.avg_price,
                    last_price=last, market_value=p.market_value,
                    unrealized_pl=p.unrealized_pl, today_pl=p.today_pl,
                    asset_class=AssetClass.CRYPTO,
                    currency=p.currency or base, ts_utc=ts,
                )
            for row in uow.positions.current(acc_id):
                if row.symbol not in held:
                    uow.positions.remove(acc_id, row.symbol)

    def snapshot_deployments(self, markets: Optional[list] = None) -> None:
        """Write one mark-to-market equity point per active deployment.

        Per-strategy equity = allocated_capital + realized (closed trades) +
        unrealized (open trades marked to the latest price), in USD.
        """
        # Latest price per pair, from broker positions.
        price: Dict[str, float] = {}
        for m in markets or [MARKET]:
            try:
                for code, p in self._broker.positions(m).items():
                    if p.qty:
                        price[code] = p.market_value / p.qty
            except Exception as exc:  # noqa: BLE001
                log.debug("price fetch failed for %s: %s", m, exc)

        ts = now_utc()
        with UnitOfWork(self._sf) as uow:
            for dep in uow.deployments.active():
                # Closed trades: SQL aggregate (constant cost as history grows).
                realized = uow.trades.realized_pnl(dep.id)
                unrealized = 0.0
                for t in uow.trades.open_for_deployment(dep.id):
                    # Partial exits already realized on a still-open trade.
                    realized += t.realized_pnl or 0.0
                    last = price.get(t.symbol, t.entry_price)
                    sign = 1.0 if t.direction == TradeDirection.LONG else -1.0
                    unrealized += (last - t.entry_price) * t.qty * sign
                wins, losses = uow.trades.win_stats(dep.id)
                uow.deployment_equity.write_snapshot(
                    dep.id, dep.allocated_capital, realized, unrealized,
                    reporting_ccy=self._reporting, wins=wins, losses=losses,
                    ts_utc=ts)

    def _net_flow(self, uow: UnitOfWork, acc_id: int, since: datetime,
                  until: datetime) -> float:
        total = 0.0
        for cf in uow.cash_flows.between(acc_id, since, until):
            rate = self._fx.rate(cf.currency, self._reporting)
            signed = cf.amount if cf.kind == CashFlowKind.DEPOSIT else -cf.amount
            total += signed * rate
        return total

    # -- daily returns --------------------------------------------------
    def rollup_daily(self, market: str, day) -> None:
        """Compute Modified-Dietz return for one UTC calendar day.

        equity_open = previous day's close (chain-linkable), falling back to the
        last snapshot before the day starts, then the day's first snapshot."""
        acc_id = self.account_id(market)
        start = datetime.combine(day, dtime.min, tzinfo=UTC_TZ)
        end = datetime.combine(day, dtime.max, tzinfo=UTC_TZ)
        span = (end - start).total_seconds()

        with UnitOfWork(self._sf) as uow:
            snaps = [s for s in uow.equity.history(acc_id, limit=10_000)
                     if start <= s.ts_utc <= end]
            if not snaps:
                return
            prev_ret = uow.returns.get(acc_id, day - timedelta(days=1))
            if prev_ret is not None:
                equity_open = prev_ret.equity_close
            else:
                before = uow.equity.last_before(acc_id, start)
                equity_open = before.total_equity if before else snaps[0].total_equity
            equity_close = snaps[-1].total_equity

            flows = []
            net_flow = 0.0
            for cf in uow.cash_flows.between(acc_id, start, end):
                rate = self._fx.rate(cf.currency, self._reporting)
                signed = (cf.amount if cf.kind == CashFlowKind.DEPOSIT else -cf.amount) * rate
                weight = (end - cf.ts_utc).total_seconds() / span if span else 1.0
                flows.append(WeightedFlow(weight=weight, amount=signed))
                net_flow += signed

            return_pct, pnl = modified_dietz(equity_open, equity_close, flows)
            uow.returns.upsert_daily(
                account_id=acc_id, day=day, equity_open=equity_open,
                equity_close=equity_close, net_flow=net_flow,
                return_pct=return_pct, pnl_nominal=pnl,
            )
        log.info("[%s] daily return %s: %.3f%% (pnl %.2f)", market, day, return_pct, pnl)

    def rollup_missing(self, market: str = MARKET) -> int:
        """Roll up every past day that has snapshots but no daily_returns row.

        Restart-safe replacement for in-memory day tracking: derives pending work
        from the DB, so days missed while the engine was down (crash, reboot,
        stopped over midnight) are backfilled on the next start."""
        acc_id = self.account_id(market)
        today = now_utc().date()
        with UnitOfWork(self._sf) as uow:
            snap_days = set(uow.equity.snapshot_dates(acc_id))
            done = set(uow.returns.dates(acc_id))
        pending = sorted(d for d in snap_days if d < today and d not in done)
        for day in pending:
            self.rollup_daily(market, day)
        return len(pending)

    # -- per-strategy ledger --------------------------------------------
    def open_ledger_qty(self, strategy: str, symbol: str) -> Optional[float]:
        """Open qty this strategy's deployment holds in `symbol` per the trade
        ledger, signed (+long / -short; 0.0 if none). Lets the engine avoid
        selling coins that belong to another deployment on the same account."""
        acc_id = self.account_id()
        dep_id = self.deployment_id(strategy)
        total = 0.0
        with UnitOfWork(self._sf) as uow:
            for direction, sign in ((TradeDirection.LONG, 1.0),
                                    (TradeDirection.SHORT, -1.0)):
                t = uow.trades.open_for_symbol(acc_id, symbol, deployment_id=dep_id,
                                               direction=direction)
                if t is not None:
                    total += sign * float(t.qty)
        return total

    # -- notifications --------------------------------------------------
    def _notify_trade(self, kind: str, d: dict) -> None:
        # Route to the strategy's own bot (falls back to system → null).
        notifier = self._notifiers.for_strategy(d.get("strategy", ""))
        try:
            notifier.send(self._format_open(d) if kind == "OPEN"
                          else self._format_close(d))
        except Exception as exc:  # noqa: BLE001 - never let notify break the loop
            log.error("notify failed: %s", exc)

    def _format_open(self, d: dict) -> str:
        ts = d["entry_ts"].strftime("%Y-%m-%d %H:%M:%S") if d.get("entry_ts") else "-"
        lines = [
            "🟢 TRADE OPENED",
            f"{d['symbol']}  {d['direction']} x{_num(d['qty'])}",
            f"Strategy: {d['strategy']} [{self._trd_env}]",
            f"Entry: {_num(d['entry_price'])}",
            f"Time (UTC): {ts}",
        ]
        if d.get("tp") is not None:
            lines.append(f"TP: {_num(d['tp'])}")
        if d.get("sl") is not None:
            lines.append(f"SL: {_num(d['sl'])}")
        return "\n".join(lines)

    def _format_close(self, d: dict) -> str:
        pnl = d.get("pnl_nominal") or 0.0
        pct = d.get("pnl_pct") or 0.0
        emoji = "✅" if pnl >= 0 else "❌"
        ts = d["exit_ts"].strftime("%Y-%m-%d %H:%M:%S") if d.get("exit_ts") else "-"
        dur = _duration(d.get("duration_sec"))
        return "\n".join([
            f"{emoji} TRADE CLOSED",
            f"{d['symbol']}  {d['direction']} x{_num(d['qty'])}",
            f"Strategy: {d['strategy']} [{self._trd_env}]",
            f"Entry {_num(d['entry_price'])} → Exit {_num(d['exit_price'])}",
            f"PnL: {pnl:+,.2f} ({pct:+.2f}%)",
            f"Commission: {_num(d.get('commission') or 0.0)}",
            f"Duration: {dur}",
            f"Time (UTC): {ts}",
        ])

    # -- helpers --------------------------------------------------------
    @staticmethod
    def _parse_ts(value) -> datetime:
        """Broker timestamps: Roostoo sends 13-digit epoch ms; datetimes and ISO
        strings are accepted too (tests, other sources). Naive = UTC."""
        if value is None or value == "" or value == 0:
            return now_utc()
        if isinstance(value, datetime):
            return to_utc(value)
        if isinstance(value, (int, float)) or str(value).isdigit():
            return from_epoch_ms(float(value))
        try:
            return to_utc(datetime.fromisoformat(str(value)))
        except ValueError:
            return now_utc()
