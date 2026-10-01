"""每日决策报告：信号 → 目标手数（实际合约）→ 与持仓比对 → 调仓清单 + 风险占用。

口径与回测完全一致：同一决策链（DecisionReplayer）、同一主力判定、同一风控上限。
输出 Markdown 到控制台与 var/reports/daily-*.md。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd
from vnpy.trader.constant import Direction, Offset

from tidewise.config.schema import AppConfig, InstrumentConfig
from tidewise.data.carry import CarryStore
from tidewise.data.pipeline import ContinuousStore
from tidewise.live.positions import Positions
from tidewise.plan import LegReason, PlannedOrder
from tidewise.strategy.replay import DailyDecision, DecisionReplayer


def _diff_orders(contract: str, held: int, target: int) -> list[PlannedOrder]:
    """净持仓调整到目标：先平后开（与 vn.py rebalance_portfolio 同序）。"""
    diff = target - held
    if diff == 0:
        return []
    out: list[PlannedOrder] = []
    if diff > 0:  # 需要更多多头：先买平空头，再买入开仓
        cover = min(diff, -held) if held < 0 else 0
        if cover:
            out.append(
                PlannedOrder(contract, Direction.LONG, Offset.CLOSE, cover, LegReason.REBALANCE)
            )
        if diff - cover:
            out.append(
                PlannedOrder(
                    contract, Direction.LONG, Offset.OPEN, diff - cover, LegReason.REBALANCE
                )
            )
    else:  # 需要更多空头：先卖平多头，再卖出开仓
        sell = min(-diff, held) if held > 0 else 0
        if sell:
            out.append(
                PlannedOrder(contract, Direction.SHORT, Offset.CLOSE, sell, LegReason.REBALANCE)
            )
        if -diff - sell:
            out.append(
                PlannedOrder(
                    contract, Direction.SHORT, Offset.OPEN, -diff - sell, LegReason.REBALANCE
                )
            )
    return out


@dataclass(frozen=True)
class InstrumentLine:
    product: str
    contract: str  # 当前主力（实际可交易合约）
    forecast: float | None
    target: int  # 目标手数（正=多）
    held: int  # 当前持仓（净）
    held_contract: str | None
    price: float
    margin: float  # 目标保证金占用（元）
    note: str = ""


@dataclass(frozen=True)
class DailyReport:
    signal_date: date
    generated_at: datetime
    lines: list[InstrumentLine]
    orders: list[PlannedOrder]
    scalar: float
    margin_usage: float
    positions_as_of: date
    warnings: list[str]

    @property
    def is_empty(self) -> bool:
        return not self.orders


def build_daily_report(cfg: AppConfig, positions: Positions, today: date) -> DailyReport:
    instruments = cfg.instruments()
    store = ContinuousStore(cfg.storage.continuous_dir)
    carry_store = CarryStore(cfg.storage.carry_dir)
    frames: dict[str, pd.DataFrame] = {}
    carry: dict[str, dict[date, float]] = {}
    warnings: list[str] = []
    ready: list[InstrumentConfig] = []
    for inst in instruments:
        try:
            df = store.load(inst.product)
            c = carry_store.load(inst.product)
        except FileNotFoundError:
            warnings.append(f"{inst.product}: 缺数据，已跳过（先运行 tidewise data update）")
            continue
        frames[inst.continuous_vt_symbol] = df
        carry[inst.continuous_vt_symbol] = dict(zip(c["date"], c["carry"], strict=True))
        ready.append(inst)
    instruments = ready
    if not instruments:
        raise FileNotFoundError("没有任何品种有数据，请先运行 tidewise data update")
    for inst in instruments:
        last = df["date"].iloc[-1]
        if (today - last).days > 4:
            warnings.append(f"{inst.product}: 数据停在 {last}，请先 tidewise data update")

    replayer = DecisionReplayer(cfg, instruments)
    decisions = replayer.replay(frames, carry)
    signal_date = decisions[-1].date

    lines: list[InstrumentLine] = []
    orders: list[PlannedOrder] = []
    # 各品种数据可能不齐（节假日期间各源更新节奏不同），按各自最后数据日取决策
    latest_dec: dict[str, DailyDecision] = {}
    for dec in decisions:
        for s in dec.targets:
            if s in frames and dec.date == frames[s]["date"].max():
                latest_dec[s] = dec
    for inst in instruments:
        vt = inst.continuous_vt_symbol
        df = frames[vt]
        last_row = df.iloc[-1]
        contract = str(last_row["contract"])
        dec = latest_dec.get(vt)
        if dec is None or dec.date != df["date"].iloc[-1]:
            warnings.append(f"{inst.product}: 无有效决策（数据异常），已跳过")
            continue
        if dec.date < signal_date:
            warnings.append(f"{inst.product}: 数据停在 {dec.date}（其他品种到 {signal_date}）")
        target = dec.targets.get(vt, 0)
        forecast = dec.forecasts.get(vt)
        price = float(last_row["raw_close"])
        margin = abs(target) * price * inst.multiplier * inst.margin_ratio

        # 持仓：该品种所有合约的净持仓
        held_by_contract = {
            c: q for c, q in positions.net.items() if c.startswith(inst.product.upper())
        }
        held = sum(held_by_contract.values())
        held_contract = next(iter(held_by_contract), None)

        note = ""
        # 换月：持仓合约不是当前主力 → 平旧开新
        if held_contract and held_contract != contract and held != 0:
            note = f"换月：{held_contract} → {contract}"
            side = Direction.SHORT if held > 0 else Direction.LONG
            orders.append(
                PlannedOrder(held_contract, side, Offset.CLOSE, abs(held), LegReason.ROLL_CLOSE)
            )
            if target:
                side2 = Direction.LONG if target > 0 else Direction.SHORT
                orders.append(
                    PlannedOrder(contract, side2, Offset.OPEN, abs(target), LegReason.ROLL_OPEN)
                )
        else:
            orders.extend(_diff_orders(contract, held, target))

        lines.append(
            InstrumentLine(
                product=inst.product,
                contract=contract,
                forecast=forecast,
                target=target,
                held=held,
                held_contract=held_contract,
                price=price,
                margin=margin,
                note=note,
            )
        )

    last_scalar = decisions[-1].scalar
    margin_total = sum(ln.margin for ln in lines) / cfg.backtest.capital
    return DailyReport(
        signal_date=signal_date,
        generated_at=datetime.now(),
        lines=lines,
        orders=orders,
        scalar=last_scalar,
        margin_usage=margin_total,
        positions_as_of=positions.as_of,
        warnings=warnings,
    )
