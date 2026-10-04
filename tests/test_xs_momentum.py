from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from core.models import Bar
from signals.base import StrategyContext
from signals.xs_momentum import CrossSectionalMomentum

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def series(pair, closes, vol_usd=50e6, start=0):
    """Daily bars with constant dollar volume; `start` delays the listing."""
    return [Bar(pair, T0 + timedelta(days=start + i), c, c, c, c, vol_usd / c)
            for i, c in enumerate(closes)]


def ctx(bars, weights=None, volume_bars=None):
    return StrategyContext(now=T0 + timedelta(days=100), pairs=list(bars), bars=bars,
                           tickers={}, equity=50_000.0, weights=weights or {},
                           volume_bars=volume_bars or {})


def trending(n_pairs=10, days=50):
    """Pair i compounds at i% a day, so the ranking is the same every day."""
    return {f"C{i}/USD": series(f"C{i}/USD", [100 * (1 + i / 100) ** d for d in range(days)])
            for i in range(n_pairs)}


def test_quintile_long_top_short_bottom_at_half_gross():
    w = CrossSectionalMomentum().target_weights(ctx(trending(10)))
    # 10 eligible -> k = 2 per side; identical ranking every day -> full tranches
    assert w == pytest.approx({"C9/USD": .25, "C8/USD": .25, "C0/USD": -.25, "C1/USD": -.25})


def test_adv_floor_is_point_in_time():
    bars = trending(10)
    # C9 trades $1m/day except a huge volume on the signal day itself
    thin = series("C9/USD", [b.close for b in bars["C9/USD"]], vol_usd=1e6)
    last = thin[-1]
    thin[-1] = Bar(last.code, last.time, last.close, last.close, last.close, last.close,
                   1e12 / last.close)
    bars["C9/USD"] = thin
    w = CrossSectionalMomentum().target_weights(ctx(bars))
    assert "C9/USD" not in w           # ADV uses data through t-1 only
    assert w["C8/USD"] > 0 and w["C7/USD"] > 0


def test_too_few_eligible_is_flat():
    assert CrossSectionalMomentum().target_weights(ctx(trending(5))) == {}


def test_missing_bars_keep_held_weight_and_low_coverage_skips():
    bars = trending(10)
    bars["X/USD"] = []
    w = CrossSectionalMomentum().target_weights(ctx(bars, {"X/USD": -0.05}))
    assert w["X/USD"] == -0.05
    sparse = {p: (b if i < 3 else []) for i, (p, b) in enumerate(trending(10).items())}
    assert CrossSectionalMomentum().target_weights(ctx(sparse)) is None


def test_perp_volume_screen_and_spot_fallback():
    bars = trending(10)
    # spot volume $10m everywhere: passes the $4m spot floor, not the $20m one
    bars = {p: series(p, [b.close for b in bs], vol_usd=10e6) for p, bs in bars.items()}
    perp = {p: series(p, [b.close for b in bs], vol_usd=50e6) for p, bs in bars.items()}
    perp["C9/USD"] = series("C9/USD", [b.close for b in bars["C9/USD"]], vol_usd=5e6)
    w = CrossSectionalMomentum().target_weights(ctx(bars, volume_bars=perp))
    assert "C9/USD" not in w and w["C8/USD"] > 0          # screened on perp volume
    # fapi down: every perp fetch empty -> spot volume vs the spot floor
    w = CrossSectionalMomentum().target_weights(ctx(bars, volume_bars={p: [] for p in bars}))
    assert w["C9/USD"] > 0


def test_unknown_param_rejected():
    with pytest.raises(ValueError):
        CrossSectionalMomentum({"formation": 7})


def notebook_target(C, V, formation, holding, q, floor):
    """The notebook's select() + run() target, verbatim logic, unscaled."""
    ADV = V.rolling(30, min_periods=10).median().shift(1)
    sig = C / C.shift(formation) - 1.0
    elig = sig.notna() & ADV.notna() & (ADV >= floor) & C.notna()
    s = sig.where(elig)
    n_elig = elig.sum(axis=1)
    k = np.maximum(2, np.floor(n_elig * q)).astype(int)
    hi_r = s.rank(axis=1, ascending=False, method="first")
    lo_r = s.rank(axis=1, ascending=True, method="first")
    ok = (n_elig >= 2 * k) & (n_elig >= 6)
    win = (hi_r.le(k, axis=0) & ok.values[:, None]).fillna(False)
    los = (lo_r.le(k, axis=0) & ok.values[:, None]).fillna(False)
    sgn = (win.astype(float).div(win.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
           - los.astype(float).div(los.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0))
    return sgn.rolling(holding, min_periods=1).mean().iloc[-1]   # book for the next day


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_matches_notebook_engine(seed):
    rng = np.random.default_rng(seed)
    days, n = 60, 25
    idx = pd.date_range(T0, periods=days, freq="D")
    C = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, .05, (days, n)), axis=0)),
                     index=idx, columns=[f"C{i}/USD" for i in range(n)])
    V = pd.DataFrame(rng.lognormal(np.log(30e6), 1.0, (days, n)), index=idx, columns=C.columns)
    for j, start in enumerate(rng.integers(0, 40, n // 3)):   # staggered listings
        C.iloc[:start, j] = np.nan
        V.iloc[:start, j] = np.nan

    bars = {p: [Bar(p, t.to_pydatetime(), c, c, c, c, 1.0)       # spot: prices only
                for t, c in C[p].dropna().items()] for p in C.columns}
    perp = {p: [Bar(p, t.to_pydatetime(), c, c, c, c, 1.0, quote_volume=V.at[t, p])
                for t, c in C[p].dropna().items()] for p in C.columns}
    got = CrossSectionalMomentum({"long_gross": 1.0, "short_gross": 1.0}).target_weights(
        ctx(bars, volume_bars=perp))
    want = notebook_target(C, V, 14, 3, 0.20, 20e6)
    want = {p: w for p, w in want.items() if abs(w) > 1e-12}
    assert want, "fixture should produce a non-empty book"
    assert got == pytest.approx(want)
