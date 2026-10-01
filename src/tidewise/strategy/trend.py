"""vn.py 组合策略：多速度 EWMAC + carry 合成信号，波动率定仓，持仓缓冲，账户级保证金上限。

目标持仓由 set_target() 设定，下单完全交给 vn.py 的 rebalance_portfolio()（先平后开、按仓差补单）。
calculate_price 给出宽限价，使回测在 T+1 开盘价成交（vn.py 撮合价 = min(委托价, 开盘价)）。
carry 逐日序列由数据层预先计算并经 setting 注入（键：vt_symbol → {date: carry}）。
"""

from __future__ import annotations

from typing import Any

from vnpy.trader.constant import Direction
from vnpy.trader.object import BarData, TickData
from vnpy_portfoliostrategy import StrategyTemplate

from tidewise.strategy.ensemble import (
    CarryState,
    EwmacEnsembleState,
    combine_forecasts,
)
from tidewise.strategy.ewmac import AVG_ABS_FORECAST, buffered_target, optimal_position


class EwmacTrendStrategy(StrategyTemplate):
    author = "tidewise"

    vol_lookback: int = 25
    vol_target: float = 0.025
    buffer_fraction: float = 0.1
    capital: float = 300_000
    warmup_days: int = 300
    price_band: float = 0.05  # 限价相对参考价的宽限（回测中用于保证按开盘价成交）
    max_margin_usage: float = 0.4  # 总保证金占用上限（占权益）
    max_instrument_margin: float = 0.1  # 单品种保证金占用上限（占权益）

    parameters = [
        "vol_lookback",
        "vol_target",
        "buffer_fraction",
        "capital",
        "warmup_days",
        "price_band",
        "max_margin_usage",
        "max_instrument_margin",
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
        # 非序列化设置项：carry 序列与品种保证金率（经 setting 注入，不进 parameters）
        self.carry_series: dict[str, dict] = setting.get("carry_series", {})
        self.margin_ratios: dict[str, float] = setting.get("margin_ratios", {})
        self.trend_states = {s: EwmacEnsembleState(self.vol_lookback) for s in vt_symbols}
        self.carry_states = {s: CarryState() for s in vt_symbols}
        self.last_forecast: dict[str, float | None] = {}

    def on_init(self) -> None:
        # 各信号自行门控（未就绪返回 None，合成时权重归一），
        # 强制预热只需满足 carry 的波动估计窗口
        min_bars = CarryState().min_bars
        if self.warmup_days < min_bars:
            raise ValueError(f"warmup_days={self.warmup_days} 小于信号所需最少样本 {min_bars}")
        self.write_log("策略初始化")
        self.load_bars(self.warmup_days)

    def on_start(self) -> None:
        self.write_log("策略启动")

    def on_stop(self) -> None:
        self.write_log("策略停止")

    def on_tick(self, tick: TickData) -> None:  # 日线策略不处理 tick
        pass

    def _forecast(self, vt_symbol: str, bar: BarData) -> float | None:
        trend = self.trend_states[vt_symbol].update(bar.close_price)
        carry_v = self.carry_series.get(vt_symbol, {}).get(bar.datetime.date())
        carry = self.carry_states[vt_symbol].update(carry_v) if carry_v is not None else None
        combined, _ = combine_forecasts(trend, carry)
        return combined

    def on_bars(self, bars: dict[str, BarData]) -> None:
        self._bars_cache = bars  # 供组合层保证金估计使用
        for vt_symbol, bar in bars.items():
            forecast = self._forecast(vt_symbol, bar)
            self.last_forecast[vt_symbol] = forecast
            if forecast is None:
                continue
            state = self.trend_states[vt_symbol]
            multiplier = self.get_size(vt_symbol)
            optimal = optimal_position(
                forecast, state.daily_vol, multiplier, self.capital, self.vol_target
            )
            avg = optimal_position(
                AVG_ABS_FORECAST, state.daily_vol, multiplier, self.capital, self.vol_target
            )
            target = buffered_target(optimal, self.get_pos(vt_symbol), avg, self.buffer_fraction)
            target = self._cap_by_margin(vt_symbol, target, bar.close_price)
            self.set_target(vt_symbol, target)

        if self.trading:
            self.rebalance_portfolio(bars)
        self.put_event()

    def _cap_by_margin(self, vt_symbol: str, target: int, price: float) -> int:
        """单品种保证金上限；总占用上限对全部目标按比例缩减。"""
        ratio = self.margin_ratios.get(vt_symbol, 0.1)
        per_lot = price * self.get_size(vt_symbol) * ratio
        if per_lot <= 0:
            return target
        limit = int(self.capital * self.max_instrument_margin / per_lot)
        target = max(-limit, min(limit, target))
        # 组合层：估计全部目标的总占用，超限按比例缩减
        total = 0.0
        for s in self.vt_symbols:
            t = target if s == vt_symbol else self.get_target(s)
            r = self.margin_ratios.get(s, 0.1)
            bar = self.bars_cache.get(s)
            if bar is None:
                continue
            total += abs(t) * bar.close_price * self.get_size(s) * r
        if total > self.capital * self.max_margin_usage and per_lot > 0:
            scale = self.capital * self.max_margin_usage / total
            target = int(target * scale) if target >= 0 else -int(-target * scale)
        return target

    @property
    def bars_cache(self) -> dict[str, BarData]:
        """最近一次 on_bars 的 K 线（用于组合层占用估计）。"""
        return getattr(self, "_bars_cache", {})

    def calculate_price(self, vt_symbol: str, direction: Direction, reference: float) -> float:
        if direction == Direction.LONG:
            return reference * (1 + self.price_band)
        return reference * (1 - self.price_band)
