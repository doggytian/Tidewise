"""决策链复放：在历史数据上逐日重放「信号 → 定仓 → 缓冲 → 风控」，得到每日目标持仓。

与 vn.py 策略（strategy/trend.py）共用同一套纯函数；一致性由测试
（tests/backtest/test_replay_consistency.py）保证：同一数据上 replay 与 vn.py 回测的
每日目标逐日一致。

持仓代理：假设每日目标都在次日开盘成交（与回测撮合口径一致），用于缓冲与组合波动估计。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from tidewise.config.schema import AppConfig, InstrumentConfig
from tidewise.portfolio import cap_by_margins, vol_scalar
from tidewise.strategy.ensemble import CarryState, EwmacEnsembleState, combine_forecasts
from tidewise.strategy.ewmac import AVG_ABS_FORECAST, buffered_target, optimal_position


@dataclass(frozen=True)
class DailyDecision:
    date: date
    targets: dict[str, int]  # 连续合约 vt_symbol → 目标手数
    forecasts: dict[str, float | None]
    scalar: float  # 组合波动率 scalar
    margin_usage: float  # 目标组合保证金占用（占权益）


class DecisionReplayer:
    """多品种联合逐日重放（板块/组合上限需要跨品种联动）。"""

    def __init__(self, cfg: AppConfig, instruments: list[InstrumentConfig]) -> None:
        self.cfg = cfg
        self.instruments = instruments
        self.vt = [i.continuous_vt_symbol for i in instruments]
        self.trend = {s: EwmacEnsembleState(cfg.sizing.vol_lookback_days) for s in self.vt}
        self.carry = {s: CarryState() for s in self.vt}
        self.positions: dict[str, int] = dict.fromkeys(self.vt, 0)  # 持仓代理（= 昨目标）
        self.prev_position: dict[str, int] = dict.fromkeys(self.vt, 0)  # 前目标（昨仓）
        self.prev_close: dict[str, float] = {}
        self._trading = True
        self._trading_start: date | None = None
        self.daily_pnls: list[float] = []

    def step(
        self,
        day: date,
        bars: dict[str, tuple[float, float]],  # vt_symbol → (open, close)
        carries: dict[str, float | None],
    ) -> DailyDecision | None:
        """推入一个交易日，返回当日决策。盈亏口径与 vn.py 策略一致：
        昨仓 × (今收 − 昨收) + 今日成交 × (今收 − 今开)。"""
        cfg = self.cfg
        mult = {s: i.multiplier for s, i in zip(self.vt, self.instruments, strict=True)}
        # positions = 昨目标（今开成交后）；prev_position = 前目标（昨仓）
        day_pnl = 0.0
        for s, (open_, close) in bars.items():
            prev = self.prev_close.get(s)
            if prev is not None:
                day_pnl += self.prev_position.get(s, 0) * (close - prev) * mult[s]
                trade = self.positions[s] - self.prev_position.get(s, 0)
                if trade:
                    day_pnl += trade * (close - open_) * mult[s]
            self.prev_close[s] = close
        self.daily_pnls.append(day_pnl)

        scalar = vol_scalar(
            self.daily_pnls,
            cfg.backtest.capital,
            cfg.portfolio.annual_vol_target,
            cfg.portfolio.realized_vol_lookback,
        )

        closes = {s: c for s, (_o, c) in bars.items()}
        forecasts: dict[str, float | None] = {}
        raw_targets: dict[str, int] = {}
        for s, close in closes.items():
            trend_f = self.trend[s].update(close)
            carry_v = carries.get(s)
            carry_f = self.carry[s].update(carry_v) if carry_v is not None else None
            forecast, _ = combine_forecasts(trend_f, carry_f)
            forecasts[s] = forecast
            if forecast is None:
                raw_targets[s] = self.positions[s]
                continue
            args = (
                self.trend[s].daily_vol,
                mult[s],
                cfg.backtest.capital,
                cfg.sizing.instrument_vol_target,
            )
            raw_targets[s] = buffered_target(
                optimal_position(forecast, *args) * scalar,
                self.positions[s],
                optimal_position(AVG_ABS_FORECAST, *args),
                cfg.sizing.buffer_fraction,
            )

        inst = {s: i for s, i in zip(self.vt, self.instruments, strict=True)}
        targets = cap_by_margins(
            raw_targets,
            closes,
            mult,
            {s: inst[s].margin_ratio for s in closes},
            {s: inst[s].cluster for s in closes},
            cfg.backtest.capital,
            cfg.risk.max_instrument_margin,
            cfg.risk.max_cluster_margin,
            cfg.risk.max_margin_usage,
        )
        # 持仓代理：目标次日开盘成交。预热期（trading=False）不交易、持仓保持 0，
        # 与 vn.py 策略一致（vn.py 预热期 get_pos 恒为 0）
        self.prev_position = self.positions
        if self._trading:
            self.positions = dict(self.positions, **targets)

        margin = sum(
            abs(t) * closes[s] * mult[s] * inst[s].margin_ratio for s, t in targets.items()
        )
        return DailyDecision(day, targets, forecasts, scalar, margin / cfg.backtest.capital)

    def replay(
        self,
        frames: dict[str, pd.DataFrame],
        carry: dict[str, dict[date, float]],
        trading_start: date | None = None,
    ) -> list[DailyDecision]:
        """按交易日并集逐日重放；当日无行情的品种不更新其状态。
        trading_start 之前为预热期：更新信号但不交易（与 vn.py 回测一致）。"""
        self._trading_start = trading_start
        days = sorted({d for df in frames.values() for d in df["date"]})
        bars_by_day: dict[date, dict[str, tuple[float, float]]] = {}
        for s, df in frames.items():
            for r in df.itertuples(index=False):
                bars_by_day.setdefault(r.date, {})[s] = (float(r.open), float(r.close))
        out = []
        for d in days:
            self._trading = self._trading_start is None or d >= self._trading_start
            dec = self.step(d, bars_by_day[d], {s: carry.get(s, {}).get(d) for s in bars_by_day[d]})
            out.append(dec)
        return out
