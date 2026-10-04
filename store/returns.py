"""Return calculations that correctly account for external capital flows."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple


@dataclass(frozen=True)
class WeightedFlow:
    """An external cash flow within a period.

    weight = fraction of the period remaining *after* the flow occurred, i.e.
    (T - t) / T where t is time-into-period. A deposit at the start has weight ~1;
    at the very end, ~0. Sign: deposits positive, withdrawals negative.
    """
    weight: float
    amount: float


def modified_dietz(begin_value: float, end_value: float,
                   flows: List[WeightedFlow]) -> Tuple[float, float]:
    """Return (return_pct, pnl_nominal) for a period using Modified Dietz.

    R = (EMV - BMV - F) / (BMV + Σ wᵢ·Fᵢ)
    where F = Σ Fᵢ. Gains exclude the capital you added/removed.

    Returns pct as a percentage (e.g. 1.5 == +1.5%). Denominator guarded: if it
    is ~0 the period had no capital at risk, so return_pct is 0.0.
    """
    net_flow = sum(f.amount for f in flows)
    pnl_nominal = end_value - begin_value - net_flow
    weighted = begin_value + sum(f.weight * f.amount for f in flows)
    if abs(weighted) < 1e-9:
        return 0.0, pnl_nominal
    return (pnl_nominal / weighted) * 100.0, pnl_nominal
