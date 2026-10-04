# roostoo_compet

Trading bot for the **Roostoo Quant Trading Hackathon**. Roostoo is the broker
(spot + `/v6` shorts, REST); **Binance spot** is the bar (OHLCV) source. The
infrastructure is strategy-agnostic. Strategies are added in `signals/` and emit
**target portfolio weights**. The engine diffs those weights against what the
account actually holds and trades only the delta.

The scaffold was cloned from a private Futu-based system. Every strategy and all
Futu code were removed, so the repo starts with just the no-op example strategy.

---

## Runbook

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp config/settings.local.example.yaml config/settings.local.yaml   # fill in keys (git-ignored)
python -m alembic upgrade head          # create the SQLite schema (run_all does this too)
python -m scripts.check_connection      # READ-ONLY preflight: Roostoo + Binance + keys + equity
python -m scripts.run_all               # supervisor: trader, auto-restart, alerts
python -m pytest -q                     # offline tests (no network)
```

Run the trader alone with `python -m scripts.run_trader`. To diagnose
Telegram, run `python -m scripts.test_notification`.

**On the EC2 box** (Sydney, Session Manager, no SSH), run the supervisor
detached so it survives the session closing:
`nohup python -m scripts.run_all > logs/run_all.out 2>&1 &` (or use a systemd
unit). Binance's main host returns HTTP 451 in some regions. The default is the
market-data mirror `data-api.binance.vision`, and the client switches hosts
automatically on 451/403.

### Keys: TEST vs COMPETITION

Roostoo issues two key sets: one for the general/testing account and one for
the official round (FAQ Q30). `roostoo.env` in `settings.yaml` selects which set
is used. It defaults to **TEST**. Switch to `COMPETITION` deliberately, either
in config or with env `ROOSTOO_ENV=COMPETITION`. Keys live only in
`config/settings.local.yaml` (`roostoo.keys.<ENV>.api_key/secret_key`) or env
`ROOSTOO_API_KEY_<ENV>` / `ROOSTOO_SECRET_KEY_<ENV>`. Never commit them.

### Competition rules that constrain the code (official FAQ)

- **30 API calls/minute across ALL Roostoo endpoints.** Calls beyond that fail.
  One shared limiter (`max_calls_per_minute: 25`) gates every request,
  including retries.
- **Every strategy change must be committed** with a clear history, so commit
  each change separately.
- **No manual intervention.** You may not stop the bot, override it, or place
  trades by hand, so the bot must run unattended (supervisor + auto-restart +
  Telegram alerts). Because only the bot trades on the account, fills the
  recorder doesn't recognise are attributed to the running strategy.
- Long and short are both allowed. The system liquidates every account
  automatically at the end of the competition.

---

## Architecture

```
 Binance spot klines ──┐                 ┌── Roostoo ticker (1 call, all pairs)
                       ▼                 ▼
               data/market_data.py  (bars | tickers | pair rules, cached)
                       │
                       ▼
   signals/<strategy>.target_weights(ctx) ──► {pair: signed weight}   (pure)
                       │
   risk/risk_manager.clamp_weights   (per-pair / gross / net caps, shorts on/off)
                       │
   engine/rebalance.plan_rebalance   (pure: weights − holdings → delta orders,
                       │              sells/short-closes before buys/short-opens)
   risk/risk_manager.check           (free USD incl. fee buffer, intra-cycle reservation)
                       │
   execution/broker.RoostooBroker ──► execution/roostoo_client.RoostooClient ──► Roostoo REST
                       │
   engine/recorder (fills→trades, equity snapshots, daily returns, Telegram)
   engine/recovery (startup reconcile DB vs broker, per (pair, direction) leg)
