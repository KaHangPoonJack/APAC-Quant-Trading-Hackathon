"""Placeholder strategy: never trades.

It exists so the whole pipeline (Binance bars -> strategy -> risk -> Roostoo ->
DB) can be run and smoke-tested before the real alpha exists.
Copy this file to start a new strategy, then register the class in
engine/factory.py STRATEGIES and set `strategy.name` in settings.yaml.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

from signals.base import Strategy, StrategyContext

log = logging.getLogger(__name__)


class NoOpStrategy(Strategy):
    name = "example_noop"

    def target_weights(self, ctx: StrategyContext) -> Optional[Dict[str, float]]:
        last = {p: (b[-1].close if b else None) for p, b in ctx.bars.items()}
        log.info("[%s] equity=%.2f last closes=%s — no-op, holding book",
                 self.name, ctx.equity, last)
        return None
