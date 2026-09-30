"""调仓单与其状态机：系统唯一的跨进程状态（vn.py 没有"确认后才下单"环节，此处自建）。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import StrEnum

from vnpy.trader.constant import Direction, Offset


class PlanStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXECUTING = "EXECUTING"
    DONE = "DONE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return not ALLOWED_TRANSITIONS[self]


class LegReason(StrEnum):
    REBALANCE = "REBALANCE"
    ROLL_CLOSE = "ROLL_CLOSE"
    ROLL_OPEN = "ROLL_OPEN"
    EXIT = "EXIT"


_S = PlanStatus

ALLOWED_TRANSITIONS: dict[PlanStatus, frozenset[PlanStatus]] = {
    _S.DRAFT: frozenset({_S.PENDING_APPROVAL, _S.DONE}),
    _S.PENDING_APPROVAL: frozenset({_S.APPROVED, _S.REJECTED, _S.EXPIRED}),
    _S.APPROVED: frozenset({_S.EXECUTING, _S.EXPIRED}),
    _S.EXECUTING: frozenset({_S.DONE, _S.PARTIAL, _S.FAILED}),
    _S.REJECTED: frozenset(),
    _S.EXPIRED: frozenset(),
    _S.DONE: frozenset(),
    _S.PARTIAL: frozenset(),
    _S.FAILED: frozenset(),
}


class InvalidTransitionError(RuntimeError):
    pass


@dataclass(frozen=True)
class TargetPosition:
    """某实际合约的目标净持仓（正=多，负=空）。"""

    vt_symbol: str
    volume: int
    note: str = ""


@dataclass(frozen=True)
class PlannedOrder:
    """供审批展示的预期委托；实际下单由 vn.py rebalance_portfolio 按仓差生成。"""

    vt_symbol: str
    direction: Direction
    offset: Offset
    volume: int
    reason: LegReason

    def __post_init__(self) -> None:
        if self.volume <= 0:
            raise ValueError(f"{self.vt_symbol}: 委托手数必须 > 0")


@dataclass(frozen=True)
class StatusChange:
    from_status: PlanStatus
    to_status: PlanStatus
    at: datetime
    note: str = ""


@dataclass(frozen=True)
class RebalancePlan:
    """signal_date：生成信号所用 K 线的交易日；执行发生在其后的首个交易时段。"""

    plan_id: str
    signal_date: date
    created_at: datetime
    targets: tuple[TargetPosition, ...]
    orders: tuple[PlannedOrder, ...]
    status: PlanStatus = PlanStatus.DRAFT
    history: tuple[StatusChange, ...] = field(default=())

    @classmethod
    def new(
        cls,
        signal_date: date,
        created_at: datetime,
        targets: list[TargetPosition],
        orders: list[PlannedOrder],
    ) -> RebalancePlan:
        symbols = [t.vt_symbol for t in targets]
        if len(symbols) != len(set(symbols)):
            raise ValueError("同一合约在调仓单中只能有一个目标")
        return cls(
            plan_id=f"{signal_date:%Y%m%d}-{uuid.uuid4().hex[:8]}",
            signal_date=signal_date,
            created_at=created_at,
            targets=tuple(targets),
            orders=tuple(orders),
        )

    @property
    def is_empty(self) -> bool:
        return not self.orders


def transition(
    plan: RebalancePlan, to: PlanStatus, *, at: datetime, note: str = ""
) -> RebalancePlan:
    """返回新状态的调仓单；非法流转抛 InvalidTransitionError。"""
    if to not in ALLOWED_TRANSITIONS[plan.status]:
        raise InvalidTransitionError(f"{plan.plan_id}: {plan.status} → {to} 不允许")
    if plan.status is PlanStatus.DRAFT and to is PlanStatus.DONE and not plan.is_empty:
        raise InvalidTransitionError(f"{plan.plan_id}: 非空调仓单不能跳过审批直接完成")
    change = StatusChange(plan.status, to, at, note)
    return replace(plan, status=to, history=(*plan.history, change))
