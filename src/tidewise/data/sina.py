"""新浪逐合约日线（经 akshare）+ 本地 Parquet 增量缓存。

新浪只提供约 2018 年之后上市的合约；已到期合约数据不再变化，下载一次后永久缓存。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

from tidewise.data.symbols import ContractId, parse_sina

log = logging.getLogger(__name__)

RAW_COLUMNS = ["contract", "date", "open", "high", "low", "close", "volume", "open_interest"]

Fetcher = Callable[[str], "pd.DataFrame | None"]


class DataSourceError(RuntimeError):
    pass


def fetch_sina_contract(sina_symbol: str) -> pd.DataFrame | None:
    """返回新浪原始日线；合约不存在返回 None；网络/格式异常抛 DataSourceError。"""
    import akshare as ak

    try:
        return ak.futures_zh_daily_sina(symbol=sina_symbol)
    except ValueError as e:
        # akshare 对不存在的合约抛 "Length mismatch"；其他 ValueError（如 JSON 解码）视为临时故障
        if "Length mismatch" in str(e):
            return None
        raise DataSourceError(f"{sina_symbol}: {e}") from e
    except Exception as e:  # noqa: BLE001 - 网络层异常类型众多，统一转为数据源错误
        raise DataSourceError(f"{sina_symbol}: {type(e).__name__}: {e}") from e


def clean_sina_frame(raw: pd.DataFrame, contract: str) -> tuple[pd.DataFrame, int]:
    """标准化列并剔除无效行，返回 (数据, 剔除行数)。"""
    df = raw.rename(columns={"hold": "open_interest"})
    missing = {"date", "open", "high", "low", "close", "volume", "open_interest"} - set(df.columns)
    if missing:
        raise DataSourceError(f"{contract}: 数据缺少列 {sorted(missing)}")
    df = df[["date", "open", "high", "low", "close", "volume", "open_interest"]].copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    for c in ("open", "high", "low", "close", "volume", "open_interest"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    valid = (
        df[["open", "high", "low", "close"]].gt(0).all(axis=1)
        & (df["high"] >= df[["open", "close", "low"]].max(axis=1))
        & (df["low"] <= df[["open", "close"]].min(axis=1))
        & df["volume"].ge(0)
        & df["open_interest"].ge(0)
    )
    dropped = int((~valid).sum())
    df = df[valid].drop_duplicates("date", keep="last").sort_values("date")
    df.insert(0, "contract", contract)
    return df.reset_index(drop=True), dropped


@dataclass
class Manifest:
    absent: set[str] = field(default_factory=set)  # 已确认不存在（仅限已过去的月份）
    complete: set[str] = field(default_factory=set)  # 已到期且已完整下载

    @classmethod
    def load(cls, path: Path) -> Manifest:
        if not path.exists():
            return cls()
        d = json.loads(path.read_text(encoding="utf-8"))
        return cls(absent=set(d.get("absent", [])), complete=set(d.get("complete", [])))

    def save(self, path: Path) -> None:
        payload = {"absent": sorted(self.absent), "complete": sorted(self.complete)}
        path.write_text(json.dumps(payload, indent=1), encoding="utf-8")


@dataclass(frozen=True)
class UpdateReport:
    product: str
    fetched: int
    contracts: int
    rows: int
    dropped_rows: int


class RawBarStore:
    """每品种一个 Parquet（全部合约逐日数据）+ 一个 manifest。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _paths(self, product: str) -> tuple[Path, Path]:
        return self.root / f"{product}.parquet", self.root / f"{product}.manifest.json"

    def load(self, product: str) -> pd.DataFrame:
        path, _ = self._paths(product)
        if not path.exists():
            return pd.DataFrame(columns=RAW_COLUMNS)
        df = pd.read_parquet(path)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        return df

    def merge_exchange(self, product: str, exchange_rows: pd.DataFrame) -> pd.DataFrame:
        """并入交易所官方日线；重叠 (contract, date) 以交易所数据为准。返回合并后数据。"""
        if exchange_rows.empty:
            return self.load(product)
        existing = self.load(product)
        # exchange_rows 在前 + keep="first" → 交易所优先
        merged = (
            pd.concat([exchange_rows, existing], ignore_index=True)
            .drop_duplicates(["contract", "date"], keep="first")
            .sort_values(["contract", "date"])
            .reset_index(drop=True)
        )
        path, _ = self._paths(product)
        merged.to_parquet(path, index=False)
        return merged

    def update(
        self,
        product: str,
        start_year: int,
        today: date,
        fetch: Fetcher = fetch_sina_contract,
        pause_s: float = 0.2,
    ) -> UpdateReport:
        self.root.mkdir(parents=True, exist_ok=True)
        path, manifest_path = self._paths(product)
        manifest = Manifest.load(manifest_path)
        frames = {c: g for c, g in self.load(product).groupby("contract")} if path.exists() else {}
        now_key = (today.year, today.month)
        fetched = dropped_total = 0

        for year in range(start_year, today.year + 2):
            for month in range(1, 13):
                cid = ContractId(product, year, month)
                sym = cid.sina_symbol
                if sym in manifest.absent or sym in manifest.complete:
                    continue
                if cid.expiry_key > (today.year + 1, 12):
                    continue
                raw = fetch(sym)
                fetched += 1
                if pause_s:
                    time.sleep(pause_s)
                expired = cid.expiry_key < now_key
                if raw is None or raw.empty:
                    if expired:
                        manifest.absent.add(sym)
                    continue
                df, dropped = clean_sina_frame(raw, sym)
                dropped_total += dropped
                if dropped:
                    log.warning("%s: 剔除 %d 行无效数据", sym, dropped)
                if df.empty:
                    continue
                frames[sym] = df
                if expired:
                    manifest.complete.add(sym)

        all_rows = (
            pd.concat(frames.values(), ignore_index=True)
            if frames
            else pd.DataFrame(columns=RAW_COLUMNS)
        )
        all_rows = all_rows.sort_values(["contract", "date"]).reset_index(drop=True)
        all_rows.to_parquet(path, index=False)
        manifest.save(manifest_path)
        return UpdateReport(product, fetched, len(frames), len(all_rows), dropped_total)


def contract_ids(product: str, raw: pd.DataFrame) -> dict[str, ContractId]:
    return {s: parse_sina(product, s) for s in raw["contract"].unique()}
