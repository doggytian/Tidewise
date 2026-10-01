import pytest

from tidewise.portfolio import cap_by_margins, vol_scalar


def test_vol_scalar_insufficient_history():
    assert vol_scalar([1, 2, 3], 100_000, 0.1, 25) == 1.0


def test_vol_scalar_scales_down_when_volatile():
    pnls = [1000 * (1 if i % 2 else -1) for i in range(30)]  # 日波动约 1000
    # 目标日波动 = 100000 × 0.1 / 16 = 625 → scalar ≈ 0.625
    s = vol_scalar(pnls, 100_000, 0.1, 25)
    assert s == pytest.approx(625 / 1000, rel=0.05)
    assert 0 < s < 1


def test_vol_scalar_never_amplifies():
    pnls = [10 * (1 if i % 2 else -1) for i in range(30)]  # 波动极小
    assert vol_scalar(pnls, 100_000, 0.1, 25) == 1.0


def test_vol_scalar_zero_vol():
    assert vol_scalar([0.0] * 30, 100_000, 0.1, 25) == 1.0


BASE = dict(
    prices={"a": 100.0, "b": 100.0, "c": 100.0},
    sizes={"a": 10.0, "b": 10.0, "c": 10.0},
    ratios={"a": 0.1, "b": 0.1, "c": 0.1},  # 每手保证金 100
)


def test_cap_instrument():
    # per lot = 100×10×0.1 = 100；上限 0.1×100000 = 10000 → 100 手，20 手不超，不变
    out = cap_by_margins(
        {"a": 20, "b": 3},
        **BASE,
        clusters={"a": "x", "b": "y"},
        equity=100_000,
        instrument_cap=0.1,
        cluster_cap=0.15,
        total_cap=0.4,
    )
    assert out == {"a": 20, "b": 3}


def test_cap_instrument_real():
    # per lot = 1000×10×0.1 = 1000；上限 0.1×100000 = 10000 → 10 手
    out = cap_by_margins(
        {"a": 25},
        prices={"a": 1000.0},
        sizes={"a": 10.0},
        ratios={"a": 0.1},
        clusters={"a": "x"},
        equity=100_000,
        instrument_cap=0.1,
        cluster_cap=0.15,
        total_cap=0.4,
    )
    assert out["a"] == 10


def test_cap_cluster():
    # 同板块两个品种各 8 手：每手保证金 100 → 合计 1600 > 0.15×10000=1500 → 各缩到 7 手
    out = cap_by_margins(
        {"a": 8, "b": 8},
        prices={"a": 100.0, "b": 100.0},
        sizes={"a": 1.0, "b": 1.0},
        ratios={"a": 1.0, "b": 1.0},
        clusters={"a": "x", "b": "x"},
        equity=10_000,
        instrument_cap=0.9,
        cluster_cap=0.15,
        total_cap=0.9,
    )
    assert out["a"] == 7 and out["b"] == 7
    total = (out["a"] + out["b"]) * 100
    assert total <= 1500


def test_cap_total():
    out = cap_by_margins(
        {"a": 30, "b": 30},
        prices={"a": 100.0, "b": 100.0},
        sizes={"a": 1.0, "b": 1.0},
        ratios={"a": 1.0, "b": 1.0},
        clusters={"a": "x", "b": "y"},
        equity=10_000,
        instrument_cap=0.9,
        cluster_cap=0.9,
        total_cap=0.4,
    )
    # 总保证金上限 4000 → 6000 超 → 各缩 2/3 → 20 手
    assert out == {"a": 20, "b": 20}


def test_cap_negative_positions_scaled_symmetrically():
    out = cap_by_margins(
        {"a": -25},
        prices={"a": 1000.0},
        sizes={"a": 10.0},
        ratios={"a": 0.1},
        clusters={"a": "x"},
        equity=100_000,
        instrument_cap=0.1,
        cluster_cap=0.15,
        total_cap=0.4,
    )
    assert out["a"] == -10


def test_empty_cluster_is_own_group():
    out = cap_by_margins(
        {"a": 15, "b": 15},
        prices={"a": 100.0, "b": 100.0},
        sizes={"a": 1.0, "b": 1.0},
        ratios={"a": 1.0, "b": 1.0},
        clusters={"a": "", "b": ""},
        equity=10_000,
        instrument_cap=0.9,
        cluster_cap=0.15,
        total_cap=0.9,
    )
    # 空 cluster 各自成组：每组合计 1500 = 上限，不超
    assert out == {"a": 15, "b": 15}
