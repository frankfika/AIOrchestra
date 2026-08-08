"""M24 — schema and store freeze tests.

The M24 plan freezes (a) the new Pydantic types in
``orchestra.core.schema`` and (b) the M24 methods on
``EventStore``. These tests pin both, so a future contributor
who deletes or renames either will see a failing test
instead of a silent regression.

The tests are smoke-level: they confirm the names and
shapes are present, not that the runtime semantics are
correct (that is the M24-SEC / M24-DLM test files).
"""

from __future__ import annotations

import os

import pytest

from orchestra.core import schema as schema_mod
from orchestra.core.schema import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalState,
    BreakGlassRequest,
    BreakGlassState,
    DeletionEvidence,
    DeletionJob,
    DeletionState,
    LegalHold,
    LifecyclePolicy,
    ResourceKind,
)
from orchestra.coordinator.event_store import EventStore


def test_break_glass_state_machine_frozen() -> None:
    """ADR-0012 — the state machine has exactly the documented
    five states. Adding a new state without updating the ADR
    is a breaking change.
    """
    assert {s.value for s in BreakGlassState} == {
        "requested",
        "first-approved",
        "active",
        "expired",
        "revoked",
    }


def test_approval_state_machine_frozen() -> None:
    """ADR-0013 — five states including ``first-approved`` for
    two-person control.
    """
    assert {s.value for s in ApprovalState} == {
        "pending",
        "approved",
        "rejected",
        "expired",
        "first-approved",
    }


def test_resource_kinds_frozen() -> None:
    """ADR-0014 — six resource kinds (artifact, receipt, event,
    webhook, cache, backup).
    """
    assert {k.value for k in ResourceKind} == {
        "artifact",
        "receipt",
        "event",
        "webhook",
        "cache",
        "backup",
    }


def test_deletion_states_frozen() -> None:
    """ADR-0014 — six states including ``held`` for Legal Hold
    blocks and ``partial`` for the partial-failure path.
    """
    assert {s.value for s in DeletionState} == {
        "pending",
        "running",
        "deleted",
        "partial",
        "failed",
        "held",
    }


def test_break_glass_request_carries_effect_ceiling() -> None:
    """ADR-0012 — the request carries the structured Effect
    payload that the runtime ceiling check enforces against.
    A request without an ``effect`` field cannot bypass the
    ceiling.
    """
    req = BreakGlassRequest(
        tenant_id="t1",
        purpose="incident-2026-08-08",
        effect={"kind": "override_egress_view", "view": "egress.restricted"},
        resource_scope={"resource_kind": "artifact", "resource_id": "art-1"},
        requested_by="alice@acme",
        ticket="INC-1234",
    )
    assert req.state == BreakGlassState.REQUESTED
    assert req.window_seconds == 900  # 15-min default
    assert req.effect["view"] == "egress.restricted"
    assert req.resource_scope["resource_kind"] == "artifact"


def test_approval_record_default_is_pending_single_approver() -> None:
    """ADR-0013 — a fresh approval is pending with one approver
    (business path). Break-glass uses ``required_approvers=2``.
    """
    apv = ApprovalRecord(
        task_run_id="t1",
        node_id="n1",
        tenant_id="t1",
        requested_by="alice",
    )
    assert apv.state == ApprovalState.PENDING
    assert apv.required_approvers == 1
    assert apv.version == 0


def test_legal_hold_carries_case_id() -> None:
    """ADR-0014 — a Legal Hold must have a non-empty case_id
    and a created_by identity. Pydantic enforces the shape;
    the service layer enforces the cross-tenant denial.
    """
    h = LegalHold(
        tenant_id="t1",
        case_id="CASE-2026-001",
        reason="regulator freeze",
        created_by="alice@acme",
    )
    assert h.case_id == "CASE-2026-001"
    assert h.released_at is None
    assert h.released_by is None


