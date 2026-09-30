import math
import random

import pytest

from tidewise.strategy.ewmac import (
    FORECAST_CAP,
    EwmacParams,
    EwmacState,
    buffered_target,
    optimal_position,
)

FAST_PARAMS = EwmacParams(4, 16, 10)


def _feed(prices, params=FAST_PARAMS):
    s = EwmacState(params)
    out = [s.update(p) for p in prices]
    return s, out


def test_not_ready_before_min_bars():
    _, out = _feed([100 + i for i in range(31)])
    assert all(f is None for f in out[:31])


def test_uptrend_positive_downtrend_negative():
    rng = random.Random(0)
    up = [100 + i + rng.uniform(-0.5, 0.5) for i in range(80)]
    down = [200 - i + rng.uniform(-0.5, 0.5) for i in range(80)]
    assert _feed(up)[1][-1] > 0
    assert _feed(down)[1][-1] < 0


def test_forecast_capped():
    _, out = _feed([100 + 0.01 * i + (0.001 if i % 2 else 0) for i in range(80)])
    assert out[-1] == FORECAST_CAP


def test_flat_prices_never_ready():
    s, out = _feed([100.0] * 80)
    assert out[-1] is None and not s.ready


def test_no_lookahead_incremental_equals_prefix():
    rng = random.Random(1)
    prices = [100.0]
    for _ in range(120):
        prices.append(prices[-1] + rng.gauss(0, 1))
    _, full = _feed(prices)
    for k in (40, 70, 100):
        _, part = _feed(prices[:k])
        assert part == full[:k]


def test_params_validation():
    with pytest.raises(ValueError):
        EwmacParams(64, 16)
    with pytest.raises(ValueError, match="forecast_scalar"):
        EwmacParams(10, 40)
    assert EwmacParams(10, 40, forecast_scalar=4.0).scalar == 4.0


def test_optimal_position_formula():
    # 预测 10、日波动 40 点、乘数 10 → 年化每手 6400；权益 30 万 × 4% = 12000 → 1.875 手
    assert optimal_position(10, 40, 10, 300_000, 0.04) == pytest.approx(1.875)
    assert optimal_position(-20, 40, 10, 300_000, 0.04) == pytest.approx(-3.75)
    assert optimal_position(10, 0, 10, 300_000, 0.04) == 0.0
    assert optimal_position(10, math.inf, 10, 300_000, 0.04) == 0.0


@pytest.mark.parametrize(
    ("optimal", "current", "expected"),
    [
        (5.2, 5, 5),  # 在缓冲区内不动
        (5.2, 2, 5),  # 低于下沿 → 调到下沿 4.7 → 5
        (5.2, 9, 6),  # 高于上沿 → 调到上沿 5.7 → 6
        (-3.0, 0, -2),  # 下沿 -3.5，上沿 -2.5 → 0 在上方 → -2.5 → -2（银行家舍入）
        (0.1, 0, 0),
    ],
)
def test_buffered_target(optimal, current, expected):
    assert buffered_target(optimal, current, avg_position=5.0, buffer_fraction=0.1) == expected
