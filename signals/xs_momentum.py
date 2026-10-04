"""Short-term cross-sectional momentum, long-short (research_notebook/
Roostoo_ShortTerm_Momentum.ipynb).

Each day t, every eligible coin is ranked by its `formation_days` return
C_t / C_{t-F} - 1 on spot closes (what Roostoo prices off). The top `quantile`
are bought and the bottom `quantile` shorted, equal-weight within each side.
Positions are held `holding_days` as overlapping tranches: the live book is
the mean of the last H daily selections, so 1/H of the book turns over per day.

Eligibility at t is point-in-time, as in the notebook: a close at t, a close
F days earlier, and trailing dollar volume (median quote volume over the
`adv_window` days ending t-1, at least `adv_min_days` of them) at or above the
floor. A day with fewer than `min_eligible` coins (or fewer than 2k) contributes
a flat tranche.

Liquidity is screened on Binance USDS-M perp volume (`ctx.volume_bars`, set
`strategy.volume_source: perp`), as the research was. Perp volume is ~5x spot,
so if it is unavailable (fapi geo-blocked or down) the screen falls back to
spot volume against the rescaled `adv_floor_usd_spot` and logs a warning.

The tranches are rebuilt from bars every cycle, so the strategy is stateless
and resumes correctly after a restart. Expects 1d bars and a daily rebalance;
`lookback_bars` must cover formation + holding + adv_window days.
"""
from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from statistics import median
from typing import Dict, List, Optional

from core.models import Bar
from signals.base import Strategy, StrategyContext

log = logging.getLogger(__name__)

DEFAULTS = dict(
    formation_days=14,
    holding_days=3,
    quantile=0.20,
    adv_floor_usd=20e6,       # on perp volume (the research's screen)
    adv_floor_usd_spot=4e6,   # fallback on spot volume: 20m / ~4.8x perp:spot
    adv_window=30,
    adv_min_days=10,
    min_eligible=6,
    long_gross=0.5,           # fraction of equity on the long side
    short_gross=0.5,          # fraction of equity on the short side
    min_bars_coverage=0.5,    # below this share of pairs with data, skip / fall back
)

Panel = Dict[str, Dict[date, Bar]]


def _panel(bars: Dict[str, List[Bar]]) -> Panel:
    return {p: {b.time.date(): b for b in bs} for p, bs in bars.items() if bs}


def _dollar_volume(b: Bar) -> float:
    return b.quote_volume or b.close * b.volume


def select(prices: Panel, volumes: Panel, t: date, formation: int, q: float,
           adv_floor: float, adv_window: int, adv_min_days: int,
           min_eligible: int) -> Dict[str, float]:
    """One day's tranche: {pair: +1/k | -1/k}, empty if the day is not
    tradeable. Mirrors the notebook's `select` (rank ties broken by pair
    order, k = max(2, floor(n*q)))."""
    sig: Dict[str, float] = {}
    past = t - timedelta(days=formation)
    adv_days = [t - timedelta(days=i) for i in range(1, adv_window + 1)]
    for pair, by_day in prices.items():
        now, then = by_day.get(t), by_day.get(past)
        if now is None or then is None or then.close <= 0:
            continue
        vol = volumes.get(pair, {})
        dv = [_dollar_volume(vol[d]) for d in adv_days if d in vol]
        if len(dv) < adv_min_days or median(dv) < adv_floor:
            continue
        sig[pair] = now.close / then.close - 1.0

    n = len(sig)
    k = max(2, math.floor(n * q))
    if n < 2 * k or n < min_eligible:
        return {}
    winners = sorted(sig, key=lambda p: sig[p], reverse=True)[:k]   # stable on ties
    losers = sorted(sig, key=lambda p: sig[p])[:k]
    out = {p: 1.0 / k for p in winners}
    for p in losers:
        out[p] = out.get(p, 0.0) - 1.0 / k
    return out


class CrossSectionalMomentum(Strategy):
    name = "xs_momentum"

    def __init__(self, params: Optional[dict] = None):
        super().__init__(params)
        unknown = set(self.params) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"unknown xs_momentum params: {sorted(unknown)}")
        self.cfg = {**DEFAULTS, **self.params}

    def target_weights(self, ctx: StrategyContext) -> Optional[Dict[str, float]]:
        c = self.cfg
        need = c["min_bars_coverage"] * max(1, len(ctx.pairs))
        prices = _panel(ctx.bars)
        if len(prices) < need:
            log.warning("[%s] bars for only %d/%d pairs — holding book",
                        self.name, len(prices), len(ctx.pairs))
            return None

        perp = _panel(ctx.volume_bars)
        if ctx.volume_bars and len(perp) >= need:
            volumes, floor, source = perp, c["adv_floor_usd"], "perp"
            prices = {p: b for p, b in prices.items() if p in perp}
        else:
            if ctx.volume_bars:
                log.warning("[%s] perp volume for only %d/%d pairs — screening on "
                            "spot volume at $%.0fm", self.name, len(perp),
                            len(ctx.pairs), c["adv_floor_usd_spot"] / 1e6)
            volumes, floor, source = prices, c["adv_floor_usd_spot"], "spot"
        last = max(max(d) for d in prices.values())

        H = int(c["holding_days"])
        tranches = [select(prices, volumes, last - timedelta(days=i),
                           int(c["formation_days"]), float(c["quantile"]), float(floor),
                           int(c["adv_window"]), int(c["adv_min_days"]),
                           int(c["min_eligible"]))
                    for i in range(H)]

        weights: Dict[str, float] = {}
        for tr in tranches:
            for p, w in tr.items():
                side = c["long_gross"] if w > 0 else c["short_gross"]
                weights[p] = weights.get(p, 0.0) + w * side / H

        # A pair whose data failed to load is unknown, not flat: keep what we
        # hold rather than trade it out on a fetch error.
        for p in ctx.pairs:
            if p not in prices and ctx.weights.get(p):
                weights[p] = ctx.weights[p]
                log.warning("[%s] %s: missing bars — keeping current weight %+.4f",
                            self.name, p, ctx.weights[p])

        weights = {p: w for p, w in weights.items() if abs(w) > 1e-12}
        longs = sorted((p for p, w in weights.items() if w > 0), key=lambda p: -weights[p])
        shorts = sorted((p for p, w in weights.items() if w < 0), key=lambda p: weights[p])
        log.info("[%s] signal date %s | %s volume >= $%.0fm | tranches %s | "
                 "long %d (%.3f) short %d (%.3f) | long=%s short=%s", self.name, last,
                 source, floor / 1e6, [len(t) for t in tranches], len(longs),
                 sum(weights[p] for p in longs), len(shorts),
                 sum(weights[p] for p in shorts), longs, shorts)
        return weights
