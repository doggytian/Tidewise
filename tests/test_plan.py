from datetime import date, datetime

import pytest
from vnpy.trader.constant import Direction, Offset

from tidewise.plan import (
    ALLOWED_TRANSITIONS,
    InvalidTransitionError,
    LegReason,
    PlannedOrder,
    PlanStatus,
    RebalancePlan,
    TargetPosition,
    transition,
)

T0 = datetime(2026, 9, 29, 15, 30)
S = PlanStatus


def _plan(legs: bool = True) -> RebalancePlan:
    targets = [TargetPosition("rb2601.SHFE", 2)]
    orders = (
        [PlannedOrder("rb2601.SHFE", Direction.LONG, Offset.OPEN, 2, LegReason.REBALANCE)]
        if legs
        else []
    )
    return RebalancePlan.new(date(2026, 9, 29), T0, targets, orders)


def test_order_volume_positive():
    with pytest.raises(ValueError):
        PlannedOrder("rb2601.SHFE", Direction.LONG, Offset.OPEN, 0, LegReason.REBALANCE)


def test_new_plan_defaults():
    p = _plan()
    assert p.status is S.DRAFT
    assert p.plan_id.startswith("20260929-")
    assert p.history == ()


def test_duplicate_product_rejected():
    t = TargetPosition("rb2601.SHFE", 1)
    with pytest.raises(ValueError):
        RebalancePlan.new(date(2026, 9, 29), T0, [t, t], [])


def test_happy_path_records_history():
    p = _plan()
    for to in (S.PENDING_APPROVAL, S.APPROVED, S.EXECUTING, S.DONE):
        p = transition(p, to, at=T0)
    assert p.status is S.DONE
    assert [c.to_status for c in p.history] == [S.PENDING_APPROVAL, S.APPROVED, S.EXECUTING, S.DONE]
    assert p.history[0].from_status is S.DRAFT


def test_transition_is_immutable():
    p = _plan()
    p2 = transition(p, S.PENDING_APPROVAL, at=T0)
    assert p.status is S.DRAFT and p2.status is S.PENDING_APPROVAL


def test_empty_plan_can_skip_approval():
    assert transition(_plan(legs=False), S.DONE, at=T0).status is S.DONE


def test_non_empty_plan_cannot_skip_approval():
    with pytest.raises(InvalidTransitionError):
        transition(_plan(), S.DONE, at=T0)


def test_approved_plan_can_expire():
    p = transition(transition(_plan(), S.PENDING_APPROVAL, at=T0), S.APPROVED, at=T0)
    assert transition(p, S.EXPIRED, at=T0, note="错过执行窗口").history[-1].note == "错过执行窗口"


@pytest.mark.parametrize("terminal", [s for s in S if s.is_terminal])
def test_terminal_states_have_no_exit(terminal):
    assert ALLOWED_TRANSITIONS[terminal] == frozenset()


def test_every_status_has_transition_entry():
    assert set(ALLOWED_TRANSITIONS) == set(S)


@pytest.mark.parametrize(
    ("path", "illegal"),
    [
        ([], S.APPROVED),  # 未推送即批准
        ([], S.EXECUTING),  # 未审批即执行
        ([S.PENDING_APPROVAL], S.EXECUTING),  # 未批准即执行
        ([S.PENDING_APPROVAL, S.REJECTED], S.APPROVED),  # 拒绝后翻案
        ([S.PENDING_APPROVAL, S.EXPIRED], S.APPROVED),  # 过期后批准
    ],
)
def test_illegal_transitions(path, illegal):
    p = _plan()
    for to in path:
        p = transition(p, to, at=T0)
    with pytest.raises(InvalidTransitionError):
        transition(p, illegal, at=T0)
