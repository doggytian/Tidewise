"""组合层风控纯函数：波动率目标 scalar + 分层保证金上限。

设计冻结于 2026-10-01（见 docs/ROADMAP.md A3），回测与实盘共用。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from tidewise.strategy.ewmac import ANNUALIZE


def vol_scalar(
    daily_pnls: Sequence[float],
    equity: float,
    annual_vol_target: float,
    lookback: int,
    max_scalar: float = 1.0,
) -> float:
    """策略自身实现波动相对目标的比例；>1 时取倒数缩减（只缩不放）。

    样本不足 lookback 或波动为 0 时返回 max_scalar（不干预）。
    """
    if len(daily_pnls) < lookback or equity <= 0:
        return max_scalar
    window = list(daily_pnls)[-lookback:]
    n = len(window)
    mean = sum(window) / n
    var = sum((x - mean) ** 2 for x in window) / (n - 1)
    realized_daily = math.sqrt(var)
    if realized_daily <= 0:
        return max_scalar
    target_daily = equity * annual_vol_target / ANNUALIZE
    return min(max_scalar, target_daily / realized_daily)


def cap_by_margins(
    targets: Mapping[str, int],
    prices: Mapping[str, float],
    sizes: Mapping[str, float],
    ratios: Mapping[str, float],
    clusters: Mapping[str, str],
    equity: float,
    instrument_cap: float,
    cluster_cap: float,
    total_cap: float,
) -> dict[str, int]:
    """分层保证金上限：单品种 → 板块 → 组合。超限层按比例向零缩减该层内全部目标。"""
    result = dict(targets)

    def usage(s: str, lots: int) -> float:
        return abs(lots) * prices.get(s, 0.0) * sizes.get(s, 0.0) * ratios.get(s, 0.0)

    def scale_down(scope: list[str], cap_cash: float) -> None:
        used = sum(usage(s, result[s]) for s in scope)
        if used > cap_cash > 0:
            k = cap_cash / used
            for s in scope:
                t = result[s]
                result[s] = int(abs(t) * k) * (1 if t >= 0 else -1)

    for s in list(result):
        cap_cash = equity * instrument_cap
        if usage(s, result[s]) > cap_cash > 0:
            result[s] = int(abs(result[s]) * cap_cash / usage(s, result[s])) * (
                1 if result[s] >= 0 else -1
            )

    by_cluster: dict[str, list[str]] = {}
    for s in result:
        by_cluster.setdefault(clusters.get(s) or s, []).append(s)
    for members in by_cluster.values():
        scale_down(members, equity * cluster_cap)

    scale_down(list(result), equity * total_cap)
    return result
