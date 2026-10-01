"""多信号组合：多速度 EWMAC 等权 + carry，权重冻结，信号未就绪时自动归一。

权重与参数在观察样本外结果之前冻结（见 docs/ROADMAP.md 研究纪律）。
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from tidewise.strategy.ewmac import FORECAST_CAP, EwmacState

TREND_SPEEDS: tuple[tuple[int, int], ...] = ((8, 32), (16, 64), (32, 128), (64, 256))
ENSEMBLE_DIVERSIFIER = 1.25  # 4 个相关性约 0.6 的信号等权合成的分散乘数（Carver）
TREND_WEIGHT = 0.6
CARRY_WEIGHT = 0.4
CARRY_EWM_SPAN = 60
CARRY_VOL_SPAN = 250
CARRY_SCALAR = 12.5  # 使平均绝对预测 ≈ 10


class EwmacEnsembleState:
    """多速度 EWMAC 等权合成。只依赖已推入的收盘价。"""

    def __init__(self, vol_lookback: int = 25) -> None:
        self.states = [EwmacState.from_spans(f, s, vol_lookback) for f, s in TREND_SPEEDS]

    @property
    def min_bars(self) -> int:
        return max(s.params.min_bars for s in self.states)

    @property
    def daily_vol(self) -> float:
        """取最慢速度的波动估计。"""
        return self.states[-1].daily_vol

    def update(self, close: float) -> float | None:
        forecasts = [f for f in (s.update(close) for s in self.states) if f is not None]
        if not forecasts:
            return None
        combined = sum(forecasts) / len(forecasts) * ENSEMBLE_DIVERSIFIER
        return max(-FORECAST_CAP, min(FORECAST_CAP, combined))


@dataclass
class CarryState:
    """carry 的 EWM 平滑 + 滚动标准差标准化。"""

    ewm_span: int = CARRY_EWM_SPAN
    vol_span: int = CARRY_VOL_SPAN
    count: int = 0
    ewm: float = math.nan
    history: deque[float] = field(default_factory=deque)

    @property
    def min_bars(self) -> int:
        return self.vol_span

    def update(self, carry: float) -> float | None:
        alpha = 2 / (self.ewm_span + 1)
        self.ewm = carry if self.count == 0 else self.ewm + alpha * (carry - self.ewm)
        self.history.append(carry)
        if len(self.history) > self.vol_span:
            self.history.popleft()
        self.count += 1
        if self.count < self.min_bars or len(self.history) < 2:
            return None
        n = len(self.history)
        mean = sum(self.history) / n
        std = math.sqrt(sum((x - mean) ** 2 for x in self.history) / (n - 1))
        if std <= 0:
            return None
        forecast = self.ewm / std * CARRY_SCALAR
        return max(-FORECAST_CAP, min(FORECAST_CAP, forecast))


def combine_forecasts(trend: float | None, carry: float | None) -> tuple[float | None, float]:
    """加权合成；未就绪的信号剔除后权重归一。返回 (预测值, 实际权重和)。"""
    parts = []
    if trend is not None:
        parts.append((trend, TREND_WEIGHT))
    if carry is not None:
        parts.append((carry, CARRY_WEIGHT))
    if not parts:
        return None, 0.0
    total_w = sum(w for _, w in parts)
    combined = sum(f * w for f, w in parts) / total_w
    return max(-FORECAST_CAP, min(FORECAST_CAP, combined)), total_w
