"""The trading loop: wires every layer together. Crypto trades 24/7, so there
are no sessions — just a poll cadence and a rebalance cadence.

Per poll (`engine.poll_interval_seconds`):
  1. refresh Roostoo tickers (1 call, all pairs) and the portfolio
     (balance + short positions, cached in the broker),
  2. cancel LIMIT orders that sat unfilled past `stale_order_seconds`,
  3. when a rebalance is due: Binance bars -> strategy.target_weights ->
     risk clamp -> plan delta orders -> risk check -> submit -> record,
  4. every `fill_sync_every_n_polls` polls (and right after trading): fold
     Roostoo order history into the trade DB,
  5. equity snapshots / daily returns / benchmarks on their own cadences, and
     the Telegram status report (equity + holdings) every
     `status_report_minutes`,
  6. heartbeat (with the rate-limit budget) for the supervisor.

Reconciliation is target-driven and stateless: each rebalance diffs the target
book against what the broker ACTUALLY holds, so a missed fill, a cancelled
order or a restart self-corrects on the next rebalance.

Rebalances are aligned to wall-clock multiples of `rebalance_seconds` (UTC),
e.g. 3600 -> just after each hourly bar closes.
"""
from __future__ import annotations

import logging
import time as _time
from typing import Dict, List, Optional

from core.config import AppConfig
from core.enums import OrderType
from core.heartbeat import TRADER, write_heartbeat
from core.models import MARKET, Bar, OrderRequest, OrderState
from core.notifier import Notifier
from data.market_data import MarketData
from engine.benchmarks import BenchmarkFetcher
from engine.rebalance import plan_rebalance, scale_entries
from engine.status_report import format_status
from engine.recorder import PersistenceService
from engine.recovery import RecoveryService
from execution.order_manager import OrderManager
from execution.portfolio import Portfolio
from risk.position_sizer import PositionSizer
from risk.risk_manager import FEE_BUFFER, RiskManager
from signals.base import Strategy, StrategyContext
from store.timeutil import now_utc

log = logging.getLogger(__name__)

# Consecutive failed polls before the system bot is alerted (once, latched).
ALERT_AFTER_FAILURES = 5
# Seconds after a rebalance boundary before acting, so the bar is closed on Binance.
BAR_CLOSE_GRACE_S = 5