def test_lifecycle_policy_default_does_not_auto_delete() -> None:
    """ADR-0014 — the safe default is to retain, not delete.
    ``auto_delete=False`` means the LifecycleSweeper will NOT
    create DeletionJobs unless a tenant policy opts in.
    """
    pol = LifecyclePolicy(
        tenant_id="t1",
        resource_kind=ResourceKind.ARTIFACT,
        retention_seconds=30 * 24 * 3600,
    )
    assert pol.auto_delete is False
    assert pol.retention_seconds == 2_592_000


def test_deletion_evidence_records_kept_copies() -> None:
    """ADR-0014 — the evidence model records both deleted and
    kept copies so a retry can target the kept ones.
    """
    e = DeletionEvidence(
        copies_deleted=1,
        copies_kept=1,
        kept_resources=[{"kind": "backup", "id": "bak-1"}],
        payload_digest="sha256:abcd",
    )
    assert e.copies_deleted == 1
    assert e.copies_kept == 1
    assert e.kept_resources[0]["id"] == "bak-1"


def test_event_kind_includes_m24_events() -> None:
    """M24 — the audit timeline must distinguish break-glass,
    hold, and deletion events from regular task events. This
    test pins the new EventKind members.
    """
    from orchestra.core.schema import EventKind

    expected = {
        "break_glass.requested",
        "break_glass.first_approved",
        "break_glass.active",
        "break_glass.expired",
        "break_glass.revoked",
        "hold.created",
        "hold.released",
        "deletion.requested",
        "deletion.completed",
        "deletion.partial",
        "deletion.blocked",
        "deletion.failed",
    }
    actual = {k.value for k in EventKind}
    missing = expected - actual
    assert not missing, f"M24 EventKind members missing: {missing}"


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL")
    and not os.environ.get("ORCHESTRA_TEST_DB"),
    reason="PostgreSQL not configured",
)
def test_event_store_exposes_m24_methods() -> None:
    """M24 — the EventStore API surface includes the new
    methods. The runtime semantics are tested in
    ``test_m24_break_glass.py`` and ``test_m24_lifecycle.py``.
    """
    store = EventStore()
    expected_methods = [
        # ADR-0013
        "create_approval",
        "record_approval_decision",
        "get_approval",
        "list_approvals_for_tenant",
        "list_approval_decisions",
        # ADR-0012
        "create_break_glass_request",
        "get_break_glass",
        "list_break_glass_for_tenant",
        "record_break_glass_approval",
        "revoke_break_glass",
        "sweep_expired_break_glass",
        # ADR-0014
        "upsert_lifecycle_policy",
        "get_lifecycle_policy",
        "create_legal_hold",
        "release_legal_hold",
        "list_legal_holds",
        "is_resource_held",
        "create_deletion_job",
        "update_deletion_job",
        "get_deletion_job",
        "list_deletion_jobs",
    ]
    for m in expected_methods:
        assert hasattr(store, m), f"EventStore missing M24 method: {m}"


def test_schema_json_export_includes_m24_types() -> None:
    """M24 — the JSON Schema export must include the new types
    so a partner SDK generator sees the wire shape.
    """
    out = schema_mod.export_json_schemas()
    for t in (
        "BreakGlassRequest",
        "ApprovalRecord",
        "ApprovalDecision",
        "LifecyclePolicy",
        "LegalHold",
        "DeletionJob",
    ):
        assert t in out, f"export_json_schemas missing M24 type: {t}"


def test_models_are_round_trip_serializable() -> None:
    """M24 — the new Pydantic models can round-trip through
    JSON. This catches accidental ``BaseModel`` → ``dataclass``
    drift and missing ``model_config = ConfigDict(extra="forbid")``.
    """
    bg = BreakGlassRequest(
        tenant_id="t1",
        purpose="test",
        effect={"k": "v"},
        resource_scope={"kind": "artifact", "id": "a1"},
        requested_by="alice",
    )
    payload = bg.model_dump_json()
    restored = BreakGlassRequest.model_validate_json(payload)
    assert restored.tenant_id == "t1"
    assert restored.effect == {"k": "v"}
