import math

import pytest

from signals.indicators import ema


def test_ema_returns_nan_before_seed():
    out = ema([1, 2, 3, 4, 5], period=3)
    assert math.isnan(out[0]) and math.isnan(out[1])
    # seed = SMA of first 3 = 2.0
    assert out[2] == pytest.approx(2.0)


def test_ema_tracks_upward_series():
    values = list(range(1, 21))
    out = ema(values, period=5)
    # EMA of a monotonically rising series rises and lags below the latest value.
    assert out[-1] < values[-1]
    assert out[-1] > out[-2]


def test_ema_shorter_than_period_is_all_nan():
    out = ema([1, 2], period=5)
    assert all(math.isnan(v) for v in out)


def test_ema_rejects_bad_period():
    with pytest.raises(ValueError):
        ema([1, 2, 3], period=0)
