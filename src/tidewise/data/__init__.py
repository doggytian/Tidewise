"""数据层：新浪逐合约日线 → 主力判定 → 加法后复权连续合约。"""

from tidewise.data.carry import CarryStore, build_carry
from tidewise.data.continuous import RollEvent, build_continuous, select_dominant
from tidewise.data.pipeline import ContinuousStore, ProductDataSummary, update_product
from tidewise.data.sina import DataSourceError, RawBarStore
from tidewise.data.symbols import ContractId, parse_sina

__all__ = [
    "CarryStore",
    "ContinuousStore",
    "ContractId",
    "DataSourceError",
    "ProductDataSummary",
    "RawBarStore",
    "RollEvent",
    "build_carry",
    "build_continuous",
    "parse_sina",
    "select_dominant",
    "update_product",
]
