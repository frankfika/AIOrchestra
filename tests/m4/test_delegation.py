"""M4 — Delegation contract tests.

The 3 modes are the binding contract between Orchestra and the host
platform. These tests pin the ownership slots so neither side can
silently renegotiate them.
"""
from __future__ import annotations

import pytest

from orchestra.integrations.delegation import (
    DELEGATE_NODE,
    DELEGATE_TASK,
    OBSERVE_ONLY,
    DelegationMode,
    contract_for_mode,
    governance_state_for,
)


def test_delegate_task_orchestra_owns_everything():
    """delegate-task: Orchestra owns the whole task lifecycle."""
    assert DELEGATE_TASK.execution_owner == "orchestra"
    assert DELEGATE_TASK.idempotency_owner == "orchestra"
    assert DELEGATE_TASK.retry_owner == "orchestra"
    assert DELEGATE_TASK.cancel_owner == "orchestra"
    assert DELEGATE_TASK.final_state_authority == "orchestra"
    assert DELEGATE_TASK.mode == DelegationMode.DELEGATE_TASK


def test_delegate_node_host_owns_idempotency_retry_cancel():
    """delegate-node: Orchestra executes; host controls retry/cancel."""
    assert DELEGATE_NODE.execution_owner == "orchestra"
    # Idempotency, retry, cancel, and final state all stay with the host.
    assert DELEGATE_NODE.idempotency_owner == "host"
    assert DELEGATE_NODE.retry_owner == "host"
    assert DELEGATE_NODE.cancel_owner == "host"
    assert DELEGATE_NODE.final_state_authority == "host"
    assert DELEGATE_NODE.mode == DelegationMode.DELEGATE_NODE


def test_observe_only_host_owns_everything():
    """observe-only: Orchestra only watches and records."""
    assert OBSERVE_ONLY.execution_owner == "host"
    assert OBSERVE_ONLY.idempotency_owner == "host"
    assert OBSERVE_ONLY.retry_owner == "host"
    assert OBSERVE_ONLY.cancel_owner == "host"
    assert OBSERVE_ONLY.final_state_authority == "host"
    assert OBSERVE_ONLY.mode == DelegationMode.OBSERVE_ONLY


def test_contract_for_mode_round_trip():
    for mode in (DelegationMode.DELEGATE_TASK, DelegationMode.DELEGATE_NODE, DelegationMode.OBSERVE_ONLY):
        c = contract_for_mode(mode)
        assert c.mode == mode
        d = c.to_dict()
        assert d["mode"] == mode.value
        # All 5 ownership slots are present in the dict.
        assert {"execution_owner", "idempotency_owner", "retry_owner", "cancel_owner", "final_state_authority"} <= d.keys()


def test_contract_for_unknown_mode_raises():
    """A bogus mode raises ValueError; the lookup is closed."""
    with pytest.raises(ValueError):
        contract_for_mode("not-a-mode")  # type: ignore[arg-type]


def test_delegate_task_and_delegate_node_have_distinct_owners():
    """The two non-observe modes are NOT allowed to share retry_owner
    semantics. A regression that conflates them is a real safety bug:
    a host-driven retry on a delegate-task call would duplicate side
    effects Orchestra already deduplicated.
    """
    assert DELEGATE_TASK.retry_owner != DELEGATE_NODE.retry_owner
    assert DELEGATE_TASK.idempotency_owner != DELEGATE_NODE.idempotency_owner
    assert DELEGATE_TASK.cancel_owner != DELEGATE_NODE.cancel_owner


def test_governance_state_for_delegate_task():
    """The governance state payload includes the delegation contract."""
    state = governance_state_for(
        mode=DelegationMode.DELEGATE_TASK,
        task_state="succeeded",
        plan_id="plan-001",
        audit_url="http://x/tasks/abc/events",
        route_url="http://x/tasks/abc/grants",
    )
    assert state["state"] == "succeeded"
    assert state["plan_id"] == "plan-001"
    assert state["audit_url"].endswith("/events")
    assert state["route_url"].endswith("/grants")
    assert state["delegation"]["mode"] == "delegate-task"
    assert state["delegation"]["final_state_authority"] == "orchestra"
    assert state["error"] == ""


def test_governance_state_carries_error_message():
    state = governance_state_for(
        mode=DelegationMode.OBSERVE_ONLY,
        task_state="failed",
        plan_id=None,
        audit_url="http://x/tasks/abc/events",
        route_url="http://x/tasks/abc/grants",
        error="contract violation: restricted data without manifest",
    )
    assert state["state"] == "failed"
    assert state["error"] == "contract violation: restricted data without manifest"
    assert state["delegation"]["mode"] == "observe-only"
    assert state["delegation"]["final_state_authority"] == "host"


def test_delegation_modes_are_distinct_strings():
    """The mode string is the on-the-wire identifier; aliases are
    forbidden. A platform that stores 'delegate_task' must NOT match
    a tool that announces 'delegate-task'."""
    assert DelegationMode.DELEGATE_TASK.value == "delegate-task"
    assert DelegationMode.DELEGATE_NODE.value == "delegate-node"
    assert DelegationMode.OBSERVE_ONLY.value == "observe-only"
    values = {m.value for m in DelegationMode}
    assert len(values) == 3
