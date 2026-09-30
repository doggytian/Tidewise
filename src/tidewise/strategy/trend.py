"""vn.py 组合策略：EWMAC 趋势 + 波动率定仓 + 持仓缓冲。

目标持仓由 set_target() 设定，下单完全交给 vn.py 的 rebalance_portfolio()（先平后开、按仓差补单）。
calculate_price 给出宽限价，使回测在 T+1 开盘价成交（vn.py 撮合价 = min(委托价, 开盘价)）。
"""

from __future__ import annotations

from typing import Any

from vnpy.trader.constant import Direction
from vnpy.trader.object import BarData, TickData
from vnpy_portfoliostrategy import StrategyTemplate

from tidewise.strategy.ewmac import (
    EwmacParams,
    EwmacState,
    buffered_target,
    optimal_position,
)


class EwmacTrendStrategy(StrategyTemplate):
    author = "tidewise"

    fast_span: int = 16
    slow_span: int = 64
    vol_lookback: int = 25
    vol_target: float = 0.04
    buffer_fraction: float = 0.1
    capital: float = 300_000
    warmup_days: int = 150
    price_band: float = 0.05  # 限价相对参考价的宽限（回测中用于保证按开盘价成交）

    parameters = [
        "fast_span",
        "slow_span",
        "vol_lookback",
        "vol_target",
        "buffer_fraction",
        "capital",
        "warmup_days",
        "price_band",
    ]
    variables: list[str] = []

    def __init__(
        self,
        strategy_engine: Any,
        strategy_name: str,
        vt_symbols: list[str],
        setting: dict,
    ) -> None:
        super().__init__(strategy_engine, strategy_name, vt_symbols, setting)
        self.params = EwmacParams(self.fast_span, self.slow_span, self.vol_lookback)
        self.states: dict[str, EwmacState] = {s: EwmacState(self.params) for s in vt_symbols}
        self.last_forecast: dict[str, float | None] = {}

    def on_init(self) -> None:
        if self.warmup_days < self.params.min_bars:
            raise ValueError(
                f"warmup_days={self.warmup_days} 小于信号所需最少样本 {self.params.min_bars}"
            )
        self.write_log("策略初始化")
        self.load_bars(self.warmup_days)

    def on_start(self) -> None:
        self.write_log("策略启动")

    def on_stop(self) -> None:
        self.write_log("策略停止")

    def on_tick(self, tick: TickData) -> None:  # 日线策略不处理 tick
        pass

    def on_bars(self, bars: dict[str, BarData]) -> None:
        for vt_symbol, bar in bars.items():
            state = self.states[vt_symbol]
            forecast = state.update(bar.close_price)
            self.last_forecast[vt_symbol] = forecast
            if forecast is None:
                continue
            multiplier = self.get_size(vt_symbol)
            optimal = optimal_position(
                forecast, state.daily_vol, multiplier, self.capital, self.vol_target
            )
            avg = optimal_position(10.0, state.daily_vol, multiplier, self.capital, self.vol_target)
            target = buffered_target(optimal, self.get_pos(vt_symbol), avg, self.buffer_fraction)
            self.set_target(vt_symbol, target)

        if self.trading:
            self.rebalance_portfolio(bars)
        self.put_event()

    def calculate_price(self, vt_symbol: str, direction: Direction, reference: float) -> float:
        if direction == Direction.LONG:
            return reference * (1 + self.price_band)
        return reference * (1 - self.price_band)
