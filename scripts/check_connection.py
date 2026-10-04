"""Preflight check: Roostoo + Binance reachable, keys valid, account readable.

Usage:
    python -m scripts.check_connection

READ-ONLY — places no orders. Run it on every new machine (and on the EC2 box)
before starting the trader. With no keys configured it still checks the public
endpoints and reports the signed ones as skipped.

It prints the equity decomposition (wallet USD, spot coins at mid, short
PositionValue) so you can confirm the broker's equity math against the Roostoo
web UI on the TEST account.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import load_config
from core.logging_setup import setup_logging
from engine.factory import build_client, build_market_data
from execution.broker import RoostooBroker
from execution.roostoo_client import num

log = logging.getLogger("check_connection")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    cfg = load_config()
    setup_logging(cfg.logging.level, cfg.logging.dir)
    client = build_client(cfg)
    md = build_market_data(cfg, client)
    ok = True

    log.info("Roostoo %s | env=%s | keys %s", cfg.roostoo.base_url, cfg.roostoo.env,
             "present" if cfg.roostoo.has_keys else "MISSING")

    # -- public -----------------------------------------------------------
    try:
        offset = client.sync_time()
        log.info("serverTime ok — local clock offset %+d ms (limit ±60000)", offset)
    except Exception as exc:  # noqa: BLE001
        log.error("serverTime FAILED: %s", exc)
        return 1
    try:
        rules = md.pair_rules(force=True)
        log.info("exchangeInfo ok — %d pairs (%d crypto)", len(rules),
                 sum(1 for r in rules.values() if r.asset_type == "crypto"))
        for p in cfg.strategy.pairs:
            r = rules.get(p)
            if r is None:
                ok = False
                log.error("  %s NOT LISTED on Roostoo", p)
            else:
                log.info("  %s price_prec=%d amount_prec=%d MiniOrder=%s can_trade=%s",
                         p, r.price_precision, r.amount_precision, r.min_notional,
                         r.can_trade)
    except Exception as exc:  # noqa: BLE001
        ok = False
        log.error("exchangeInfo FAILED: %s", exc)
    try:
        tickers = md.tickers(force=True)
        for p in cfg.strategy.pairs:
            t = tickers.get(p)
            log.info("  ticker %s: %s", p, f"bid={t.bid} ask={t.ask} last={t.last}"
                     if t else "MISSING")
    except Exception as exc:  # noqa: BLE001
        ok = False
        log.error("ticker FAILED: %s", exc)

    # -- Binance --------------------------------------------------------------
    for p in cfg.strategy.pairs:
        try:
            bars = md.bars(p, cfg.strategy.bar_interval, 5)
            last = bars[-1] if bars else None
            log.info("  binance %s %s: %d bars, last %s close=%s", md.binance.symbol(p),
                     cfg.strategy.bar_interval, len(bars),
                     last.time.isoformat() if last else "-", last.close if last else "-")
        except Exception as exc:  # noqa: BLE001
            ok = False
            log.error("  binance %s FAILED: %s", p, exc)

    # -- signed ---------------------------------------------------------------
    if not cfg.roostoo.has_keys:
        log.warning("signed endpoints SKIPPED — add roostoo.keys.%s to "
                    "config/settings.local.yaml", cfg.roostoo.env)
        return 0 if ok else 1
    broker = RoostooBroker(client, md, env=cfg.roostoo.env, include_shorts=True)
    try:
        wallet = broker.wallet()
        usd = wallet.get("USD", {})
        log.info("balance ok — USD free=%.2f lock=%.2f; %d asset(s) in wallet",
                 num(usd, "Free"), num(usd, "Lock"), len(wallet))
        legs = broker.position_legs()
        for leg in legs:
            log.info("  leg %-10s qty=%+.8g value=%.2f", leg.code, leg.qty, leg.market_value)
        shorts = broker.short_rows()
        acc = broker.account()
        longs = sum(p.market_value for p in legs if p.qty > 0)
        short_val = sum(num(r, "PositionValue") for r in shorts)
        log.info("equity = USD %.2f + longs %.2f + shorts(PositionValue) %.2f = %.2f",
                 num(usd, "Free") + num(usd, "Lock"), longs, short_val, acc.total_assets)
        log.info("account id (DB): %s", broker.acc_id())
    except Exception as exc:  # noqa: BLE001
        ok = False
        log.error("balance/positions FAILED: %s", exc)
    try:
        pc = client.pending_count()
        log.info("pending orders: %s", pc.get("TotalPending", 0))
        hist = client.query_order(limit=5)
        log.info("recent orders: %d shown (of last 5)", len(hist))
        for r in hist:
            log.info("  #%s %s %s %s qty=%s filled=%s @%s", r.get("OrderID"), r.get("Pair"),
                     r.get("Side"), r.get("Status"), r.get("Quantity"),
                     r.get("FilledQuantity"), r.get("FilledAverPrice"))
    except Exception as exc:  # noqa: BLE001
        ok = False
        log.error("order queries FAILED: %s", exc)

    log.info("Roostoo calls used: %d (limit %d/min)", client.calls_total,
             cfg.roostoo.max_calls_per_minute)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
