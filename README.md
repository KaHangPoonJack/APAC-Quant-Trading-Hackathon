# Roostoo Quant Trading Hackathon — autonomous trading bot

An autonomous bot that trades the **Roostoo** competition account through its
REST API (spot and `/v6` shorts), using **Binance** public market data for
research and signals. It ran unattended on AWS EC2 (Sydney) for the whole
competition: no manual orders, no manual overrides.

The team ran **two strategies**, one after the other. Both live in this repo,
along with the research and the full commit history of every change.

| Period (UTC) | Strategy | Type | Code | Config |
|---|---|---|---|---|
| 2026-10-04 → 2026-10-14 | **Short-term cross-sectional momentum** | Systematic, market-neutral long-short | [`signals/xs_momentum.py`](signals/xs_momentum.py) | [`config/strategies/xs_momentum.yaml`](config/strategies/xs_momentum.yaml) |
| 2026-10-14 → end | **Directional hold** | Discretionary, concentrated 5-coin book | [`signals/directional_hold.py`](signals/directional_hold.py) | [`config/strategies/directional_hold.yaml`](config/strategies/directional_hold.yaml) |

---

## 1. Strategy 1 — Short-term cross-sectional momentum (`xs_momentum`)

### Hypothesis
Among the coins Roostoo lists, the biggest gainers over the past two weeks keep
outperforming the biggest losers over the next few days. Buying winners and
shorting losers in equal size captures that spread while cancelling out the
direction of the overall crypto market.

### Research
The full study is [`research_notebook/Roostoo_ShortTerm_Momentum.ipynb`](research_notebook/Roostoo_ShortTerm_Momentum.ipynb),
with all outputs saved.

**Design (pre-registered before looking at results):**
- **Universe:** every crypto pair Roostoo lists, mapped to Binance USDⓈ-M
  perpetuals for price, volume and funding history (2020-01 → 2026-10).
  Tokenised stocks are excluded because they trade on an exchange calendar.
- **Grid:** formation window F ∈ {1, 3, 7, 14} days × holding window
  H ∈ {1, 3, 7, 14} days.
- **Selection:** top and bottom **quintile**, equal-weight within each side.
  A quintile rather than a fixed count, because the universe grows from 10 to
  66 coins over the sample.
- **Execution:** the signal uses the close of day t; the position is live from
  t+1. Positions are held as H overlapping daily tranches, so 1/H of the book
  turns over each day.
- **Costs:** 5 bp per side, plus realised funding charged by position.
- **Controls:**
  - a point-in-time liquidity screen (median dollar volume through t−1);
  - an equal-weight-universe benchmark, since crypto rose about 1,400% over
    the sample;
  - a random-portfolio null with the same names, holding period and costs;
  - a capacity estimate.
- **Primary sample:** 2023-01 → 2026-10, when at least 30 coins are live.
  The full 2020+ history is reported only as a robustness check.

**Results (primary sample, long-short, net Sharpe):**

| Formation \ Holding | H=1 | H=3 | H=7 | H=14 |
|---|---|---|---|---|
| F=1 | 0.08 | 0.94 | 1.12 | 1.48 |
| F=3 | 1.07 | 1.00 | 0.91 | 1.19 |
| F=7 | 0.94 | 0.97 | 0.83 | 1.01 |
| **F=14** | 1.34 | **1.06** | 0.83 | 0.72 |

- **Market-neutral:** beta to the equal-weight universe is between −0.09 and
  +0.01 in every cell.
- **Beats the random null:** all 8 cells tested beat their random-portfolio
  null at p < 0.05, most at p = 0.000.
- **Liquidity screen helps:** a $20m daily-volume floor *raises* the F14/H3
  Sharpe from 1.06 to 1.28.
- **Capacity:** about $61–72m per rebalance at 1% participation.
- **The long leg is mostly the market:** beta 1.01. The short "losers keep
  losing" leg only works with formation windows of about a week or longer.
  That's why the strategy is run long-short rather than long-only.

### Configuration traded, and why
**F = 14 days, H = 3 days, top/bottom quintile, $20m perp-volume floor.**
- This sits inside the notebook's recommended region (F 7–14, H 1–3).
- H = 3 was chosen over H = 1 because it trades a third as much. The notebook
  assumed 5 bp per trade, but Roostoo charges 10 bp to open and 10 bp to close
  a short.
