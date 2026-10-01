"""一致性：同一数据上，DecisionReplayer 的每日目标必须与 vn.py 回测策略的目标逐日一致。

这是「报告与回测同源」的护栏：report 阶段用 replayer，回测用 vn.py 引擎。
"""

from tidewise.backtest.runner import run_backtest
from tidewise.data.carry import CarryStore
from tidewise.data.pipeline import ContinuousStore
from tidewise.strategy.replay import DecisionReplayer


def test_replay_matches_backtest_targets(cfg):
    """cfg fixture 提供合成 rb 数据（含 carry）。"""
    result = run_backtest(cfg, ["rb"])
    inst = cfg.instruments(["rb"])
    frames = {"rb888.SHFE": ContinuousStore(cfg.storage.continuous_dir).load("rb")}
    carry_df = CarryStore(cfg.storage.carry_dir).load("rb")
    carry = {"rb888.SHFE": dict(zip(carry_df["date"], carry_df["carry"], strict=True))}

    decisions = DecisionReplayer(cfg, inst).replay(
        frames, carry, trading_start=result.trading_start
    )
    by_date = {d.date: d for d in decisions}

    # 从回测成交重建策略的逐日目标序列：目标 = 累计成交（次日开盘成交，全部为全开仓口径）
    # 更直接的对法：replayer 的目标应等于 vn.py 策略在每日收盘时 set_target 的值。
    # vn.py 的 target 序列 = 持仓 + 当日本不该有的活动委托… 这里用「次日持仓」近似：
    # 目标(d) == 持仓(d+1)，持仓(d+1) = Σ trades[:d+1]（开盘价成交、无拒单）
    trades = result.trades.sort_values("date")
    daily_dates = sorted({d.date for d in decisions})
    pos_after: dict = {}
    pos = 0
    for d in daily_dates:
        day_trades = trades[trades["date"] == d]
        for t in day_trades.itertuples(index=False):
            pos += t.volume if t.direction == "多" else -t.volume
        pos_after[d] = pos

    checked = 0
    for d in daily_dates:
        # 跳过预热期（策略 trading=False 之前不设目标）
        if d < result.trading_start:
            continue
        i = daily_dates.index(d)
        if i + 1 >= len(daily_dates):
            break
        next_day = daily_dates[i + 1]
        replay_target = by_date[d].targets["rb888.SHFE"]
        # vn.py 策略在 d 日 set_target，d+1 开盘成交 → d+1 收盘后持仓 = 目标
        assert pos_after[next_day] == replay_target, (
            f"{d}: replay 目标 {replay_target} ≠ vn.py 次日持仓 {pos_after[next_day]}"
        )
        checked += 1
    assert checked > 300
