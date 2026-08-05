"""M2 — Fenced Runtime test suite.

The M2 gate requires:
  - Crash/Retry/Unknown tests
  - Old Lease denial
  - Offline Receipt verification
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone, timedelta

import pytest

from orchestra.coordinator.receipt import ReceiptBuilder
from orchestra.core.hashing import hmac_keygen
from orchestra.core.ids import new_id
from orchestra.core.schema import (
    AuditEvent,
    EventKind,
    SignedReceipt,
)
from orchestra.evidence.cli import verify_receipt_offline
from orchestra.evidence.merkle import MerkleLog, verify_inclusion_proof
from orchestra.runtime.credential_broker import CredentialBroker
from orchestra.runtime.fencing import FencingGuard, StaleFencingToken
from orchestra.runtime.lease import FencingToken, Lease, LeaseState
from orchestra.runtime.outbox import Dispatcher, Outbox
from orchestra.runtime.reconciler import Reconciler
from orchestra.runtime.state_machine import NodeRunStateMachine, TaskRunStateMachine
from orchestra.core.schema import NodeRunState, TaskRunState


# ---------------------------------------------------------------------------
# RUN-001: State machine
# ---------------------------------------------------------------------------


def test_task_run_state_machine_legal_transitions():
    sm = TaskRunStateMachine()
    assert sm.transition(TaskRunState.CREATED, TaskRunState.PLANNED) == TaskRunState.PLANNED
    assert sm.transition(TaskRunState.PLANNED, TaskRunState.RUNNING) == TaskRunState.RUNNING
    assert sm.transition(TaskRunState.RUNNING, TaskRunState.SUCCEEDED) == TaskRunState.SUCCEEDED
    # Terminal states have no outgoing transitions.
    with pytest.raises(Exception):
        sm.transition(TaskRunState.SUCCEEDED, TaskRunState.RUNNING)


def test_node_run_state_machine_illegal_transition_raises():
    sm = NodeRunStateMachine()
    # PENDING cannot jump to SUCCEEDED without going through RUNNING.
    with pytest.raises(Exception):
        sm.transition(NodeRunState.PENDING, NodeRunState.SUCCEEDED)


# ---------------------------------------------------------------------------
# RUN-002: Fencing Token
# ---------------------------------------------------------------------------


def test_fencing_guard_rejects_stale_token():
    guard = FencingGuard()
    t1 = FencingToken(cell_id="cell-1", value=10)
    t0 = FencingToken(cell_id="cell-1", value=5)
    guard.check(t1, node_run_id="n-1")
    with pytest.raises(StaleFencingToken):
        guard.check(t0, node_run_id="n-1")


def test_fencing_guard_accepts_monotonic_increase():
    guard = FencingGuard()
    for v in (1, 2, 3, 10, 100):
        guard.check(FencingToken(cell_id="c", value=v), node_run_id="n")
    assert guard.current_high_water("c") == 100


def test_fencing_guard_per_node_run_isolation():
    guard = FencingGuard()
    guard.check(FencingToken(cell_id="c", value=1), node_run_id="n-1")
    # A new node-run can start fresh.
    guard.check(FencingToken(cell_id="c", value=1), node_run_id="n-2")


# ---------------------------------------------------------------------------
# RUN-002: Outbox + Dispatcher
# ---------------------------------------------------------------------------


def test_outbox_enqueue_and_dispatch():
    outbox = Outbox()
    event = AuditEvent(
        task_run_id="t", node_run_id="n", kind=EventKind.NODE_STARTED,
        payload={"node_id": "n"},
    )
    outbox.enqueue(event)
    assert len(outbox.pending()) == 1
    received: list[AuditEvent] = []
    Dispatcher(outbox, event_store_sink=received.append).flush()
    assert len(received) == 1
    assert len(outbox.pending()) == 0


def test_outbox_dispatcher_keeps_failed_entries_for_retry():
    outbox = Outbox()
    event = AuditEvent(
        task_run_id="t", node_run_id="n", kind=EventKind.NODE_STARTED,
        payload={"node_id": "n"},
    )
    outbox.enqueue(event)
    def failing_sink(_ev):
        raise RuntimeError("boom")
    rep = Dispatcher(outbox, event_store_sink=failing_sink, max_attempts=2).flush()
    assert rep["failed"] == 1
    assert rep["delivered"] == 0
    # The entry is still pending.
    assert len(outbox.pending()) == 1


# ---------------------------------------------------------------------------
# RUN-002: Reconciler (Unknown handling)
# ---------------------------------------------------------------------------


def test_reconciler_flags_expired_lease_as_unknown():
    now = datetime.now(timezone.utc)
    lease = Lease(
        lease_id="l1",
        task_run_id="t",
        node_run_id="n",
        cell_id="c",
        fencing_token=FencingToken(cell_id="c", value=1),
        state=LeaseState.ACTIVE,
        issued_at=now.isoformat(),
        expires_at=(now - timedelta(seconds=10)).isoformat(),
    )
    report = Reconciler().reconcile(
        leases=[lease], node_run_states={"n": "running"}, now_iso=now.isoformat()
    )
    assert "n" in report.unknown_node_runs
    assert any("Unknown" in a for a in report.actions)


def test_reconciler_does_not_blindly_retry_unknown_node_run():
    """Invariant: the Reconciler must NOT auto-retry; it must
    surface the gap. This is the M2 enforcement of the plan's
    "Retry 只能为同一 Node Run 使用已批准的幂等语义".
    """
    now = datetime.now(timezone.utc)
    lease = Lease(
        lease_id="l1",
        task_run_id="t",
        node_run_id="n",
        cell_id="c",
        fencing_token=FencingToken(cell_id="c", value=1),
        state=LeaseState.ACTIVE,
        issued_at=now.isoformat(),
        expires_at=(now - timedelta(seconds=10)).isoformat(),
    )
    report = Reconciler().reconcile([lease], {"n": "running"}, now_iso=now.isoformat())
    # The report's actions never include "retry" or "resubmit".
    assert not any("retry" in a.lower() or "resubmit" in a.lower() for a in report.actions)


def test_reconciler_surfaces_revoked_leases():
    now = datetime.now(timezone.utc)
    lease = Lease(
        lease_id="l1",
        task_run_id="t",
        node_run_id="n",
        cell_id="c",
        fencing_token=FencingToken(cell_id="c", value=1),
        state=LeaseState.REVOKED,
        issued_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=600)).isoformat(),
    )
    report = Reconciler().reconcile([lease], {"n": "running"}, now_iso=now.isoformat())
    assert "l1" in report.revoked_leases


# ---------------------------------------------------------------------------
# IDN-001: Credential Broker
# ---------------------------------------------------------------------------


def test_credential_broker_issues_and_verifies_grant():
    from orchestra.core.schema import DataView, Purpose

    broker = CredentialBroker()
    broker.add_cell("c1")
    grant = broker.issue(
        cell_id="c1",
        task_run_id="t",
        node_run_id="n",
        task_id="task-1",
        node_id="n1",
        capability_id="c",
        manifest_id="m:c",
        data_view=DataView(name="v", shape="reference"),
        purpose=Purpose(code="x"),
    )
    assert grant.signature is not None
    assert broker.verify(grant, "c1")


def test_credential_broker_rejects_revoked_kid():
    from orchestra.core.schema import DataView, Purpose

    broker = CredentialBroker()
    broker.add_cell("c1")
    grant = broker.issue(
        cell_id="c1",
        task_run_id="t",
        node_run_id="n",
        task_id="task-1",
        node_id="n1",
        capability_id="c",
        manifest_id="m:c",
        data_view=DataView(name="v", shape="reference"),
        purpose=Purpose(code="x"),
    )
    current_kid = broker._current_kid["c1"]
    broker.revoke("c1", current_kid)
    # The grant was issued under the current (now revoked) kid;
    # verify_with_kid rejects it because the kid is revoked.
    assert not broker.verify_with_kid(grant, "c1", current_kid)
    # The (legacy) verify() loops over all non-revoked issuers; the
    # current is revoked, so no issuer accepts the grant.
    assert not broker.verify(grant, "c1")


def test_credential_broker_audience_narrowing():
    from orchestra.core.schema import DataView, Purpose

    broker = CredentialBroker()
    broker.add_cell("c1")
    parent = broker.issue(
        cell_id="c1",
        task_run_id="t",
        node_run_id="n",
        task_id="task-1",
        node_id="n1",
        capability_id="c",
        manifest_id="m:c",
        data_view=DataView(name="v", shape="reference"),
        purpose=Purpose(code="x"),
    )
    # Child with a *different* audience is rejected (M2 only knows
    # the "P0" audience string, so this is a coarse check).
    with pytest.raises(RuntimeError):
        broker.issue(
            cell_id="c1",
            task_run_id="t",
            node_run_id="n2",
            task_id="task-1",
            node_id="n2",
            capability_id="c",
            manifest_id="m:c",
            data_view=DataView(name="v", shape="reference"),
            purpose=Purpose(code="x"),
            parent_grant_id=parent.grant_id,
            parent_audience="p5",  # not a subset of "p0"
        )


# ---------------------------------------------------------------------------
# EVD-001: Merkle log
# ---------------------------------------------------------------------------


def test_merkle_log_root_changes_with_each_append():
    log = MerkleLog()
    r0 = log.root()
    r1 = log.append("e1")
    r2 = log.append("e2")
    assert r0 != r1 != r2 != r0


def test_merkle_inclusion_proof_verifies():
    log = MerkleLog()
    log.append("a")
    log.append("b")
    log.append("c")
    log.append("d")
    p0 = log.inclusion_proof(0)
    p1 = log.inclusion_proof(1)
    p2 = log.inclusion_proof(2)
    p3 = log.inclusion_proof(3)
    assert verify_inclusion_proof(p0)
    assert verify_inclusion_proof(p1)
    assert verify_inclusion_proof(p2)
    assert verify_inclusion_proof(p3)
    assert p0.root == p1.root == p2.root == p3.root == log.root()


def test_merkle_inclusion_proof_detects_tampering():
    log = MerkleLog()
    log.append("a")
    log.append("b")
    log.append("c")
    p = log.inclusion_proof(1)
    # Tamper with the leaf hash.
    p.leaf_hash = "deadbeef" * 8
    assert not verify_inclusion_proof(p)


# ---------------------------------------------------------------------------
# EVD-002: Offline Receipt verification
# ---------------------------------------------------------------------------


def test_offline_receipt_verification_ok():
    key = hmac_keygen()
    rb = ReceiptBuilder(key)
    r = rb.build(
        task_run_id="t", node_run_id="n", node_id="n",
        plan_digest="plan:abc", capability_id="c1", manifest_id="m:c1",
        data_view={"name": "v", "shape": "reference", "fields": []},
        inputs={"x": 1}, outputs={"y": 2},
        started_at="2026-01-01T00:00:00.000Z",
        ended_at="2026-01-01T00:00:01.000Z",
        status="succeeded",
    )
    out = verify_receipt_offline(r, plan_digest="plan:abc")
    assert out["verified"] is True


def test_offline_receipt_verification_detects_plan_digest_mismatch():
    key = hmac_keygen()
    rb = ReceiptBuilder(key)
    r = rb.build(
        task_run_id="t", node_run_id="n", node_id="n",
        plan_digest="plan:abc", capability_id="c1", manifest_id="m:c1",
        data_view={"name": "v", "shape": "reference", "fields": []},
        inputs={"x": 1}, outputs={"y": 2},
        started_at="2026-01-01T00:00:00.000Z",
        ended_at="2026-01-01T00:00:01.000Z",
        status="succeeded",
    )
    out = verify_receipt_offline(r, plan_digest="plan:WRONG")
    assert out["verified"] is False
    assert "plan_digest" in out["reason"]


def test_offline_receipt_verification_cli(tmp_path, capsys):
    key = hmac_keygen()
    rb = ReceiptBuilder(key)
    r = rb.build(
        task_run_id="t", node_run_id="n", node_id="n",
        plan_digest="plan:abc", capability_id="c1", manifest_id="m:c1",
        data_view={"name": "v", "shape": "reference", "fields": []},
        inputs={"x": 1}, outputs={"y": 2},
        started_at="2026-01-01T00:00:00.000Z",
        ended_at="2026-01-01T00:00:01.000Z",
        status="succeeded",
    )
    receipt_path = tmp_path / "r.json"
    receipt_path.write_text(r.model_dump_json())
    # Patch sys.argv and run.
    import sys
    from orchestra.evidence.cli import main as cli_main
    old = sys.argv
    sys.argv = ["verify", str(receipt_path), "--plan-digest", "plan:abc"]
    rc = cli_main()
    out = capsys.readouterr().out
    assert rc == 0
    assert '"verified": true' in out
    sys.argv = ["verify", str(receipt_path), "--plan-digest", "plan:WRONG"]
    rc = cli_main()
    out = capsys.readouterr().out
    assert rc == 2
    assert '"verified": false' in out
    sys.argv = old
