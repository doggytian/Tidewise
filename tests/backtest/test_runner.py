"""用合成数据端到端跑 vn.py 回测，验证撮合口径、换月成本与报告。"""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from tidewise.backtest.runner import run_backtest, write_report
from tidewise.config import parse_config
from tidewise.data.carry import CarryStore, build_carry
from tidewise.data.continuous import build_continuous
from tidewise.data.pipeline import ContinuousStore


def _synthetic_raw(days: int = 900, seed: int = 7) -> pd.DataFrame:
    """两段趋势 + 噪声；每 120 日换一次合约，远月比近月高 20 点。"""
    rng = np.random.default_rng(seed)
    trend = np.concatenate([np.linspace(0, 600, days // 2), np.linspace(600, 0, days - days // 2)])
    base = 3000 + trend + rng.normal(0, 15, days).cumsum() * 0.3
    start = date(2020, 1, 2)
    dates = [start + timedelta(days=i) for i in range(days)]
    contracts = [
        "RB2101",
        "RB2105",
        "RB2110",
        "RB2201",
        "RB2205",
        "RB2210",
        "RB2301",
        "RB2305",
    ]
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
    return pd.DataFrame(rows)


@pytest.fixture
def cfg(tmp_path, example_raw):
    example_raw["storage"] = {"root": str(tmp_path)}
    example_raw["backtest"] = {"start": "2021-02-01", "capital": 300000, "slippage_ticks": 1}
    example_raw["data"]["force_roll_day"] = 28  # 合成数据按自然日排列，避免强制换月干扰
    c = parse_config(example_raw)
    raw = _synthetic_raw()
    df, events = build_continuous("rb", raw, confirm_days=3, force_roll_day=28)
    ContinuousStore(c.storage.continuous_dir).save("rb", df, events)
    CarryStore(c.storage.carry_dir).save("rb", build_carry("rb", raw, 3, 28))
    return c


def test_backtest_end_to_end(cfg, tmp_path):
    result = run_backtest(cfg, ["rb"])
    s = result.stats
    assert s["total_trade_count"] > 0
    assert result.trading_start >= date(2021, 2, 1)
    assert result.roll_count > 0 and result.roll_cost_total > 0
    # 净盈亏已扣除换月成本
    assert result.daily["roll_cost"].sum() == pytest.approx(result.roll_cost_total)
    report = write_report(result, tmp_path / "reports", "rb")
    text = report.read_text(encoding="utf-8")
    assert "换月成本" in text and "rb888.SHFE" in text


def test_fills_at_next_open(cfg):
    result = run_backtest(cfg, ["rb"])
    frame = ContinuousStore(cfg.storage.continuous_dir).load("rb").set_index("date")
    assert not result.trades.empty
    # 宽限限价 → 撮合价 = min(限价, 开盘价) = 开盘价；且成交日期不早于回测起点
    for t in result.trades.itertuples(index=False):
        assert t.price == pytest.approx(frame.at[t.date, "open"])
    # 预热恰为 trading_start 之前的全部交易日：最早成交在 trading_start 的下一交易日
    assert result.trades["date"].min() > result.trading_start


def test_matches_independent_simulation(cfg):
    """vn.py 回测毛盈亏与独立纯 Python 模拟逐日一致（防框架口径漂移）。

    复刻策略的完整决策链：多速度 EWMAC + carry 合成 → 波动率定仓 → 持仓缓冲 → 保证金上限。
    合成数据下保证金上限不触发（仓位 ≤ 2 手），故此处可不复刻组合层缩减。
    """
    from tidewise.strategy.ensemble import CarryState, EwmacEnsembleState, combine_forecasts
    from tidewise.strategy.ewmac import buffered_target, optimal_position

    result = run_backtest(cfg, ["rb"])
    df = ContinuousStore(cfg.storage.continuous_dir).load("rb")
    carry = dict(
        zip(
            CarryStore(cfg.storage.carry_dir).load("rb")["date"],
            CarryStore(cfg.storage.carry_dir).load("rb")["carry"],
            strict=True,
        )
    )
    sz, s, rk = cfg.sizing, cfg.backtest, cfg.risk
    trend = EwmacEnsembleState(sz.vol_lookback_days)
    carry_state = CarryState()
    warm = int((df["date"] < result.trading_start).sum())
    pos, pending, prev, pnl = 0, None, 0.0, {}
    for i, b in enumerate(df.itertuples(index=False)):
        trade = pending - pos if pending is not None else 0
        pnl[b.date] = ((pos * (b.close - prev)) if i else 0.0) + trade * (b.close - b.open)
        pos += trade
        t = trend.update(b.close)
        c_v = carry.get(b.date)
        c = carry_state.update(c_v) if c_v is not None else None
        f, _ = combine_forecasts(t, c)
        target = pos
        if f is not None:
            args = (trend.daily_vol, 10, s.capital, sz.instrument_vol_target)
            target = buffered_target(
                optimal_position(f, *args), pos, optimal_position(10, *args), sz.buffer_fraction
            )
            per_lot = b.close * 10 * 0.10  # rb 保证金率
            limit = int(s.capital * rk.max_instrument_margin / per_lot)
            target = max(-limit, min(limit, target))
            assert abs(target) * per_lot <= s.capital * rk.max_margin_usage
        pending = target if i >= warm else None
        prev = b.close
    ref = pd.Series(pnl) * 10
    vn = result.daily["total_pnl"]
    assert (ref.reindex(vn.index) - vn).abs().max() == pytest.approx(0, abs=1e-6)


def test_warmup_insufficient(cfg, example_raw, tmp_path):
    example_raw["storage"] = {"root": str(tmp_path)}
    example_raw["backtest"] = {"start": "2020-02-01", "capital": 300000}
    with pytest.raises(ValueError, match="预热"):
        run_backtest(parse_config(example_raw), ["rb"])


def test_missing_data(example_raw, tmp_path):
    example_raw["storage"] = {"root": str(tmp_path / "empty")}
    with pytest.raises(FileNotFoundError, match="data update"):
        run_backtest(parse_config(example_raw), ["rb"])
