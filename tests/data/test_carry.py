from datetime import date, timedelta

import pandas as pd
import pytest

from tidewise.data.carry import build_carry


def _rows(contract, start, days, close, oi):
    ois = oi if isinstance(oi, list) else [oi] * days
    return [
        {
            "contract": contract,
            "date": start + timedelta(days=i),
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 100,
            "open_interest": ois[i],
        }
        for i in range(days)
    ]


def test_carry_annualized_backwardation_positive():
    """近月 3000、次远月 2900（隔 5 个月）：贴水结构 carry 为正。"""
    d0 = date(2025, 1, 2)
    raw = pd.DataFrame(_rows("RB2505", d0, 5, 3000, 1000) + _rows("RB2510", d0, 5, 2900, 100))
    df = build_carry("rb", raw, confirm_days=3, force_roll_day=15)
    assert len(df) == 5  # 首日收盘即可判定主力，carry 以判定日收盘价计算，T+1 开盘交易
    expected = (3000 / 2900 - 1) * 365 / 150
    assert df["carry"].iloc[0] == pytest.approx(expected)
    assert df["near"].iloc[0] == "RB2505" and df["far"].iloc[0] == "RB2510"


def test_carry_follows_dominant_roll():
    d0 = date(2025, 1, 2)
    far_oi = [100, 2000, 2000, 2000, 2000]
    raw = pd.DataFrame(
        _rows("RB2505", d0, 5, 3000, 1000)
        + _rows("RB2510", d0, 5, 3100, far_oi)
        + _rows("RB2601", d0, 5, 3050, 50)
    )
    df = build_carry("rb", raw, confirm_days=1, force_roll_day=15)
    # 判定换月后（次日持有新合约），near 变为 RB2510，far 变为 RB2601
    rolled = df[df["near"] == "RB2510"]
    assert not rolled.empty and set(rolled["far"]) == {"RB2601"}


def test_carry_empty_raw():
    df = build_carry("rb", pd.DataFrame(columns=["contract", "date", "close", "open_interest"]))
    assert df.empty
