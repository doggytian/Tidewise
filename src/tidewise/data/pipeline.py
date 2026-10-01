"""数据流水线：逐合约原始日线 → 主力判定 → 连续合约落盘。"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from tidewise.config.schema import AppConfig, InstrumentConfig
from tidewise.data.carry import CarryStore, build_carry
from tidewise.data.continuous import RollEvent, build_continuous
from tidewise.data.sina import Fetcher, RawBarStore, UpdateReport, fetch_sina_contract

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProductDataSummary:
    product: str
    raw: UpdateReport
    bars: int
    first_date: date | None
    last_date: date | None
    current_contract: str | None
    rolls: int


class ContinuousStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, product: str) -> Path:
        return self.root / f"{product}.parquet"

    def rolls_path(self, product: str) -> Path:
        return self.root / f"{product}.rolls.json"

    def save(self, product: str, df: pd.DataFrame, events: list[RollEvent]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        df.to_parquet(self.path(product), index=False)
        payload = [{**asdict(e), "decided_on": e.decided_on.isoformat()} for e in events]
        self.rolls_path(product).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def load(self, product: str) -> pd.DataFrame:
        p = self.path(product)
        if not p.exists():
            raise FileNotFoundError(f"{product}: 缺少连续合约数据，请先运行 tidewise data update")
        df = pd.read_parquet(p)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        return df


def update_product(
    cfg: AppConfig,
    inst: InstrumentConfig,
    today: date,
    fetch: Fetcher = fetch_sina_contract,
    pause_s: float = 0.2,
) -> ProductDataSummary:
    raw_store = RawBarStore(cfg.storage.raw_bars_dir)
    report = raw_store.update(
        inst.product, cfg.data.history_start_year, today, fetch=fetch, pause_s=pause_s
    )
    raw = raw_store.load(inst.product)
    df, events = build_continuous(
        inst.product, raw, cfg.data.roll_confirm_days, cfg.data.force_roll_day
    )
    ContinuousStore(cfg.storage.continuous_dir).save(inst.product, df, events)
    CarryStore(cfg.storage.carry_dir).save(
        inst.product,
        build_carry(inst.product, raw, cfg.data.roll_confirm_days, cfg.data.force_roll_day),
    )
    return ProductDataSummary(
        product=inst.product,
        raw=report,
        bars=len(df),
        first_date=df["date"].iloc[0] if len(df) else None,
        last_date=df["date"].iloc[-1] if len(df) else None,
        current_contract=events[-1].to_contract
        if events
        else (df["contract"].iloc[-1] if len(df) else None),
        rolls=len(events),
    )
