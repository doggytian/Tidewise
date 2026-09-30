"""合约代码规范：内部统一用 ContractId(product, 四位年, 月)，按需转换为新浪/vn.py 格式。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from vnpy.trader.constant import Exchange

_SINA_RE = re.compile(r"^([A-Z]{1,2})(\d{2})(\d{2})$")


@dataclass(frozen=True)
class ContractId:
    product: str  # 与配置一致的交易所大小写，如 rb / MA / IF
    year: int
    month: int

    def __post_init__(self) -> None:
        if not 1 <= self.month <= 12:
            raise ValueError(f"非法月份: {self.month}")
        if not 2000 <= self.year <= 2099:
            raise ValueError(f"非法年份: {self.year}")

    @property
    def expiry_key(self) -> tuple[int, int]:
        return (self.year, self.month)

    @property
    def sina_symbol(self) -> str:
        """新浪统一用大写品种 + 四位年月（郑商所也是 4 位）。"""
        return f"{self.product.upper()}{self.year % 100:02d}{self.month:02d}"

    def vnpy_symbol(self, exchange: Exchange) -> str:
        """CTP/vn.py 规范：郑商所 3 位（年份个位 + 月），其余 4 位。"""
        if exchange is Exchange.CZCE:
            return f"{self.product}{self.year % 10}{self.month:02d}"
        return f"{self.product}{self.year % 100:02d}{self.month:02d}"

    def force_roll_date(self, day: int) -> date:
        """交割月前一个月的指定日：个人账户不能持仓进入交割月。"""
        y, m = (self.year - 1, 12) if self.month == 1 else (self.year, self.month - 1)
        return date(y, m, day)

    def __str__(self) -> str:
        return self.sina_symbol


def parse_sina(product: str, sina_symbol: str) -> ContractId:
    m = _SINA_RE.match(sina_symbol)
    if not m or m.group(1) != product.upper():
        raise ValueError(f"{sina_symbol!r} 不是品种 {product} 的新浪合约代码")
    return ContractId(product, 2000 + int(m.group(2)), int(m.group(3)))
