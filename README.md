# roostoo_compet

An autonomous trading bot for the Roostoo Quant Trading Hackathon. It trades
through the **Roostoo** REST API (spot and shorts) and uses **Binance** public
spot klines as its market-data source.

## How it works

- A strategy (`signals/`) returns target portfolio weights, such as
  `{"BTC/USD": 0.4, "ETH/USD": -0.1}`.
- The engine applies risk caps and compares the targets with what the account
  actually holds.
- It sends only the orders needed to close the gap, selling before it buys.
- Every order, fill, round-trip trade and hourly equity snapshot is stored in
  SQLite. Telegram alerts and an hourly equity/holdings report provide monitoring.
- A supervisor (`scripts/run_all.py`) keeps the bot running unattended. It
  restarts crashed processes, detects a hung loop through heartbeats, and keeps
  every Roostoo call under the 30-calls-per-minute limit.

## Quick start

```bash
pip install -r requirements.txt
cp config/settings.local.example.yaml config/settings.local.yaml   # add API keys
python -m scripts.check_connection     # read-only preflight
python -m scripts.run_all              # run the trader under the supervisor
python -m pytest -q                    # offline test suite
```

Configuration lives in `config/settings.yaml`. Secrets go only in the
git-ignored `config/settings.local.yaml` or in environment variables. See
`CLAUDE.md` for the architecture, the strategy contract and Roostoo API notes.

## Layout

| Path | Role |
|---|---|
| `signals/` | strategies (pure): `base.py` contract, `example_noop.py`, `xs_momentum.py` (live: 14d/3d cross-sectional momentum, long-short) |
| `engine/` | loop, rebalance planner, recorder, recovery, benchmarks, factory |
| `execution/` | Roostoo client (signing, rate limit) + broker adapter |
| `data/` | Binance klines, Roostoo tickers/pair rules |
| `risk/` | weight caps, order sizing, buying-power checks |
| `store/` | SQLite schema, repositories, Alembic migrations |
| `scripts/` | entrypoints: `run_all`, `run_trader`, `check_connection`, `test_notification` |
