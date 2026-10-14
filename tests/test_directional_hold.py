"""DirectionalHold: enter the fixed book, never trim, freeze, honour the deadline."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.config import DEFAULT_CONFIG, load_config
from risk.risk_manager import RiskManager
from signals.base import StrategyContext
from signals.directional_hold import DirectionalHold

BOOK = {"VIRTUAL/USD": 0.198, "NEAR/USD": 0.198, "XLM/USD": 0.198,
        "PENGU/USD": 0.198, "UNI/USD": -0.198}
DEADLINE = "2026-10-15T01:00:00+00:00"
BEFORE = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
AFTER = datetime(2026, 10, 15, 1, 0, tzinfo=timezone.utc)


def strat(**kw):
    return DirectionalHold({"book": BOOK, "entry_until": DEADLINE, **kw})


def ctx(weights, now=BEFORE):
    return StrategyContext(now=now, pairs=list(BOOK) + ["ARB/USD", "ZEC/USD"], bars={},
                           tickers={}, equity=97_000.0, weights=dict(weights))


def test_switch_from_momentum_book_targets_the_five_legs_and_drops_the_rest():
    # Momentum leftovers: NEAR already long, PENGU short (wrong side), others to close.
    held = {"NEAR/USD": 0.0625, "PENGU/USD": -0.05, "ARB/USD": -0.06, "ZEC/USD": -0.06}
    out = strat().target_weights(ctx(held))
    assert out == BOOK                                   # ARB/ZEC omitted -> engine closes


def test_filled_leg_is_held_at_its_current_weight_never_trimmed():
    held = {**BOOK, "NEAR/USD": 0.23, "UNI/USD": -0.19, "ARB/USD": -0.05}
    out = strat().target_weights(ctx(held))
    assert out["NEAR/USD"] == 0.23                       # above target: not trimmed
    assert out["UNI/USD"] == -0.19                       # 96% of target: filled, held


def test_leg_below_fill_ratio_is_topped_up():
    held = {**BOOK, "XLM/USD": 0.15}                     # 76% of target (scaled entry)
    out = strat().target_weights(ctx(held))
    assert out["XLM/USD"] == 0.198


def test_freezes_once_filled_and_never_trades_again():
    s = strat()
    assert s.target_weights(ctx({**BOOK, "XLM/USD": 0.195})) is None
    assert s.frozen
    # later the book drifts far off target — still no trades
    assert s.target_weights(ctx({**BOOK, "NEAR/USD": 0.05, "UNI/USD": -0.4})) is None


def test_dust_outside_the_book_does_not_block_the_freeze():
    s = strat()
    assert s.target_weights(ctx({**BOOK, "ARB/USD": -0.0004})) is None


def test_after_the_deadline_it_never_trades_even_after_a_restart():
    fresh = strat()                                      # restart: in-memory freeze lost
    assert fresh.target_weights(ctx({"NEAR/USD": 0.05}, now=AFTER)) is None


def test_bad_params_are_rejected():
    with pytest.raises(ValueError):
        DirectionalHold({"book": BOOK})                  # no deadline
    with pytest.raises(ValueError):
        DirectionalHold({"book": {}, "entry_until": DEADLINE})
    with pytest.raises(ValueError):
        DirectionalHold({"book": BOOK, "entry_until": DEADLINE, "typo": 1})


def test_live_config_runs_directional_hold_within_the_risk_caps():
    cfg = load_config()
    assert cfg.strategy.name == "directional_hold"
    s = DirectionalHold(cfg.strategy.params)
    assert s.book == BOOK and s.entry_until == AFTER
    assert set(BOOK) <= set(cfg.strategy.pairs) and len(cfg.strategy.pairs) == 64
    clamped, notes = RiskManager(cfg.risk).clamp_weights(BOOK)
    assert clamped == pytest.approx(BOOK) and notes == []   # 99% gross: no scaling
    assert cfg.allocations["directional_hold"] == 100_000


def test_momentum_config_is_preserved_and_switchable(tmp_path):
    src = DEFAULT_CONFIG.read_text(encoding="utf-8")
    mom = (DEFAULT_CONFIG.parent / "strategies" / "xs_momentum.yaml").resolve()
    assert "strategy_file: strategies/directional_hold.yaml" in src
    alt = tmp_path / "settings.yaml"
    alt.write_text(src.replace("strategy_file: strategies/directional_hold.yaml",
                               f"strategy_file: {mom.as_posix()}"), encoding="utf-8")
    cfg = load_config(alt)
    assert cfg.strategy.name == "xs_momentum"
    assert cfg.strategy.volume_source == "perp" and cfg.strategy.lookback_bars == 60
    assert cfg.strategy.params == {"formation_days": 14, "holding_days": 3,
                                   "quantile": 0.20, "adv_floor_usd": 20000000,
                                   "adv_floor_usd_spot": 4000000,
                                   "long_gross": 0.5, "short_gross": 0.5}
