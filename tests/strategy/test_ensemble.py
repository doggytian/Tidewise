import random

import pytest

from tidewise.strategy.ensemble import (
    CARRY_WEIGHT,
    ENSEMBLE_DIVERSIFIER,
    TREND_SPEEDS,
    TREND_WEIGHT,
    CarryState,
    EwmacEnsembleState,
    combine_forecasts,
)


def test_ensemble_uptrend_positive():
    rng = random.Random(0)
    prices = [100 + 0.3 * i + rng.uniform(-1, 1) for i in range(600)]
    e = EwmacEnsembleState(vol_lookback=25)
    out = [e.update(p) for p in prices]
    assert out[-1] is not None and out[-1] > 0


def test_ensemble_none_before_all_ready():
    e = EwmacEnsembleState()
    # 最慢速度需要 2*256=512 根预热；此前部分速度有预测时 ensemble 只归一合成
    out = [e.update(100 + i * 0.01) for i in range(300)]
    assert any(f is not None for f in out)  # 快速度已就绪即给出合成值


def test_ensemble_no_lookahead():
    rng = random.Random(2)
    prices = [100.0]
    for _ in range(600):
        prices.append(prices[-1] + rng.gauss(0, 1))
    e1, e2 = EwmacEnsembleState(), EwmacEnsembleState()
    full = [e1.update(p) for p in prices]
    part = [e2.update(p) for p in prices[:500]]
    assert part == full[:500]


def test_ensemble_diversifier_applied():
    e = EwmacEnsembleState()
    e.states = [type("S", (), {"update": lambda self, c: 5.0})() for _ in TREND_SPEEDS]
    assert e.update(100) == pytest.approx(5.0 * ENSEMBLE_DIVERSIFIER)


def test_carry_constant_series_gives_none():
    """恒定 carry 的标准差为 0，无法标准化 → None（不发信号）。"""
    c = CarryState()
    out = [c.update(0.05) for _ in range(300)]
    assert out[-1] is None


def test_carry_positive_series_positive_forecast():
    rng = random.Random(3)
    c = CarryState()
    vals = [0.08 + rng.gauss(0, 0.02) for _ in range(400)]
    out = [c.update(v) for v in vals]
    assert out[-1] is not None and out[-1] > 0


def test_combine_weights_and_renormalize():
    f, w = combine_forecasts(10.0, -10.0)
    assert f == pytest.approx(10 * TREND_WEIGHT - 10 * CARRY_WEIGHT)
    assert w == pytest.approx(1.0)
    f2, w2 = combine_forecasts(10.0, None)
    assert f2 == 10.0 and w2 == pytest.approx(TREND_WEIGHT)
    f3, w3 = combine_forecasts(None, None)
    assert f3 is None and w3 == 0.0
