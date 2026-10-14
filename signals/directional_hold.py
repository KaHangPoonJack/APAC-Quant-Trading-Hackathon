"""Directional buy-and-hold: enter a fixed signed book once, then freeze.

The team picks the coins and signed weights (`book`). The strategy only gets the
account there and then stops trading for good — the competition liquidates
every account at the end, so there is no exit logic.

Entry phase (until `entry_until`, UTC). Each rebalance returns:
  * a book leg below `fill_ratio` of its target, or on the wrong side, at its
    target weight (topped up / flipped);
  * a book leg already at or above `fill_ratio` of its target at its CURRENT
    weight, so it is held, never trimmed;
  * every other configured pair omitted, so the engine closes it.
As soon as every leg is filled and nothing else is held, the strategy freezes.

Frozen (or past `entry_until`): returns None — "no change" — forever. The
deadline is what keeps a restart from ever re-trading a frozen book: the
in-memory freeze is lost on restart, the deadline in config is not.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, Optional

from signals.base import Strategy, StrategyContext

log = logging.getLogger(__name__)

DEFAULTS = dict(book={}, fill_ratio=0.95, entry_until=None)
# Positions outside the book smaller than this weight are dust that cannot be
# sold under the exchange minimum; they do not block the freeze.
DUST_WEIGHT = 0.001


def _parse_utc(value) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class DirectionalHold(Strategy):
    name = "directional_hold"

    def __init__(self, params: Optional[dict] = None):
        super().__init__(params)
        unknown = set(self.params) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"unknown directional_hold params: {sorted(unknown)}")
        cfg = {**DEFAULTS, **self.params}
        self.book: Dict[str, float] = {str(p).upper(): float(w)
                                       for p, w in (cfg["book"] or {}).items()}
        if not self.book or any(w == 0 for w in self.book.values()):
            raise ValueError("directional_hold needs a non-empty book of non-zero weights")
        if cfg["entry_until"] is None:
            raise ValueError("directional_hold needs entry_until (UTC deadline)")
        self.fill_ratio = float(cfg["fill_ratio"])
        self.entry_until = _parse_utc(cfg["entry_until"])
        self.frozen = False

    def target_weights(self, ctx: StrategyContext) -> Optional[Dict[str, float]]:
        if self.frozen:
            return None
        if ctx.now >= self.entry_until:
            self.frozen = True
            log.info("[%s] entry window ended %s — holding the book untouched",
                     self.name, self.entry_until.isoformat())
            return None

        out: Dict[str, float] = {}
        pending = []
        for pair, target in self.book.items():
            w = ctx.weights.get(pair, 0.0)
            if w * target > 0 and abs(w) >= self.fill_ratio * abs(target):
                out[pair] = w                    # filled: hold, never trim
            else:
                out[pair] = target               # missing / short of target / wrong side
                pending.append(f"{pair} {w:+.3f}->{target:+.3f}")
        others = {p: w for p, w in ctx.weights.items()
                  if p not in self.book and abs(w) >= DUST_WEIGHT}

        if not pending and not others:
            self.frozen = True
            log.info("[%s] book filled %s — frozen, no further trades", self.name,
                     {p: round(w, 4) for p, w in out.items()})
            return None
        log.info("[%s] entry phase until %s | to fill: %s | to close: %s", self.name,
                 self.entry_until.isoformat(), pending or "-", sorted(others) or "-")
        return out
