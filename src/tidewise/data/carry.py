"""carry（展期收益）序列：近月（当日主力）与次远月的年化价差。

口径与连续合约一致：T 日收盘判定的主力在 T+1 持有，carry 也用 T 日收盘价计算，
策略在 T+1 开盘交易。近月 = 主力；次远月 = 比主力更远月、当日有行情、持仓量最大的合约。
carry = (close_near / close_far - 1) × 365 / 到期月相隔天数。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from tidewise.data.continuous import select_dominant
from tidewise.data.symbols import ContractId, parse_sina

CARRY_COLUMNS = ["date", "near", "far", "carry"]


def _days_between(a: ContractId, b: ContractId) -> int:
    return ((b.year - a.year) * 12 + (b.month - a.month)) * 30


def build_carry(
    product: str,
    raw: pd.DataFrame,
    confirm_days: int = 3,
    force_roll_day: int = 15,
) -> pd.DataFrame:
    """返回每日 carry（实数）；无合格次远月的日子缺行。"""
    if raw.empty:
        return pd.DataFrame(columns=CARRY_COLUMNS)
    dominant, _ = select_dominant(product, raw, confirm_days, force_roll_day)
    if dominant.empty:
        return pd.DataFrame(columns=CARRY_COLUMNS)
    ids: dict[str, ContractId] = {s: parse_sina(product, s) for s in raw["contract"].unique()}
    close = raw.pivot_table(index="date", columns="contract", values="close", aggfunc="last")
    oi = raw.pivot_table(index="date", columns="contract", values="open_interest", aggfunc="last")

    rows = []
    for d, near in dominant.items():
        nearer_key = ids[near].expiry_key
        day_close, day_oi = close.loc[d], oi.loc[d]
        farther = [
            s
            for s, oi_v in day_oi.dropna().items()
            if ids[s].expiry_key > nearer_key and pd.notna(day_close[s])
        ]
        if not farther or pd.isna(day_close.get(near)):
            continue
        far = max(farther, key=lambda s: day_oi[s])
        days = _days_between(ids[near], ids[far])
        if days <= 0:
            continue
        carry = (day_close[near] / day_close[far] - 1.0) * 365.0 / days
        rows.append({"date": d, "near": near, "far": far, "carry": carry})
    return pd.DataFrame(rows, columns=CARRY_COLUMNS)


class CarryStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, product: str) -> Path:
        return self.root / f"{product}.parquet"

    def save(self, product: str, df: pd.DataFrame) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        df.to_parquet(self.path(product), index=False)

    def load(self, product: str) -> pd.DataFrame:
        p = self.path(product)
        if not p.exists():
            raise FileNotFoundError(f"{product}: 缺少 carry 数据，请先运行 tidewise data update")
        df = pd.read_parquet(p)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        return df