- F = 14 is where the short leg's edge is clearest.

### Implementation
[`signals/xs_momentum.py`](signals/xs_momentum.py) is a direct port of the
notebook's `select` function. Every day, just after 00:00 UTC, it:
1. ranks the eligible coins by their 14-day return;
2. builds the tranches for the last 3 days and averages them;
3. outputs target weights.

**Sizing:** each side gets 50% of equity (`long_gross` / `short_gross` = 0.5).
Roostoo makes every long cost its full value in cash and every short lock its
full value as collateral, so long + short can't exceed about 100% of equity.
The risk layer caps the total at 99% and keeps 1% in cash. This is **half** the
notebook's book (100% long + 100% short), so live returns and volatility should
be about half the notebook's figures.

**Design choices:**
- The strategy is **stateless**: it rebuilds its tranches from price bars every
  cycle, so a restart resumes correctly.
- If Binance's futures API is unreachable, the liquidity screen falls back to
  spot volume against a floor rescaled to $4m (perp volume is about 5× spot).

### Limitations (stated in the notebook, plus one later check)
- **Survivorship:** the universe is Roostoo's list *today*, so coins that
  collapsed and were delisted are missing. This flatters both legs, especially
  the short leg.
- **One regime:** 2023–2026 is mostly a bull market, and the study is entirely
  in-sample, with no held-out period.
- **Post-hoc check (not in the notebook):** we rebuilt the universe
  point-in-time from *all* Binance USDⓈ-M perps, including 129 that were later
  delisted, and kept the 60 most liquid each day. Sharpe fell to about 0.6–0.8
  (t ≈ 1.1–1.5), which is **not statistically significant**. The long leg's
  edge mostly disappeared; the short leg's held up. We treat the momentum edge
  as real but weak.

### Live record (2026-10-04 → 2026-10-14)
Account equity was about **−3%** when the team switched strategies. Two engine
issues were found while it was live. Both were fixed live, each with its own
commit and a regression test:

1. **Equity double-counting** (commit `55542a4`, live 2026-10-08 UTC):
   - Roostoo counts short collateral both as locked USD and inside each
     short's position value.
   - The bot counted it twice, so it believed equity was about $137k when the
     real figure was about $98k.
   - As a result it sized every trade too large and drifted to about 20% net
     long.
   - The fix counts the collateral once. It was confirmed against Roostoo's
     own balance to the cent.
2. **Lopsided entries when cash ran short** (commit `dc994ef`, live 2026-10-10 UTC):
   - Profits on shorts stay locked in Roostoo until each short is closed.
   - When cash then ran out mid-rebalance, entries were skipped in
     alphabetical order.
   - The short book happened to be mostly late in the alphabet, so the
     portfolio sat at about +5% net long.
   - Now every entry is scaled down by the same factor instead
     (`engine/rebalance.scale_entries`).

---

## 2. Strategy 2 — Directional hold (`directional_hold`)

### Why the team switched
After 10 days the market-neutral book was down about 3%, its expected edge was
small (see Limitations above), and little time was left. In a ranking
competition, a low-volatility strategy that's slightly down has very little
chance of placing. The team chose deliberately to trade the momentum book's
steady, low-volatility profile for a **concentrated directional bet**, which has
more variance and a real chance of a top finish.

### The book
The coins and directions are the team's **discretionary conviction**: a
judgement call on each coin, not a systematic signal.

| Coin | Side | Weight of equity |
|---|---|---|
| VIRTUAL | Long | 19.8% |
| NEAR | Long | 19.8% |
| XLM | Long | 19.8% |
| PENGU | Long | 19.8% |
| UNI | Short | 19.8% |

- **Total:** 99% of equity invested, 1% cash, **59.4% net long**.
- **Why 19.8% and not 24.5%:** the team first wanted 24.5% per coin, which
  totals 122.5% of equity. Roostoo's full-collateral shorts make that
  impossible, so all five were scaled equally to fit 99%.

### Mechanics
The book is entered once and then held untouched. The competition liquidates
every account at the end, so there's no exit logic.

