"""Technical indicators as pure functions over sequences of floats."""
from __future__ import annotations

from typing import List, Sequence


def ema(values: Sequence[float], period: int) -> List[float]:
    """Exponential moving average.

    Seeded with a simple moving average of the first `period` values so the
    output does not over-weight the very first observation. The returned list is
    the same length as the input; the first `period-1` entries are `float('nan')`
    because the EMA is not yet defined there.

    Raises ValueError if `period` is not a positive int.
    """
    if period <= 0:
        raise ValueError("period must be a positive integer")
    n = len(values)
    out: List[float] = [float("nan")] * n
    if n < period:
        return out

    multiplier = 2.0 / (period + 1)
    # Seed: SMA of the first `period` values.
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = (values[i] - prev) * multiplier + prev
        out[i] = prev
    return out
