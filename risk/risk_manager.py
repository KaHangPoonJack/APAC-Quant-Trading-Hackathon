"""Portfolio-level risk: clamp target weights, then gate each order.

Two stages, both pure:
  1. `clamp_weights` — before any order exists: drop pairs we can't trade, drop
     shorts when disabled, cap |w| per pair, then scale the whole book down to
     the gross and net exposure caps (scaling keeps the strategy's relative
     weights instead of truncating whichever pair happened to come last).
  2. `check` — per order: exposure-REDUCING orders (SELL, SHORT_CLOSE) are always
     approved; exposure-ADDING orders (BUY, SHORT_OPEN) need free USD after what
     earlier orders in the same cycle already reserved.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from core.config import RiskConfig
from core.models import AccountSnapshot, OrderRequest

log = logging.getLogger(__name__)

# Headroom for fees (Roostoo shorts charge 0.1% up front; spot ~0.01-0.1%).
FEE_BUFFER = 0.002


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str = ""


class RiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg

    def clamp_weights(self, weights: Dict[str, float],
                      tradable: Optional[Iterable[str]] = None
                      ) -> Tuple[Dict[str, float], List[str]]:
        """Return (clamped weights, notes explaining each adjustment)."""
        notes: List[str] = []
        allowed = set(tradable) if tradable is not None else None
        out: Dict[str, float] = {}
        for pair, w in weights.items():
            try:
                w = float(w)
            except (TypeError, ValueError):
                notes.append(f"{pair}: non-numeric weight dropped")
                continue
            if w != w:                                   # NaN
                notes.append(f"{pair}: NaN weight dropped")
                continue
            if allowed is not None and pair not in allowed:
                notes.append(f"{pair}: not tradable on Roostoo — dropped")
                continue
            if w < 0 and not self.cfg.allow_short:
                notes.append(f"{pair}: short {w:+.3f} -> 0 (allow_short=false)")
                w = 0.0
            cap = self.cfg.max_weight_per_pair
            if abs(w) > cap:
                notes.append(f"{pair}: |{w:+.3f}| capped to {cap}")
                w = cap if w > 0 else -cap
            out[pair] = w

        gross = sum(abs(w) for w in out.values())
        gross_cap = self.cfg.max_gross_exposure * (1.0 - self.cfg.cash_buffer_pct)
        if gross_cap > 0 and gross > gross_cap:
            k = gross_cap / gross
            out = {p: w * k for p, w in out.items()}
            notes.append(f"gross {gross:.3f} scaled to {gross_cap:.3f}")
        net = sum(out.values())
        if self.cfg.max_net_exposure > 0 and abs(net) > self.cfg.max_net_exposure:
            k = self.cfg.max_net_exposure / abs(net)
            out = {p: w * k for p, w in out.items()}
            notes.append(f"net {net:+.3f} scaled to {self.cfg.max_net_exposure:.3f}")
        return out, notes

    def check(self, req: OrderRequest, account: AccountSnapshot,
              reserved_usd: float = 0.0) -> RiskDecision:
        if not req.side.adds_exposure:
            return RiskDecision(True, "exposure-reducing order approved")
        need = req.notional * (1.0 + FEE_BUFFER)
        available = account.buying_power - reserved_usd
        if need > available:
            return RiskDecision(
                False, f"insufficient free USD: need {need:,.2f}, have {available:,.2f}"
                       f" ({reserved_usd:,.2f} reserved this cycle)")
        return RiskDecision(True, "approved")
