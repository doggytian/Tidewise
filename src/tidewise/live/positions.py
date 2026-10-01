"""持仓文件：var/positions.yaml，手工维护（后续接 CTP 只读查询）。

格式：
    as_of: 2026-09-30          # 持仓对应的交易日（防止用错日期的旧文件）
    positions:
      rb2701: 2                # 正=多，负=空；净持仓
      MA601: -1
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml


class PositionsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Positions:
    as_of: date
    net: dict[str, int]  # 合约代码（新浪格式大写）→ 净手数


def load_positions(path: Path) -> Positions:
    if not path.exists():
        return Positions(date.min, {})
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise PositionsError(f"{path}: 顶层必须是映射")
    if not isinstance(raw.get("as_of"), date):
        raise PositionsError(f"{path}: 缺少 as_of 日期（如 as_of: 2026-09-30）")
    positions = raw.get("positions") or {}
    if not isinstance(positions, dict):
        raise PositionsError(f"{path}: positions 必须是映射")
    net: dict[str, int] = {}
    for contract, qty in positions.items():
        if not isinstance(qty, int):
            raise PositionsError(f"{contract}: 手数必须是整数（正=多，负=空），得到 {qty!r}")
        # 校验合约代码格式（品种字母 + 3/4 位年月）
        sym = str(contract).upper()
        if not re.match(r"^[A-Z]{1,2}\d{3,4}$", sym):
            raise PositionsError(f"无法识别的合约代码: {contract!r}")
        net[sym] = qty
    return Positions(raw["as_of"], net)


def save_template(path: Path, as_of: date, contracts: list[str]) -> None:
    """生成持仓文件模板（不存在时）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"as_of: {as_of.isoformat()}", "positions:", "  # 合约: 净手数（正=多，负=空）"]
    lines += [f"  # {c}: 0" for c in contracts]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
