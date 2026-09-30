"""主力合约判定 + 加法后复权连续合约。

主力规则（回测与实盘共用，只看 T 日及以前数据）：
1. 合格合约：当日有行情，且未到强制换月日（交割月前一个月的 force_roll_day 日）。
2. 主力只向远月切换，不回切。
3. 远月合约在合格合约中持仓量连续 confirm_days 日第一 → 切换。
4. 当前主力到达强制换月日 → 立即切到合格合约中持仓量最大的远月。

T 日收盘判定的主力，在 T+1 交易时段持有；因此连续序列第 d 日的价格来自 d-1 收盘时判定的合约。
复权：切换日用两个合约在判定日的收盘价差做加法调整，保证持仓盈亏（点数 × 乘数）与真实一致；
序列锚定最新合约（最新价格 = 真实价格）。长期贴水品种（如铁矿）累积价差可能使早期价格为负，
此时整体上移一个常数——价差、盈亏、EWMAC 信号均不受影响。真实价格始终保存在 raw_close，
定仓与保证金计算必须用 raw_close。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import pandas as pd

from tidewise.data.symbols import ContractId, parse_sina

CONTINUOUS_COLUMNS = [
    "date",
    "contract",
    "open",
    "high",
    "low",
    "close",
    "raw_close",
    "volume",
    "open_interest",
    "rolled",
]


@dataclass(frozen=True)
class RollEvent:
    decided_on: date  # 判定日（T），T+1 起持有新合约
    from_contract: str
    to_contract: str
    gap: float  # to.close - from.close（判定日）
    forced: bool


def select_dominant(
    product: str,
    raw: pd.DataFrame,
    confirm_days: int,
    force_roll_day: int,
) -> tuple[pd.Series, list[RollEvent]]:
    """返回 (每个交易日收盘后判定的主力合约, 换月事件)。"""
    if raw.empty:
        return pd.Series(dtype=object), []
    ids: dict[str, ContractId] = {s: parse_sina(product, s) for s in raw["contract"].unique()}
    force_date = {s: cid.force_roll_date(force_roll_day) for s, cid in ids.items()}
    oi = raw.pivot_table(index="date", columns="contract", values="open_interest", aggfunc="last")
    close = raw.pivot_table(index="date", columns="contract", values="close", aggfunc="last")

    current: str | None = None
    streak_contract: str | None = None
    streak = 0
    decided: dict[date, str] = {}
    events: list[RollEvent] = []

    for d, row in oi.iterrows():
        present = row.dropna()
        eligible = {s: v for s, v in present.items() if d < force_date[s]}
        if current is None:
            if eligible:
                current = max(eligible, key=eligible.get)
                decided[d] = current
            continue
        if current not in present:
            # 数据源缺口（当日主力无行情）：不做任何判定，维持原主力，连续计数不中断也不累加
            decided[d] = current
            continue

        farther = {s: v for s, v in eligible.items() if ids[s].expiry_key > ids[current].expiry_key}
        leader = max(eligible, key=eligible.get) if eligible else None
        new: str | None = None
        forced = False

        if d >= force_date[current]:
            if farther:
                new, forced = max(farther, key=farther.get), True
        elif leader is not None and leader in farther:
            streak = streak + 1 if leader == streak_contract else 1
            streak_contract = leader
            if streak >= confirm_days:
                new = leader
        else:
            streak_contract, streak = None, 0

        if new is not None:
            gap = float(close.at[d, new] - close.at[d, current])
            if not math.isfinite(gap):
                raise ValueError(f"{product} {d}: 换月价差无效 {current}→{new}")
            events.append(RollEvent(d, current, new, gap, forced))
            current, streak_contract, streak = new, None, 0
        decided[d] = current

    return pd.Series(decided, name="contract"), events


def build_continuous(
    product: str,
    raw: pd.DataFrame,
    confirm_days: int = 3,
    force_roll_day: int = 15,
) -> tuple[pd.DataFrame, list[RollEvent]]:
    dominant, events = select_dominant(product, raw, confirm_days, force_roll_day)
    if dominant.empty:
        return pd.DataFrame(columns=CONTINUOUS_COLUMNS), events

    held = dominant.shift(1).dropna()  # d 日持有 d-1 收盘判定的合约
    bars = raw.set_index(["date", "contract"])
    keys = [(d, c) for d, c in held.items() if (d, c) in bars.index]
    df = bars.loc[keys].reset_index()
    df["raw_close"] = df["close"]
    df["rolled"] = df["contract"].ne(df["contract"].shift(1)) & df.index.to_series().gt(0)

    # 加法后复权：从最新往回累加每次换月的价差
    adj = pd.Series(0.0, index=df.index)
    gap_by_first_day = {}
    for e in events:
        after = df.index[(df["date"] > e.decided_on) & (df["contract"] == e.to_contract)]
        if len(after):
            gap_by_first_day[after[0]] = e.gap
    cumulative = 0.0
    for i in range(len(df) - 1, -1, -1):
        adj.iat[i] = cumulative
        if i in gap_by_first_day:
            cumulative += gap_by_first_day[i]
    for c in ("open", "high", "low", "close"):
        df[c] = df[c] + adj

    min_low = float(df["low"].min())
    if min_low <= 0:
        shift = float(raw["low"].min()) - min_low
        for c in ("open", "high", "low", "close"):
            df[c] = df[c] + shift
    return df[CONTINUOUS_COLUMNS].reset_index(drop=True), events
