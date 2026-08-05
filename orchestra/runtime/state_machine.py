"""M2 RUN-001 — Task Run + Node Run state machines.

P0 had :class:`TaskRunState` and :class:`NodeRunState` enums in
``orchestra.core.schema``. M2 adds the *transition tables* —
the legal next-state given a current state and an event — and
the guard that prevents illegal transitions.

The state machine is the runtime enforcement of plan §0.1.2
"state machine"; the audit timeline records every transition.
"""
from __future__ import annotations

from dataclasses import dataclass

from orchestra.core.errors import ContractViolation
from orchestra.core.schema import NodeRunState, TaskRunState


# Task Run state machine
TASK_TRANSITIONS: dict[TaskRunState, set[TaskRunState]] = {
    TaskRunState.CREATED: {TaskRunState.PLANNED, TaskRunState.FAILED, TaskRunState.CANCELLED},
    TaskRunState.PLANNED: {TaskRunState.RUNNING, TaskRunState.FAILED, TaskRunState.CANCELLED},
    TaskRunState.RUNNING: {
        TaskRunState.AWAITING_APPROVAL,
        TaskRunState.SUCCEEDED,
        TaskRunState.FAILED,
        TaskRunState.CANCELLED,
    },
    TaskRunState.AWAITING_APPROVAL: {
        TaskRunState.RUNNING,
        TaskRunState.SUCCEEDED,
        TaskRunState.FAILED,
        TaskRunState.CANCELLED,
    },
    TaskRunState.SUCCEEDED: set(),  # terminal
    TaskRunState.FAILED: set(),     # terminal
    TaskRunState.CANCELLED: set(),  # terminal
}


# Node Run state machine
NODE_TRANSITIONS: dict[NodeRunState, set[NodeRunState]] = {
    NodeRunState.PENDING: {
        NodeRunState.RUNNING,
        NodeRunState.AWAITING_APPROVAL,
        NodeRunState.FAILED,
        NodeRunState.CANCELLED,
    },
    NodeRunState.RUNNING: {
        NodeRunState.AWAITING_APPROVAL,
        NodeRunState.SUCCEEDED,
        NodeRunState.FAILED,
        NodeRunState.CANCELLED,
    },
    NodeRunState.AWAITING_APPROVAL: {
        NodeRunState.RUNNING,
        NodeRunState.SUCCEEDED,
        NodeRunState.FAILED,
        NodeRunState.CANCELLED,
    },
    NodeRunState.SUCCEEDED: set(),
    NodeRunState.FAILED: set(),
    NodeRunState.CANCELLED: set(),
}


class TaskRunStateMachine:
    def can_transition(self, current: TaskRunState, target: TaskRunState) -> bool:
        return target in TASK_TRANSITIONS.get(current, set())

    def transition(self, current: TaskRunState, target: TaskRunState) -> TaskRunState:
        if not self.can_transition(current, target):
            raise ContractViolation(
                f"illegal task run transition: {current.value} -> {target.value}"
            )
        return target


class NodeRunStateMachine:
    def can_transition(self, current: NodeRunState, target: NodeRunState) -> bool:
        return target in NODE_TRANSITIONS.get(current, set())

    def transition(self, current: NodeRunState, target: NodeRunState) -> NodeRunState:
        if not self.can_transition(current, target):
            raise ContractViolation(
                f"illegal node run transition: {current.value} -> {target.value}"
            )
        return target
