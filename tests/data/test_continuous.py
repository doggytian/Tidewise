from datetime import date, timedelta

import pandas as pd
import pytest

from tidewise.data.continuous import build_continuous, select_dominant


def _rows(contract: str, start: date, days: int, close: float, oi: list[float] | float):
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


D0 = date(2025, 1, 2)


def _two_contract_raw(far_oi: list[float]) -> pd.DataFrame:
    # RB2505 恒定持仓 1000、价格 3000；RB2510 价格 3100，持仓按 far_oi 变化
    n = len(far_oi)
    return pd.DataFrame(_rows("RB2505", D0, n, 3000, 1000) + _rows("RB2510", D0, n, 3100, far_oi))


def test_roll_needs_confirm_days():
    raw = _two_contract_raw([500, 1200, 1200, 800, 1200, 1200, 1200, 1200])
    dom, events = select_dominant("rb", raw, confirm_days=3, force_roll_day=15)
    assert dom.iloc[0] == "RB2505"
    # 第 2~3 日领先但第 4 日中断；第 5~7 日连续领先 3 日 → 第 7 日判定切换
    assert len(events) == 1
    e = events[0]
    assert e.decided_on == D0 + timedelta(days=6)
    assert (e.from_contract, e.to_contract, e.gap, e.forced) == ("RB2505", "RB2510", 100.0, False)
    assert dom.loc[D0 + timedelta(days=5)] == "RB2505"
    assert dom.loc[D0 + timedelta(days=6)] == "RB2510"


def test_never_rolls_back_to_near_month():
    raw = _two_contract_raw([2000] * 4 + [10] * 4)
    dom, events = select_dominant("rb", raw, confirm_days=1, force_roll_day=15)
    assert dom.iloc[0] == "RB2510"  # 首日直接选持仓最大者
    assert events == []
    assert set(dom) == {"RB2510"}


def test_forced_roll_before_delivery_month():
    start = date(2025, 4, 10)
    raw = pd.DataFrame(
        _rows("RB2505", start, 10, 3000, 5000) + _rows("RB2510", start, 10, 3100, 100)
    )
    dom, events = select_dominant("rb", raw, confirm_days=3, force_roll_day=15)
    assert len(events) == 1 and events[0].forced
    assert events[0].decided_on == date(2025, 4, 15)
    assert dom.loc[date(2025, 4, 14)] == "RB2505"
    assert dom.loc[date(2025, 4, 15)] == "RB2510"


def test_continuous_back_adjusted_and_held_next_day():
    raw = _two_contract_raw([500, 1200, 1200, 1200, 1200, 1200])
    df, events = build_continuous("rb", raw, confirm_days=3, force_roll_day=15)
    decided = events[0].decided_on  # 第 4 日
    # 序列从第 2 日开始（第 1 日收盘才判定首个主力）
    assert df["date"].iloc[0] == D0 + timedelta(days=1)
    before = df[df["date"] <= decided]
    after = df[df["date"] > decided]
    assert set(before["contract"]) == {"RB2505"}  # 判定当日仍持有旧合约
    assert set(after["contract"]) == {"RB2510"}
    # 加法后复权：旧合约价格 + 价差，最新价格等于真实价格
    assert before["close"].tolist() == [3100.0] * len(before)
    assert after["close"].tolist() == after["raw_close"].tolist()
    assert before["raw_close"].tolist() == [3000.0] * len(before)
    assert df["rolled"].sum() == 1
    assert bool(df.loc[df["date"] == decided + timedelta(days=1), "rolled"].iloc[0])


def test_no_lookahead_prefix_invariance():
    """截断未来数据不改变已有日期的主力判定。"""
    raw = _two_contract_raw([500, 900, 1200, 1300, 1400, 1500, 1600, 1700, 1800, 1900])
    full, _ = select_dominant("rb", raw, confirm_days=2, force_roll_day=15)
    for k in range(3, 10):
        cut = raw[raw["date"] < D0 + timedelta(days=k)]
        part, _ = select_dominant("rb", cut, confirm_days=2, force_roll_day=15)
        assert part.equals(full.iloc[: len(part)])


def test_data_hole_does_not_trigger_roll():
    """真实案例：新浪 MA 2019-07-17 只有冷门合约有行情，不得据此换月。"""
    raw = _two_contract_raw([500] * 6)
    hole = D0 + timedelta(days=3)
    raw = raw[~((raw["date"] == hole) & (raw["contract"] == "RB2505"))]
    dom, events = select_dominant("rb", raw, confirm_days=1, force_roll_day=15)
    assert events == []
    assert dom.loc[hole] == "RB2505"
    df, _ = build_continuous("rb", raw, confirm_days=1, force_roll_day=15)
    assert hole not in set(df["date"])  # 缺口日不造数据
    assert df["close"].notna().all()


def test_empty_raw():
    df, events = build_continuous(
        "rb", pd.DataFrame(columns=["contract", "date", "open_interest", "close"])
    )
    assert df.empty and events == []


def test_nonpositive_after_adjust_shifts_uniformly():
    raw = pd.DataFrame(
        _rows("RB2505", D0, 5, 50, 1000) + _rows("RB2510", D0, 5, 10, [0, 2000, 2000, 2000, 2000])
    )
    raw.loc[raw["contract"] == "RB2505", "low"] = 5  # 旧合约低点 5，价差 -40 → 复权后为负
    df, _ = build_continuous("rb", raw, confirm_days=1, force_roll_day=15)
    assert (df[["open", "high", "low", "close"]] > 0).all().all()
    # 整体平移：相邻收盘价差与未平移的加法复权一致（旧合约段 50-40=10，新合约段 10）
    assert df["close"].diff().dropna().abs().max() == pytest.approx(0)
    assert df["raw_close"].tolist()[:1] == [50.0]
