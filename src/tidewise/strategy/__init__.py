"""策略层：纯函数信号/定仓 + vn.py 组合策略封装。"""

from tidewise.strategy.ewmac import (
    EwmacParams,
    EwmacState,
    buffered_target,
    optimal_position,
)

__all__ = ["EwmacParams", "EwmacState", "buffered_target", "optimal_position"]