1. **Entry window** (until `entry_until` = 2026-10-15 01:00 UTC):
   - Any coin outside the book is closed. That covers all the remaining
     momentum positions.
   - Any book position below 95% of its target, or on the wrong side, is
     brought to target. For example, PENGU and XLM were shorts in the momentum
     book and were flipped to longs.
   - A position already at or above 95% of target is held exactly as it is,
     never trimmed.
2. **Freeze:** once every position is filled and nothing else is held, the
   strategy returns "no change" for good.
3. **Hard deadline:** after `entry_until`, the strategy never trades again,
   even if the bot restarts. The deadline lives in config, so unlike an
   in-memory flag it survives a restart.

**Risk, accepted knowingly:**
- **No stop-loss or rebalancing.** Position weights drift with prices.
- **About 59% net long,** so a 10% move across the four longs is roughly a 6%
  move in equity.
- **The UNI short's loss is capped at its collateral,** because Roostoo never
  asks for more.
- **There's no backtest.** This is a discretionary view, not a researched
  signal.

---

## 3. The trading engine (shared by both strategies)

```
Binance klines ─► strategy (pure) ─► risk caps ─► rebalance planner ─► Roostoo
                  target weights      per-pair /    target − holdings    REST
                                      total / net   = delta orders
```

- **Strategies are pure functions:** bars and current holdings go in, target
  weights come out ([`signals/base.py`](signals/base.py)). The engine handles
  everything else.
- **Stateless reconciliation:** every rebalance re-compares targets with real
  holdings. Missed fills, cancelled orders and restarts all correct themselves.
- **Order of operations:** exits are sent before entries, so the cash they free
  funds the entries. If cash still falls short, all entries are scaled
  together.
- **Within the competition rules:**
  - **Rate limit:** one shared limiter keeps every Roostoo call, including
    retries, under the **30 calls/minute** limit.
  - **No manual intervention:** a supervisor ([`scripts/run_all.py`](scripts/run_all.py))
    restarts crashed processes and detects a hung loop through heartbeats.
  - **Monitoring:** Telegram alerts and an hourly equity/holdings report.
- **Audit trail:** every order, fill, round-trip trade and hourly equity
  snapshot is stored in SQLite.
- **Tests:** 187 offline tests (`python -m pytest -q`) cover the
  strategies, risk, the rebalance planner, the Roostoo request signing, the
  broker accounting and the engine loop.

### Switching strategies
The active strategy is chosen by one line in
[`config/settings.yaml`](config/settings.yaml):

```yaml
strategy_file: strategies/directional_hold.yaml   # or strategies/xs_momentum.yaml
```

### Run it

```bash
pip install -r requirements.txt
cp config/settings.local.example.yaml config/settings.local.yaml   # add API keys (git-ignored)
python -m scripts.check_connection     # read-only preflight: connectivity, keys, equity breakdown
python -m scripts.run_all              # run the trader under the supervisor
python -m pytest -q                    # offline test suite (no network)
```

API keys go only in the git-ignored `config/settings.local.yaml` or in
environment variables. [`CLAUDE.md`](CLAUDE.md) has the detailed architecture
and the Roostoo API notes.

### Layout

| Path | Role |
|---|---|
| `signals/` | strategies: `base.py` contract, `xs_momentum.py`, `directional_hold.py`, `example_noop.py` |
| `config/` | `settings.yaml` (engine, risk, active strategy) and `strategies/*.yaml` (one file per strategy) |
| `research_notebook/` | the momentum research notebook |
| `engine/` | loop, rebalance planner, recorder, recovery, benchmarks, factory |
| `execution/` | Roostoo client (signing, rate limit) and broker adapter (equity, orders, fills) |
| `data/` | Binance klines; Roostoo tickers and pair rules |
| `risk/` | weight caps, order sizing, free-cash checks |
| `store/` | SQLite schema, repositories, Alembic migrations |
| `scripts/` | entry points: `run_all`, `run_trader`, `check_connection`, `test_notification` |
| `tests/` | offline test suite |

### Change history (strategy-relevant commits)

| Commit | Date | Change |
|---|---|---|
| `3478e1f` | 2026-10-04 | Initial infrastructure + `xs_momentum` goes live |
| `55542a4` | 2026-10-09 | Fix equity double-counting short collateral |
| `dc994ef` | 2026-10-10 | Scale entries evenly when cash is short |
| `e259150` | 2026-10-14 | Switch to `directional_hold`; momentum config kept in `config/strategies/` |
