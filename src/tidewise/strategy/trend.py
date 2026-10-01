"""vn.py 组合策略：多速度 EWMAC + carry 合成信号，波动率定仓，持仓缓冲，分层保证金上限，
组合波动率目标 scalar。

目标持仓由 set_target() 设定，下单完全交给 vn.py 的 rebalance_portfolio()（先平后开、按仓差补单）。
calculate_price 给出宽限价，使回测在 T+1 开盘价成交（vn.py 撮合价 = min(委托价, 开盘价)）。
carry 逐日序列由数据层预先计算并经 setting 注入（键：vt_symbol → {date: carry}）。
"""

from __future__ import annotations

from collections import deque
from typing import Any

from vnpy.trader.constant import Direction
from vnpy.trader.object import BarData, TickData, TradeData
from vnpy_portfoliostrategy import StrategyTemplate

from tidewise.portfolio import cap_by_margins, vol_scalar
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
    max_margin_usage: float = 0.4  # 组合保证金占用上限
    max_instrument_margin: float = 0.1  # 单品种保证金占用上限
    max_cluster_margin: float = 0.15  # 同板块品种合计保证金占用上限
    portfolio_vol_target: float = 0.10  # 组合年化波动目标（占权益）
    portfolio_vol_lookback: int = 25  # 实现波动估计窗口

    parameters = [
        "vol_lookback",
        "vol_target",
        "buffer_fraction",
        "capital",
        "warmup_days",
        "price_band",
        "max_margin_usage",
        "max_instrument_margin",
        "max_cluster_margin",
        "portfolio_vol_target",
        "portfolio_vol_lookback",
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
        # 非序列化设置项：carry 序列、保证金率、板块（经 setting 注入，不进 parameters）
        self.carry_series: dict[str, dict] = setting.get("carry_series", {})
        self.margin_ratios: dict[str, float] = setting.get("margin_ratios", {})
        self.clusters: dict[str, str] = setting.get("clusters", {})
        self.trend_states = {s: EwmacEnsembleState(self.vol_lookback) for s in vt_symbols}
        self.carry_states = {s: CarryState() for s in vt_symbols}
        self.last_forecast: dict[str, float | None] = {}
        # 组合波动估计：策略自身逐日盈亏
        self._daily_pnls: deque[float] = deque(maxlen=self.portfolio_vol_lookback * 2)
        self._prev_close: dict[str, float] = {}
        self._today_trades: list[tuple[str, float, float]] = []

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

    def update_trade(self, trade: TradeData) -> None:
        super().update_trade(trade)
        sign = 1 if trade.direction == Direction.LONG else -1
        # 逐笔盈亏在 on_bars 用当日收盘价结算
        self._today_trades.append((trade.vt_symbol, sign * trade.volume, trade.price))

    def on_bars(self, bars: dict[str, BarData]) -> None:
        # 1) 组合层实现波动：持仓盈亏（昨仓 × 收盘差）+ 今日成交盈亏（成交→收盘）
        # 注意：on_bars 时 get_pos 已含今日成交，昨仓 = 当前持仓 − 今日成交
        today_signed: dict[str, float] = {}
        for vt_symbol, signed_vol, _price in self._today_trades:
            today_signed[vt_symbol] = today_signed.get(vt_symbol, 0.0) + signed_vol
        day_pnl = 0.0
        for vt_symbol, bar in bars.items():
            prev = self._prev_close.get(vt_symbol)
            if prev is not None:
                pos_start = self.get_pos(vt_symbol) - today_signed.get(vt_symbol, 0.0)
                day_pnl += pos_start * (bar.close_price - prev) * self.get_size(vt_symbol)
            self._prev_close[vt_symbol] = bar.close_price
        for vt_symbol, signed_vol, price in self._today_trades:
            if vt_symbol in bars:
                day_pnl += (
                    signed_vol * (bars[vt_symbol].close_price - price) * self.get_size(vt_symbol)
                )
        self._today_trades.clear()
        self._daily_pnls.append(day_pnl)

        scalar = vol_scalar(
            list(self._daily_pnls),
            self.capital,
            self.portfolio_vol_target,
            self.portfolio_vol_lookback,
        )

        # 2) 逐品种信号 → 未封顶目标
        raw_targets: dict[str, int] = {}
        prices: dict[str, float] = {}
        for vt_symbol, bar in bars.items():
            prices[vt_symbol] = bar.close_price
            trend = self.trend_states[vt_symbol].update(bar.close_price)
            carry_v = self.carry_series.get(vt_symbol, {}).get(bar.datetime.date())
            carry = self.carry_states[vt_symbol].update(carry_v) if carry_v is not None else None
            forecast, _ = combine_forecasts(trend, carry)
            self.last_forecast[vt_symbol] = forecast
            if forecast is None:
                raw_targets[vt_symbol] = self.get_pos(vt_symbol)  # 信号未就绪：保持
                continue
            state = self.trend_states[vt_symbol]
            multiplier = self.get_size(vt_symbol)
            optimal = optimal_position(
                forecast, state.daily_vol, multiplier, self.capital, self.vol_target
            )
            avg = optimal_position(
                AVG_ABS_FORECAST, state.daily_vol, multiplier, self.capital, self.vol_target
            )
            raw_targets[vt_symbol] = buffered_target(
                optimal * scalar, self.get_pos(vt_symbol), avg, self.buffer_fraction
            )

        # 3) 分层保证金上限
        targets = cap_by_margins(
            raw_targets,
            prices,
            {s: self.get_size(s) for s in bars},
            self.margin_ratios,
            self.clusters,
            self.capital,
            self.max_instrument_margin,
            self.max_cluster_margin,
            self.max_margin_usage,
        )
        for vt_symbol, target in targets.items():
            self.set_target(vt_symbol, target)

        if self.trading:
            self.rebalance_portfolio(bars)
        self.put_event()

    def calculate_price(self, vt_symbol: str, direction: Direction, reference: float) -> float:
        if direction == Direction.LONG:
            return reference * (1 + self.price_band)
        return reference * (1 - self.price_band)
