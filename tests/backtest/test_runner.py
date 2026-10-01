"""用合成数据端到端跑 vn.py 回测，验证撮合口径、换月成本与报告。"""

from datetime import date

import pandas as pd
import pytest

from tidewise.backtest.runner import run_backtest, write_report
from tidewise.config import parse_config
from tidewise.data.carry import CarryStore
from tidewise.data.pipeline import ContinuousStore


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

    复刻策略的完整决策链：多速度 EWMAC + carry 合成 → 波动率定仓 × 组合波动 scalar
    → 持仓缓冲 → 保证金上限。
    """
    from tidewise.portfolio import cap_by_margins, vol_scalar
    from tidewise.strategy.ensemble import CarryState, EwmacEnsembleState, combine_forecasts
    from tidewise.strategy.ewmac import buffered_target, optimal_position

    result = run_backtest(cfg, ["rb"])
    df = ContinuousStore(cfg.storage.continuous_dir).load("rb")
    carry_df = CarryStore(cfg.storage.carry_dir).load("rb")
    carry = dict(zip(carry_df["date"], carry_df["carry"], strict=True))
    sz, s, rk, pf = cfg.sizing, cfg.backtest, cfg.risk, cfg.portfolio
    trend = EwmacEnsembleState(sz.vol_lookback_days)
    carry_state = CarryState()
    warm = int((df["date"] < result.trading_start).sum())
    pos, pending, prev, pnl = 0, None, 0.0, {}
    daily_pnls: list[float] = []
    for i, b in enumerate(df.itertuples(index=False)):
        trade = pending - pos if pending is not None else 0
        day_pnl = ((pos * (b.close - prev)) if i else 0.0) + trade * (b.close - b.open)
        pnl[b.date] = day_pnl
        daily_pnls.append(day_pnl * 10)
        pos += trade
        scalar = vol_scalar(daily_pnls, s.capital, pf.annual_vol_target, pf.realized_vol_lookback)
        t = trend.update(b.close)
        c_v = carry.get(b.date)
        c = carry_state.update(c_v) if c_v is not None else None
        f, _ = combine_forecasts(t, c)
        target = pos
        if f is not None:
            args = (trend.daily_vol, 10, s.capital, sz.instrument_vol_target)
            target = buffered_target(
                optimal_position(f, *args) * scalar,
                pos,
                optimal_position(10, *args),
                sz.buffer_fraction,
            )
            target = cap_by_margins(
                {"rb888.SHFE": target},
                {"rb888.SHFE": b.close},
                {"rb888.SHFE": 10},
                {"rb888.SHFE": 0.10},
                {"rb888.SHFE": "black"},
                s.capital,
                rk.max_instrument_margin,
                rk.max_cluster_margin,
                rk.max_margin_usage,
            )["rb888.SHFE"]
        pending = target if i >= warm else None
        prev = b.close
    ref = pd.Series(pnl) * 10
    vn = result.daily["total_pnl"]
    assert (ref.reindex(vn.index) - vn).abs().max() == pytest.approx(0, abs=1e-6)
    # 组合波动 scalar 与保证金上限的缩减路径由 tests/test_portfolio.py 单独覆盖


def test_warmup_insufficient(cfg, example_raw, tmp_path):
    example_raw["storage"] = {"root": str(tmp_path)}
    example_raw["backtest"] = {"start": "2020-02-01", "capital": 300000}
    with pytest.raises(ValueError, match="预热"):
        run_backtest(parse_config(example_raw), ["rb"])


def test_missing_data(example_raw, tmp_path):
    example_raw["storage"] = {"root": str(tmp_path / "empty")}
    with pytest.raises(FileNotFoundError, match="data update"):
        run_backtest(parse_config(example_raw), ["rb"])
