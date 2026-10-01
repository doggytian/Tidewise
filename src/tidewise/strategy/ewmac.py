"""EWMAC 趋势信号与波动率定仓（纯函数，回测与实盘信号共用）。

方法来自 Rob Carver《Systematic Trading》/《Advanced Futures Trading Strategies》公开的公式：
- 原始信号 = (EMA_fast − EMA_slow) / 日价格波动（点数）
- 预测值 = 原始信号 × forecast_scalar，截断到 [−20, 20]；平均绝对值约 10
- 目标手数 = 预测值/10 × 权益 × 年化波动目标 / (日波动点数 × 乘数 × 16)
仅参考公开公式，未使用 pysystemtrade（GPL-3）代码。
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

FORECAST_CAP = 20.0
AVG_ABS_FORECAST = 10.0
ANNUALIZE = 16.0  # sqrt(256)

# Carver 给出的 EWMAC 预测缩放系数（按 fast/slow 组合）
FORECAST_SCALARS: dict[tuple[int, int], float] = {
    (2, 8): 10.6,
    (4, 16): 7.5,
    (8, 32): 5.3,
    (16, 64): 3.75,
    (32, 128): 2.65,
    (64, 256): 1.87,
}


@dataclass(frozen=True)
class EwmacParams:
    fast_span: int = 16
    slow_span: int = 64
    vol_lookback: int = 25
    forecast_scalar: float | None = None  # None = 查 FORECAST_SCALARS

    def __post_init__(self) -> None:
        if not 1 < self.fast_span < self.slow_span:
            raise ValueError("需要 1 < fast_span < slow_span")
        if self.vol_lookback < 5:
            raise ValueError("vol_lookback 至少 5")
        if (
            self.forecast_scalar is None
            and (self.fast_span, self.slow_span) not in FORECAST_SCALARS
        ):
            raise ValueError(
                f"({self.fast_span},{self.slow_span}) 无默认 forecast_scalar，需显式配置"
            )

    @property
    def scalar(self) -> float:
        if self.forecast_scalar is not None:
            return self.forecast_scalar
        return FORECAST_SCALARS[(self.fast_span, self.slow_span)]

    @property
    def min_bars(self) -> int:
        """EMA 充分收敛所需的最少样本。"""
        return 2 * self.slow_span


@dataclass
class EwmacState:
    """逐根推进的增量状态。update() 只依赖已推入的数据，不存在未来函数。"""

    params: EwmacParams

    @classmethod
    def from_spans(cls, fast_span: int, slow_span: int, vol_lookback: int = 25) -> EwmacState:
        return cls(EwmacParams(fast_span, slow_span, vol_lookback))

    count: int = 0
    ema_fast: float = math.nan
    ema_slow: float = math.nan
    last_close: float = math.nan
    diffs: deque[float] = field(default_factory=deque)

    def update(self, close: float) -> float | None:
        p = self.params
        if self.count == 0:
            self.ema_fast = self.ema_slow = close
        else:
            self.ema_fast += 2 / (p.fast_span + 1) * (close - self.ema_fast)
            self.ema_slow += 2 / (p.slow_span + 1) * (close - self.ema_slow)
            self.diffs.append(close - self.last_close)
            if len(self.diffs) > p.vol_lookback:
                self.diffs.popleft()
        self.last_close = close
        self.count += 1
        return self.forecast()

    @property
    def daily_vol(self) -> float:
        """日价格波动（点数，样本标准差）。"""
        n = len(self.diffs)
        if n < 2:
            return math.nan
        mean = sum(self.diffs) / n
        return math.sqrt(sum((x - mean) ** 2 for x in self.diffs) / (n - 1))

    @property
    def ready(self) -> bool:
        return self.count >= self.params.min_bars and self.daily_vol > 0

    def forecast(self) -> float | None:
        if not self.ready:
            return None
        raw = (self.ema_fast - self.ema_slow) / self.daily_vol
        return max(-FORECAST_CAP, min(FORECAST_CAP, raw * self.params.scalar))


def optimal_position(
    forecast: float,
    daily_vol_points: float,
    multiplier: float,
    equity: float,
    vol_target: float,
) -> float:
    """未取整的目标手数（有符号）。"""
    if daily_vol_points <= 0 or multiplier <= 0 or equity <= 0:
        return 0.0
    annual_cash_vol_per_lot = daily_vol_points * multiplier * ANNUALIZE
    return forecast / AVG_ABS_FORECAST * equity * vol_target / annual_cash_vol_per_lot


def buffered_target(
    optimal: float, current: int, avg_position: float, buffer_fraction: float
) -> int:
    """Carver 持仓缓冲：当前持仓落在 optimal ± buffer 内不交易，否则只调到缓冲区边缘。"""
    width = abs(avg_position) * buffer_fraction
    lower, upper = optimal - width, optimal + width
    if lower <= current <= upper:
        return current
    edge = lower if current < lower else upper
    return int(round(edge))
