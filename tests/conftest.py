from __future__ import annotations

from pathlib import Path

import pytest
import yaml

EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "config" / "example.yaml"


@pytest.fixture
def example_config() -> Path:
    return EXAMPLE_CONFIG


@pytest.fixture
def example_raw() -> dict:
    return yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))


@pytest.fixture
def cfg(tmp_path, example_raw):
    """合成 rb 数据的测试配置（回测/报告/一致性测试共用）。"""
    return build_synthetic_cfg(tmp_path, example_raw)


def build_synthetic_cfg(tmp_path: Path, example_raw: dict):
    """合成 rb 数据的测试配置（900 根日线，8 个合约轮换）。"""
    from datetime import date, timedelta

    import numpy as np
    import pandas as pd

    from tidewise.config import parse_config
    from tidewise.data.carry import CarryStore, build_carry
    from tidewise.data.continuous import build_continuous
    from tidewise.data.pipeline import ContinuousStore

    rng = np.random.default_rng(7)
    days = 900
    trend = np.concatenate([np.linspace(0, 600, days // 2), np.linspace(600, 0, days - days // 2)])
    base = 3000 + trend + rng.normal(0, 15, days).cumsum() * 0.3
    start = date(2020, 1, 2)
    dates = [start + timedelta(days=i) for i in range(days)]
    contracts = ["RB2101", "RB2105", "RB2110", "RB2201", "RB2205", "RB2210", "RB2301", "RB2305"]
    rows = []
    for i, d in enumerate(dates):
        seg = min(i // 120, len(contracts) - 2)
        for k, c in enumerate(contracts):
            price = base[i] + 20 * k
            oi = 1000 if k == seg + 1 and i % 120 > 100 else (800 if k == seg else 100)
            rows.append(
                {
                    "contract": c,
                    "date": d,
                    "open": price,
                    "high": price + 5,
                    "low": price - 5,
                    "close": price,
                    "volume": 100,
                    "open_interest": oi,
                }
            )
    raw = pd.DataFrame(rows)

    cfg = parse_config(
        {
            **example_raw,
            "storage": {"root": str(tmp_path)},
            "backtest": {"start": "2021-02-01", "capital": 300000, "slippage_ticks": 1},
            "data": {**example_raw["data"], "force_roll_day": 28},
        }
    )
    df, events = build_continuous("rb", raw, confirm_days=3, force_roll_day=28)
    ContinuousStore(cfg.storage.continuous_dir).save("rb", df, events)
    CarryStore(cfg.storage.carry_dir).save("rb", build_carry("rb", raw, 3, 28))
    return cfg