class TradingEngine:
    def __init__(
        self,
        cfg: AppConfig,
        market_data: MarketData,
        strategy: Strategy,
        sizer: PositionSizer,
        risk: RiskManager,
        portfolio: Portfolio,
        orders: OrderManager,
        client=None,                         # RoostooClient, for budget/time sync
        recorder: Optional[PersistenceService] = None,
        recovery: Optional[RecoveryService] = None,
        benchmarks: Optional[BenchmarkFetcher] = None,
        notifier: Optional[Notifier] = None,
        wall_clock=_time.time,
    ):
        self.cfg = cfg
        self.md = market_data
        self.strategy = strategy
        self.sizer = sizer
        self.risk = risk
        self.portfolio = portfolio
        self.orders = orders
        self.client = client
        self.recorder = recorder
        self.recovery = recovery
        self.benchmarks = benchmarks
        self.notifier = notifier
        self._wall = wall_clock

        self.pairs: List[str] = list(cfg.strategy.pairs)
        self.order_type = OrderType(cfg.engine.order_type.upper())
        self._last_rebalance_bucket: Optional[int] = None
        self._polls = 0
        self._sync_pending = True
        self._order_first_seen: Dict[str, float] = {}
        self._last_snapshot_key: Optional[tuple] = None
        self._last_rollup_date = None
        self._last_report_bucket: Optional[int] = None
        self._last_report_equity: Optional[float] = None
        self._consecutive_failures = 0
        self._alerted = False
        self._last_error = ""
        self._running = False

    # -- lifecycle ------------------------------------------------------
    def start(self) -> None:
        """Recover prior state, then run the loop until stopped."""
        self._startup()
        self._running = True
        log.info("Engine started | strategy=%s | pairs=%s | bars=%s | rebalance=%ss | env=%s",
                 self.strategy.name, self.pairs, self.cfg.strategy.bar_interval,
                 self.cfg.strategy.rebalance_seconds, self.cfg.roostoo.env)
        try:
            while self._running:
                self.run_once()
                _time.sleep(self.cfg.engine.poll_interval_seconds)
        except KeyboardInterrupt:
            log.info("Interrupted — shutting down engine loop")
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False

    def _startup(self) -> None:
        if self.client is not None:
            try:
                self.client.sync_time()
            except Exception as exc:  # noqa: BLE001
                log.warning("server time sync failed (signed calls may be rejected): %s", exc)
        try:
            rules = self.md.pair_rules()
            missing = [p for p in self.pairs if p not in rules]
            if missing:
                log.error("configured pairs NOT listed on Roostoo: %s", missing)
        except Exception as exc:  # noqa: BLE001
            log.error("exchangeInfo failed at startup: %s", exc)
        # Reboot recovery must happen before any trading decision so we resume
        # open trades rather than duplicate or abandon them.
        if self.recovery is not None:
            log.info("Running reboot recovery...")
            self.recovery.recover()
        if self.recorder is not None:
            try:
                n = self.recorder.rollup_missing(MARKET)
                if n:
                    log.info("backfilled %d missed daily return(s)", n)
            except Exception as exc:  # noqa: BLE001
                log.error("rollup backfill failed: %s", exc)
        self._update_benchmarks()

    # -- one iteration --------------------------------------------------
    def run_once(self) -> None:
        self._polls += 1
        ok = True
        traded = False
        try:
            self.md.tickers(force=True)
            self.portfolio.refresh()
        except Exception as exc:  # noqa: BLE001
            ok = False
            self._fail(f"portfolio/ticker refresh failed: {exc}")

        if ok:
            self._handle_stale_orders()
            if self._rebalance_due():
                try:
                    traded = bool(self.rebalance())
                except Exception as exc:  # noqa: BLE001
                    ok = False
                    log.exception("rebalance failed: %s", exc)
                    self._fail(f"rebalance failed: {exc}")

        self._maybe_sync_fills()
        self._maybe_snapshot_and_rollup()
        if ok:
            self._maybe_status_report(refresh=traded)
            self._recovered()
        self._write_heartbeat()

    # -- rebalance --------------------------------------------------------
    def _rebalance_due(self) -> bool:
        period = max(1, int(self.cfg.strategy.rebalance_seconds))
        now = self._wall()
        bucket = int((now - BAR_CLOSE_GRACE_S) // period)
        return bucket != self._last_rebalance_bucket

    def _mark_rebalanced(self) -> None:
        period = max(1, int(self.cfg.strategy.rebalance_seconds))
        self._last_rebalance_bucket = int((self._wall() - BAR_CLOSE_GRACE_S) // period)

    def _bars(self) -> Dict[str, List[Bar]]:
        out: Dict[str, List[Bar]] = {}
        for pair in self.pairs:
            try:
                out[pair] = self.md.bars(pair, self.cfg.strategy.bar_interval,
                                         self.cfg.strategy.lookback_bars)
            except Exception as exc:  # noqa: BLE001 — one pair must not block the rest
                log.error("[%s] bar fetch failed: %s", pair, exc)
                out[pair] = []
        return out

    def _volume_bars(self) -> Dict[str, List[Bar]]:
        if self.cfg.strategy.volume_source != "perp":
            return {}
        out: Dict[str, List[Bar]] = {}
        for pair in self.pairs:
            try:
                out[pair] = self.md.perp_bars(pair, self.cfg.strategy.bar_interval,
                                              self.cfg.strategy.lookback_bars)
            except Exception as exc:  # noqa: BLE001
                log.error("[%s] perp bar fetch failed: %s", pair, exc)
                out[pair] = []
                if getattr(exc, "geo_blocked", False):
                    log.error("perp market data geo-blocked — skipping it this cycle")
                    return {p: [] for p in self.pairs}
        return out

    def rebalance(self) -> List[OrderState]:
        """Ask the strategy for its book and trade the delta. Returns orders sent."""
        tickers = self.md.tickers()
        ctx = StrategyContext(
            now=now_utc(), pairs=list(self.pairs), bars=self._bars(), tickers=tickers,
            equity=self.portfolio.equity, weights=self.portfolio.current_weights(),
            volume_bars=self._volume_bars(),
        )
        targets = self.strategy.target_weights(ctx)
        self._mark_rebalanced()
        if targets is None:
            return []

        # Full book over the configured universe: omitted pairs mean flat.
        book = {p: float(targets.get(p, 0.0)) for p in self.pairs}
        ignored = sorted(set(targets) - set(self.pairs))
        if ignored:
            log.warning("strategy returned pairs outside strategy.pairs (ignored): %s", ignored)
        rules = self.md.pair_rules()
        tradable = [p for p, r in rules.items() if r.can_trade]
        book, notes = self.risk.clamp_weights(book, tradable)
        for n in notes:
            log.info("risk: %s", n)

        if self.order_type == OrderType.LIMIT:
            # Resting orders from the last rebalance would double up with the
            # new plan; clear them and re-read holdings first.
            if self.orders.cancel_all():
                self.portfolio.refresh()

        plan = plan_rebalance(book, self.portfolio.equity, self.portfolio.legs,
                              tickers, rules, self.sizer, self.order_type)
        for n in plan.notes:
            log.info("plan: %s", n)
        if not plan.orders:
            log.info("rebalance: book already at target %s", book)
            return []

        sent: List[OrderState] = []
        account = self.portfolio.account
        exits = [o for o in plan.orders if not o.side.adds_exposure]
        entries = [o for o in plan.orders if o.side.adds_exposure]
        for req in exits:                       # always approved by risk.check
            state = self._submit(req)
            if state is not None:
                sent.append(state)
        if sent:
            # Exits free cash, but MARKET proceeds aren't in the cached
            # snapshot yet — re-read once so entries can use them.
            try:
                account = self.portfolio.refresh()
            except Exception as exc:  # noqa: BLE001
                log.warning("refresh after exits failed: %s", exc)

        # Share any cash shortfall across ALL entries (longs and shorts alike)
        # instead of letting the pairs that sort last absorb it.
        entries, scale_notes = scale_entries(entries, account.buying_power, rules,
                                             self.sizer, FEE_BUFFER)
        for n in scale_notes:
            log.info("plan: %s", n)

        reserved = 0.0
        for req in entries:
            decision = self.risk.check(req, account, reserved_usd=reserved)
            if not decision.approved:
                log.warning("[%s] %s blocked by risk: %s", req.code, req.side.value,
                            decision.reason)
                continue
            state = self._submit(req)
            if state is None:
                continue
            sent.append(state)
            reserved += req.notional
        self._sync_pending = True
        return sent

    def _submit(self, req: OrderRequest) -> Optional[OrderState]:
        state = self.orders.submit(req)
        if state is not None and state.order_id and self.recorder is not None:
            try:
                self.recorder.record_order(MARKET, state, self.strategy.name, req.reason,
                                           order_type=req.order_type)
            except Exception as exc:  # noqa: BLE001
                log.error("failed to record order %s: %s", state.order_id, exc)
        return state

    # -- housekeeping -------------------------------------------------------
    def _handle_stale_orders(self) -> None:
        """Cancel LIMIT orders unfilled past `stale_order_seconds`. No reprice
        needed: the next rebalance re-plans from actual holdings."""
        if self.order_type != OrderType.LIMIT:
            return                      # MARKET orders never rest; save the call
        try:
            open_os = self.orders.open_orders()
        except Exception as exc:  # noqa: BLE001
            log.debug("open-order query failed: %s", exc)
            return
        now = _time.monotonic()
        stale_after = max(1, self.cfg.engine.stale_order_seconds)
        open_ids = set()
        for o in open_os:
            open_ids.add(o.order_id)
            first = self._order_first_seen.setdefault(o.order_id, now)
            if now - first >= stale_after and self.orders.cancel(o.order_id):
                log.warning("[%s] cancelled stale %s order %s (unfilled %.0fs)",
                            o.code, o.side.value, o.order_id, now - first)
                self._sync_pending = True
        self._order_first_seen = {k: v for k, v in self._order_first_seen.items()
                                  if k in open_ids}

    def _maybe_sync_fills(self) -> None:
        if self.recorder is None:
            return
        every = max(1, self.cfg.engine.fill_sync_every_n_polls)
        if not (self._sync_pending or self._polls % every == 0):
            return
        try:
            self.recorder.sync_fills(MARKET)
            self._sync_pending = False
        except Exception as exc:  # noqa: BLE001
            log.error("sync_fills failed: %s", exc)

    def _maybe_snapshot_and_rollup(self) -> None:
        if self.recorder is None:
            return
        now = now_utc()
        interval = max(1, self.cfg.engine.snapshot_interval_minutes)
        bucket = (now.hour * 60 + now.minute) // interval
        key = (now.year, now.month, now.day, bucket)
        if self._last_snapshot_key == key:
            return
        self._last_snapshot_key = key
        try:
            self.recorder.snapshot_market(MARKET)
        except Exception as exc:  # noqa: BLE001
            log.error("equity snapshot failed: %s", exc)
        try:
            self.recorder.snapshot_deployments([MARKET])
        except Exception as exc:  # noqa: BLE001
            log.error("deployment snapshot failed: %s", exc)

        # Roll up past days once the UTC day advances (DB-derived, so gaps from
        # downtime are covered too).
        today = now.date()
        if self._last_rollup_date is None:
            self._last_rollup_date = today
        elif today != self._last_rollup_date:
            try:
                self.recorder.rollup_missing(MARKET)
            except Exception as exc:  # noqa: BLE001
                log.error("daily rollup failed: %s", exc)
            self._last_rollup_date = today
            self._update_benchmarks()

    def _maybe_status_report(self, refresh: bool = False) -> None:
        """Equity + holdings to the notifier on each wall-clock bucket of
        `status_report_minutes` (and once at startup). Uses this poll's
        portfolio snapshot, so it costs no Roostoo calls."""
        minutes = self.cfg.engine.status_report_minutes
        if minutes <= 0 or self.notifier is None or not self.portfolio.ready:
            return
        bucket = int(self._wall() // (minutes * 60))
        if bucket == self._last_report_bucket:
            return
        self._last_report_bucket = bucket
        try:
            # orders went out this poll: report holdings after them, not before
            account = self.portfolio.refresh() if refresh else self.portfolio.account
            text = format_status(
                now_utc(), self.strategy.name, self.cfg.roostoo.env, account,
                self.portfolio.legs, self.cfg.allocations.get(self.strategy.name),
                self._last_report_equity)
            self._last_report_equity = account.total_assets
            self.notifier.send(text)
        except Exception as exc:  # noqa: BLE001 - reporting must never break the loop
            log.error("status report failed: %s", exc)

    def _update_benchmarks(self) -> None:
        if self.benchmarks is None:
            return
        try:
            self.benchmarks.update()
        except Exception as exc:  # noqa: BLE001 - benchmarks must never break the loop
            log.error("benchmark update failed: %s", exc)

    # -- health ---------------------------------------------------------------
    def _fail(self, msg: str) -> None:
        self._consecutive_failures += 1
        self._last_error = msg
        log.error(msg)
        if (self._consecutive_failures >= ALERT_AFTER_FAILURES and not self._alerted
                and self.notifier is not None):
            self._alerted = True
            self.notifier.send(f"⚠️ TRADER DEGRADED — {self._consecutive_failures} failed "
                               f"polls in a row.\nLast error: {msg}")

    def _recovered(self) -> None:
        if self._alerted and self.notifier is not None:
            self.notifier.send("✅ TRADER RECOVERED — polls succeeding again.")
        self._consecutive_failures = 0
        self._alerted = False
        self._last_error = ""

    def _write_heartbeat(self) -> None:
        try:
            budget = {}
            if self.client is not None:
                budget = {"roostoo_calls_last_60s": self.client.limiter.calls_in_window(),
                          "roostoo_calls_total": self.client.calls_total}
            write_heartbeat(
                TRADER, self.cfg.engine.poll_interval_seconds,
                env=self.cfg.roostoo.env, strategy=self.strategy.name,
                pairs=self.pairs,
                equity=round(self.portfolio.equity, 2) if self.portfolio.ready else None,
                last_error=self._last_error, **budget,
            )
        except Exception as exc:  # noqa: BLE001 - never let heartbeat break the loop
            log.debug("heartbeat write failed: %s", exc)
