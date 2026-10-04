"""Periodic Telegram status: equity and holdings (pure formatting).

Built from the portfolio snapshot the engine already refreshed this poll, so a
report costs no Roostoo calls. Roostoo reports no entry price for spot
holdings, so positions are shown by value and weight, and performance by
equity change.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from core.models import AccountSnapshot, Position, coin_of


def _usd(v: float) -> str:
    return f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"


def _chg(now: float, then: Optional[float]) -> str:
    if not then:
        return "n/a"
    d = now - then
    return f"{'+' if d >= 0 else '-'}${abs(d):,.0f} ({d / then * 100:+.2f}%)"


def format_status(now: datetime, strategy: str, env: str, account: AccountSnapshot,
                  legs: List[Position], start_equity: Optional[float],
                  prev_equity: Optional[float]) -> str:
    eq = account.total_assets
    longs = sorted((p for p in legs if p.market_value > 0), key=lambda p: -p.market_value)
    shorts = sorted((p for p in legs if p.market_value < 0), key=lambda p: p.market_value)
    lv = sum(p.market_value for p in longs)
    sv = sum(p.market_value for p in shorts)

    lines = [
        f"📊 {strategy} · {env} · {now:%Y-%m-%d %H:%M} UTC",
        f"Equity {_usd(eq)}",
        f"  since start {_chg(eq, start_equity)}",
        f"  since last report {_chg(eq, prev_equity)}",
        f"Cash {_usd(account.cash)} | Long {_usd(lv)} ({len(longs)}) | "
        f"Short {_usd(sv)} ({len(shorts)})",
    ]
    if eq > 0:
        lines.append(f"Gross {(lv - sv) / eq * 100:.1f}% | Net {(lv + sv) / eq * 100:+.1f}%")
    for title, side in (("Longs", longs), ("Shorts", shorts)):
        if side:
            lines.append(f"\n{title}:")
            lines += [f"  {coin_of(p.code):<10} {_usd(p.market_value):>9}  "
                      f"{(p.market_value / eq * 100 if eq > 0 else 0):+5.1f}%" for p in side]
    if not legs:
        lines.append("\nNo open positions.")
    return "\n".join(lines)
