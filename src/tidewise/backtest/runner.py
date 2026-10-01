"""回测：vn.py 组合回测引擎 + Tidewise 连续合约数据 + 换月成本补记。

- 数据直接从 Parquet 注入引擎（不经 vn.py 全局数据库），回测可复现、无全局状态。
- 撮合：T 日收盘信号 → T+1 按 min(委托价, 开盘价) 成交（vn.py 原生逻辑）。
- 手续费：vn.py 仅支持比例费率，按手收费的品种按样本期收盘价中位数折算。
- 换月成本：连续合约上不产生真实换月成交，按「持仓手数 × 2 腿 ×（手续费 + 滑点）」补记。
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from pathlib import Path

import numpy as np
import pandas as pd
from vnpy.trader.constant import Interval
from vnpy.trader.object import BarData
from vnpy.trader.utility import extract_vt_symbol
from vnpy_portfoliostrategy import BacktestingEngine

from tidewise.config.schema import AppConfig, InstrumentConfig
from tidewise.data.pipeline import CarryStore, ContinuousStore
from tidewise.strategy.ensemble import CarryState
from tidewise.strategy.trend import EwmacTrendStrategy

BAR_TIME = time(15, 0)


class TidewiseBacktestingEngine(BacktestingEngine):
    """覆盖 load_data：从内存 DataFrame 注入 K 线。"""

    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        super().__init__()
        self._frames = frames

    def load_data(self) -> None:
        self.history_data.clear()
        self.dts.clear()
        for vt_symbol in self.vt_symbols:
            symbol, exchange = extract_vt_symbol(vt_symbol)
            df = self._frames[vt_symbol]
            for r in df.itertuples(index=False):
                dt = datetime.combine(r.date, BAR_TIME)
                if dt < self.start or dt > self.end:
                    continue
                bar = BarData(
                    symbol=symbol,
                    exchange=exchange,
                    datetime=dt,
                    interval=Interval.DAILY,
                    volume=float(r.volume),
                    open_interest=float(r.open_interest),
                    open_price=float(r.open),
                    high_price=float(r.high),
                    low_price=float(r.low),
                    close_price=float(r.close),
                    gateway_name="BACKTEST",
                )
                self.dts.add(dt)
                self.history_data[(dt, vt_symbol)] = bar
        # vn.py 以 datetime(1970,1,1) 为初值统计预热天数，首根 K 线会被多计 1 天，
        # 导致实际预热比 load_bars(days) 少一根；置空使预热恰为 days 根。
        self.datetime = None

    def output(self, msg: object) -> None:  # 静默 vn.py 进度输出，统一由报告呈现
        self.logs.append(str(msg))


@dataclass(frozen=True)
class InstrumentCost:
    vt_symbol: str
    multiplier: float
    price_tick: float
    rate: float
    slippage: float  # 单位价格滑点（vn.py 口径：每手成本 = slippage × 乘数）


@dataclass(frozen=True)
class BacktestResult:
    stats: dict
    daily: pd.DataFrame
    roll_cost_total: float
    roll_count: int
    costs: dict[str, InstrumentCost]
    trading_start: date
    end: date
    trades: pd.DataFrame  # date, vt_symbol, direction, offset, price, volume


def _instrument_cost(
    inst: InstrumentConfig, df: pd.DataFrame, slippage_ticks: float
) -> InstrumentCost:
    # vn.py 按「成交价（复权价）× 乘数 × 费率」收费；复权价与真实价存在价差/平移，
    # 用两者中位数之比折算，使每手手续费≈真实价格下的水平。
    ref_raw = float(df["raw_close"].median())
    ref_adj = float(df["close"].median())
    return InstrumentCost(
        vt_symbol=inst.continuous_vt_symbol,
        multiplier=inst.multiplier,
        price_tick=inst.price_tick,
        rate=inst.commission_rate(ref_raw) * ref_raw / ref_adj,
        slippage=slippage_ticks * inst.price_tick,
    )


def run_backtest(cfg: AppConfig, products: list[str] | None = None) -> BacktestResult:
    instruments = cfg.instruments(products)
    store = ContinuousStore(cfg.storage.continuous_dir)
    carry_store = CarryStore(cfg.storage.carry_dir)
    frames = {i.continuous_vt_symbol: store.load(i.product) for i in instruments}
    carry_series: dict[str, dict] = {}
    for i in instruments:
        try:
            c = carry_store.load(i.product)
        except FileNotFoundError as e:
            raise FileNotFoundError(f"{e}（carry 与连续合约一起由 data update 生成）") from e
        carry_series[i.continuous_vt_symbol] = dict(zip(c["date"], c["carry"], strict=True))
    costs = {
        i.continuous_vt_symbol: _instrument_cost(
            i, frames[i.continuous_vt_symbol], cfg.backtest.slippage_ticks
        )
        for i in instruments
    }

    bt = cfg.backtest
    all_dates = sorted({d for df in frames.values() for d in df["date"]})
    if not all_dates:
        raise ValueError("无可用数据")
    end = bt.end or all_dates[-1]
    warmup_days = sum(1 for d in all_dates if d < bt.start)
    # 预热下限 = carry 波动估计窗口；各 EWMAC 速度未就绪时自动被剔除（权重归一）
    min_warmup = CarryState().min_bars
    if warmup_days < min_warmup:
        raise ValueError(
            f"回测起点 {bt.start} 之前只有 {warmup_days} 个交易日，信号预热需要 ≥ {min_warmup} 日；"
            "请推迟 backtest.start 或提前 data.history_start_year"
        )

    engine = TidewiseBacktestingEngine(frames)
    engine.set_parameters(
        vt_symbols=list(frames),
        interval=Interval.DAILY,
        start=datetime.combine(all_dates[0], time.min),
        end=datetime.combine(end, time.max),
        rates={s: c.rate for s, c in costs.items()},
        slippages={s: c.slippage for s, c in costs.items()},
        sizes={s: c.multiplier for s, c in costs.items()},
        priceticks={s: c.price_tick for s, c in costs.items()},
        capital=bt.capital,
        annual_days=244,
    )
    engine.add_strategy(
        EwmacTrendStrategy,
        {
            "vol_lookback": cfg.sizing.vol_lookback_days,
            "vol_target": cfg.sizing.instrument_vol_target,
            "buffer_fraction": cfg.sizing.buffer_fraction,
            "capital": bt.capital,
            "warmup_days": warmup_days,
            "max_margin_usage": cfg.risk.max_margin_usage,
            "max_instrument_margin": cfg.risk.max_instrument_margin,
            "carry_series": carry_series,
            "margin_ratios": {i.continuous_vt_symbol: i.margin_ratio for i in instruments},
        },
    )
    engine.load_data()
    engine.run_backtesting()
    daily = engine.calculate_result()
    if daily is None or daily.empty:
        raise RuntimeError("回测无成交，无法统计：\n" + "\n".join(engine.logs[-20:]))
    bad = daily.index[~np.isfinite(daily["net_pnl"].to_numpy(dtype=float))]
    if len(bad):
        raise RuntimeError(f"逐日盈亏出现非有限值（数据问题），首个日期 {bad[0]}，共 {len(bad)} 日")
    if any("触发异常" in m for m in engine.logs):
        raise RuntimeError("回测中途异常终止：\n" + "\n".join(engine.logs[-20:]))

    roll_cost, roll_count, roll_by_date = _roll_costs(engine, frames, costs)
    daily = daily.copy()
    daily["roll_cost"] = pd.Series(roll_by_date).reindex(daily.index).fillna(0.0)
    daily["net_pnl"] = daily["net_pnl"] - daily["roll_cost"]
    stats = engine.calculate_statistics(daily, output=False)
    trading_start = next(d for d in all_dates if d >= bt.start)
    trades = pd.DataFrame(
        [
            {
                "date": t.datetime.date(),
                "vt_symbol": t.vt_symbol,
                "direction": t.direction.value,
                "offset": t.offset.value,
                "price": t.price,
                "volume": t.volume,
            }
            for t in engine.trades.values()
        ],
        columns=["date", "vt_symbol", "direction", "offset", "price", "volume"],
    )
    return BacktestResult(stats, daily, roll_cost, roll_count, costs, trading_start, end, trades)


def _roll_costs(
    engine: BacktestingEngine,
    frames: dict[str, pd.DataFrame],
    costs: dict[str, InstrumentCost],
) -> tuple[float, int, dict[date, float]]:
    """换月日持仓 × 2 腿 ×（手续费 + 滑点）。"""
    by_date: dict[date, float] = {}
    total, count = 0.0, 0
    results = engine.daily_results
    for vt_symbol, df in frames.items():
        c = costs[vt_symbol]
        for r in df[df["rolled"]].itertuples(index=False):
            dr = results.get(r.date)
            if dr is None:
                continue
            lots = abs(dr.start_poses.get(vt_symbol, 0))
            if not lots:
                continue
            per_leg = lots * c.multiplier * (r.raw_close * c.rate + c.slippage)
            cost = 2 * per_leg
            by_date[r.date] = by_date.get(r.date, 0.0) + cost
            total += cost
            count += 1
    return total, count, by_date


def write_report(result: BacktestResult, out_dir: Path, label: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = out_dir / f"backtest-{label}-{stamp}"
    result.daily.to_csv(base.with_suffix(".daily.csv"))
    s = result.stats

    def fmt(v: object) -> str:
        if isinstance(v, float):
            return "nan" if math.isnan(v) else f"{v:,.2f}"
        return str(v)

    rows = [
        ("回测区间", f"{result.trading_start} ~ {result.end}"),
        ("起始资金", fmt(s.get("capital"))),
        ("结束资金", fmt(s.get("end_balance"))),
        ("总收益率 %", fmt(s.get("total_return"))),
        ("年化收益率 %", fmt(s.get("annual_return"))),
        ("最大回撤 %", fmt(s.get("max_ddpercent"))),
        ("最长回撤天数", fmt(s.get("max_drawdown_duration"))),
        ("夏普比率", fmt(s.get("sharpe_ratio"))),
        ("收益回撤比", fmt(s.get("return_drawdown_ratio"))),
        ("总成交笔数", fmt(s.get("total_trade_count"))),
        ("总手续费", fmt(s.get("total_commission"))),
        ("总滑点", fmt(s.get("total_slippage"))),
        ("换月次数（有持仓）", fmt(result.roll_count)),
        ("换月成本", fmt(result.roll_cost_total)),
    ]
    lines = [f"# 回测报告：{label}", "", "| 指标 | 数值 |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in rows]
    lines += [
        "",
        "## 成本假设",
        "",
        "| 合约 | 乘数 | 最小变动 | 比例费率 | 滑点(价格) |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {c.vt_symbol} | {c.multiplier:g} | {c.price_tick:g} | {c.rate:.6f} | {c.slippage:g} |"
        for c in result.costs.values()
    ]
    lines += [
        "",
        "> 统计已扣除换月成本。数据源为新浪逐合约日线，主力按持仓量规则自行判定，加法后复权。",
    ]
    md = base.with_suffix(".md")
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    base.with_suffix(".stats.json").write_text(
        json.dumps(
            {k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in s.items()}
            | {
                "roll_cost_total": result.roll_cost_total,
                "roll_count": result.roll_count,
                "costs": {k: asdict(v) for k, v in result.costs.items()},
            },
            ensure_ascii=False,
            indent=1,
            default=str,
        ),
        encoding="utf-8",
    )
    return md


__all__ = ["BacktestResult", "TidewiseBacktestingEngine", "run_backtest", "write_report"]
