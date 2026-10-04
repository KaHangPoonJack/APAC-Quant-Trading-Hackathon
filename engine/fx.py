"""FX rate provider.

The recorder asks for a `rate(base, quote)` each hour, right before writing an
equity snapshot, and persists what it gets so valuations stay reproducible.

`ConfigFxProvider` reads rates from config (works offline, fully testable). A
live provider (e.g. USDT/USD) can be dropped in later behind the same interface without
touching the recorder.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Dict

log = logging.getLogger(__name__)


class FxProvider(ABC):
    @abstractmethod
    def rate(self, base: str, quote: str) -> float:
        """Units of `quote` per 1 unit of `base` (rate(USDT,USD) ≈ 1.0)."""
        raise NotImplementedError


class ConfigFxProvider(FxProvider):
    """Static rates from config. Same-currency pairs return 1.0; the inverse of a
    known pair is derived automatically."""

    def __init__(self, rates: Dict[str, float]):
        # keys like "USDT/USD" -> 1.0
        self._rates = {k.upper(): float(v) for k, v in rates.items()}

    def rate(self, base: str, quote: str) -> float:
        base, quote = base.upper(), quote.upper()
        if base == quote:
            return 1.0
        key = f"{base}/{quote}"
        if key in self._rates:
            return self._rates[key]
        inv = f"{quote}/{base}"
        if inv in self._rates and self._rates[inv] != 0:
            return 1.0 / self._rates[inv]
        raise KeyError(
            f"no FX rate configured for {base}->{quote} (add '{key}' to config fx.rates)"
        )
