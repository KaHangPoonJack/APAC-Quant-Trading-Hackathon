"""Strategy interface. Every alpha implements `target_weights`.

The contract (the engine does everything else):

  * Input — a `StrategyContext`: closed Binance bars per configured pair,
    Roostoo tickers (executable bid/ask), current equity, current signed
    weights, and the UTC time.
  * Output — the FULL target book as {pair: signed weight of equity}:
      +0.25 = hold 25% of equity long in that pair (spot)
      -0.10 = hold 10% of equity short (/v6 short; needs risk.allow_short)
       0 or omitted = flat in that pair
    Pairs outside `strategy.pairs` are ignored. Return `None` to mean
    "no opinion this cycle — leave the book as it is".
  * The engine clamps the weights to the risk caps, diffs them against what is
    actually held, and sends only the delta orders (sells before buys).

Strategies must stay pure: never call the broker, never do I/O. All state they
need across cycles lives on the instance; anything that must survive a restart
should be re-derivable from bars (or written by the strategy's own runner).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from core.models import Bar, Position, Ticker


@dataclass(frozen=True)
class StrategyContext:
    now: datetime                                   # UTC
    pairs: List[str]                                # configured universe
    bars: Dict[str, List[Bar]]                      # pair -> closed bars, oldest first
    tickers: Dict[str, Ticker]                      # pair -> executable prices
    equity: float                                   # USD
    weights: Dict[str, float] = field(default_factory=dict)  # current signed weights
    # pair -> Binance perp bars, for liquidity screens; only filled when
    # strategy.volume_source is "perp", and empty for a pair whose fetch failed
    volume_bars: Dict[str, List[Bar]] = field(default_factory=dict)


class Strategy(ABC):
    """Base class for all strategies."""

    name: str = "strategy"

    def __init__(self, params: Optional[dict] = None):
        self.params = dict(params or {})

    @abstractmethod
    def target_weights(self, ctx: StrategyContext) -> Optional[Dict[str, float]]:
        """Return the desired book, or None for "no change"."""
        raise NotImplementedError

    def on_recover(self, legs: List[Position]) -> None:
        """Called once at startup with what the account actually holds, so a
        stateful strategy can resume instead of starting from flat. Optional."""