```

`engine/trading_engine.py` runs the poll loop, `engine/factory.py` is the
composition root (strategy registry: `STRATEGIES`), and `scripts/run_all.py`
supervises the processes.

**Layer rule.** Only `execution/` talks to Roostoo, and only `data/` talks to
Binance. `signals/`, `risk/` and `engine/rebalance.py` are pure and
unit-tested. `core/` imports nothing else from the project.

### The strategy contract (`signals/base.py`)

```python
class MyStrategy(Strategy):
    name = "my_strategy"
    def target_weights(self, ctx: StrategyContext) -> dict[str, float] | None: ...
```

- `ctx` provides `now` (UTC), `pairs`, and `bars[pair]`, which holds closed
  Binance bars, oldest first. It also provides `tickers[pair]` (Roostoo bid/ask),
  `equity` (USD) and `weights` (the current signed weights).
- The return value is the **full book**. `+0.25` means 25% of equity long, and
  `-0.1` means a 10% short (needs `risk.allow_short: true`). A configured pair
  that is omitted is traded to flat. Returning `None` means "no change this
  cycle".
- With `strategy.volume_source: perp`, `ctx.volume_bars[pair]` also carries
  Binance USDⓈ-M perp bars (for liquidity screens; perp volume is ~5× spot).
  `fapi.binance.com` has no mirror and answers 451 from the US, so strategies
  must cope with those lists being empty.
- Strategies stay pure: no broker calls and no I/O. An optional
  `on_recover(legs)` hook runs at startup.
- To add a strategy, create the file in `signals/` (copy `example_noop.py`) and
  register it in `engine/factory.py` `STRATEGIES`. Then set `strategy.name`,
  `pairs`, `bar_interval`, `lookback_bars`, `rebalance_seconds` and `params` in
  `settings.yaml`. `params` is passed to the strategy's constructor untouched.

### Engine loop (`engine/trading_engine.py`)

Each poll (`engine.poll_interval_seconds`, default 60s) costs about 2–3 Roostoo
calls: one ticker call for all pairs, one balance call, and one short-positions
call when shorts are enabled. The broker caches these for 5s and drops the cache
after every order. Then the loop:

1. Rebalances when a new wall-clock bucket of `rebalance_seconds` starts. It
   waits 5s past the boundary so the Binance bar has closed. A rebalance asks
   the strategy for weights, clamps them, plans orders, checks risk, submits,
   and records each order.
2. Runs `sync_fills` every `fill_sync_every_n_polls` polls and right after
   trading. This is one `query_order` call.
3. Writes equity and deployment snapshots every `snapshot_interval_minutes`, the
   UTC-midnight daily-return rollup, and a daily benchmark refresh.
4. Writes the heartbeat, including Roostoo calls in the last 60s and `last_error`.

Reconciliation is **stateless**: each rebalance re-diffs against real holdings,
so missed fills, cancelled orders and restarts all correct themselves. With
`order_type: LIMIT`, orders are priced at the opposite touch, and unfilled ones
are cancelled after `stale_order_seconds`. Pending orders are also cancelled
before each rebalance so nothing doubles up. After 5 consecutive failed polls
the system Telegram bot is alerted once, and once more on recovery.

---

## Roostoo API: quirks encoded in `execution/roostoo_client.py`

- **Signing**: HMAC-SHA256(secret, key-sorted `k=v&…`), sent in headers
  `RST-API-KEY` and `MSG-SIGNATURE`. GET requests carry the params in the query
  string; POST requests send them as a form-urlencoded body. Only send
  documented params, because the server signs only those (`None` values are
  dropped). A test checks the signature against the README example
  (`20b7fd55…`).
- **`timestamp`** is 13-digit milliseconds and must be within ±60s of server
  time. The client measures the server offset at startup (`sync_time`).
- **Failures return HTTP 200 with `Success: false`**. The client raises
  `RoostooError` in that case, except for "no order matched" and "no pending
  order", which come back as empty results.
- **Zero-valued fields are omitted** from responses. Always read them through
  `num(d, key)`.
- **Order-creating POSTs are never retried after a timeout or 5xx**, because the
  order may already exist (`RoostooError.ambiguous`). Read-only calls retry with
  backoff.
- **Shorts** (`/v6`) are sized by USD **collateral**, not quantity. The broker
  sends `qty × price`. Market shorts fill at MaxBid. Opening or closing a short
  costs 0.1% each way. Closes are reduce-only and return **no order id**; the
  fill shows up in `query_order` with `Side=SHORT_CLOSE`.
- **Equity** = wallet USD (Free+Lock) + spot coins × mid + Σ short
  `PositionValue`. A short's collateral leaves the wallet when the short opens,
  and `PositionValue` = collateral + unrealized PnL.
  `scripts/check_connection.py` prints each term so you can check it against
  the Roostoo UI.
- **Fills** come from finished orders in `query_order` (there is no deal feed),
  keyed `roostoo-<OrderID>`. A BUY commission charged in the coin is converted
  to USD. In that case the broker holds slightly less than the recorded qty;
  rebalancing uses broker holdings, and recovery adjusts the DB.
- `exchangeInfo` includes **tokenized stocks** (`AssetType: "stock"`, e.g.
  `NVDAB/USD`). Binance has no data for these.
- Small negative balances such as −0.01 are rounding (FAQ Q24) and are ignored.

### Known unknowns: check on the TEST account before relying on them

1. The exact `query_order` fields on SHORT_OPEN / SHORT_CLOSE rows. `deals()`
   falls back from `FilledQuantity`/`FilledAverPrice` to `Quantity`/`Price`.
2. Whether `/v6/short_open`'s `ID` (a position id when the short fills
   immediately) matches the `OrderID` in `query_order`. If it doesn't, short
   opens are recorded as adopted orders under the running strategy, so PnL is
   still correct.
3. Whether the equity formula matches the Roostoo leaderboard's balance once a
   short is open.
4. Whether `ticker` counts toward the 30/min limit. The code assumes it does.

---

## Persistence (`store/`)

The DB is SQLite (WAL) at `data/roostoo_compet.db` (git-ignored). Access goes
through SQLAlchemy and `store.repository.UnitOfWork` (the only SQL surface), and
Alembic manages the schema (one initial migration). **Timestamps are UTC**
(`UTCDateTime`, columns `*_utc`) and values are **USD**. There are 16 generic
tables, including strategies/deployments (one deployment per `strategy.name` ×
env), accounts, orders, fills, trades (round trips, partial-close PnL, LONG and
SHORT kept separate per pair), equity snapshots and composition,
deployment-equity snapshots (MTM and win rate), Modified-Dietz daily returns,
FX, and benchmarks.

When a model changes, run `python -m alembic revision --autogenerate -m "..."`.
Then add `import store.timeutil` to the generated file, and give every
constraint a name (SQLite batch mode requires it; see
`store/migrations/README.md`).

## Liveness and notifications

- `core/heartbeat.py`: the trader writes `logs/heartbeat_trader.json`, and
  `run_all` raises an alert if the process is alive but has stopped beating.
- `core/notifier.py`: one Telegram bot per strategy name plus a `system` bot.
  Missing bots fall back to `system`, then to a no-op, and a failed send never
  breaks the loop. Trade open/close alerts are sent after the DB commit.
- `engine/status_report.py`: hourly Telegram report of equity and holdings
  (`engine.status_report_minutes`, 0 = off). It reuses the poll's account
  snapshot, so it costs no Roostoo calls.

## Conventions

- Pairs use Roostoo format (`BTC/USD`). Binance symbols (`BTCUSDT`) appear only
  inside `data/binance.py`.
- Quantities are floats. Round them **down** with `PairRule.round_qty`, and keep
  every order's value above `MiniOrder` and `risk.min_order_usd`.
- New settings go in `settings.yaml` together with their dataclass in
  `core/config.py`. Strategy-only knobs go in `strategy.params`.
- Each stage of the loop is fail-isolated: one bad response or pair never kills
  the loop.
