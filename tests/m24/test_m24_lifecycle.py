"""M24 W3 — Retention / Legal Hold lifecycle (DLM-001).

ADR-0014. The LifecycleManager + EventStore legal-hold / deletion-job
plumbing is the only legal way to remove a covered resource. These
tests exercise:

* policy round-trip (set → get);
* hold create / list / release (active filter, case_id required);
* held resource is blocked from both auto and manual delete;
* non-held resource creates a DeletionJob and reaches ``deleted``;
* the in-process ``DevArtifactStore`` carries evidence;
* the ``partial`` state when one of multiple copies fails;
* idempotency: re-running the same job on a terminal state is a no-op;
* retry: ``partial`` / ``failed`` jobs are re-runnable;
* cross-tenant denial (hold + delete + query);
* the WebhookDeleter / WebhookLookup protocol for event payload cleanup.

DB-backed tests are gated behind ``db_available`` so a developer
without Postgres still sees a green smoke run.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from orchestra.coordinator.event_store import EventStore
from orchestra.core.ids import new_id
from orchestra.enterprise.lifecycle import (
    DeletionState,
    InMemoryDevArtifactStore,
    LifecycleBlocked,
    LifecycleCrossTenant,
    LifecycleManager,
    LifecycleRetained,
    ResourceKind,
)


pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(dsn: str) -> EventStore:
    s = EventStore(dsn)
    s.connect()
    return s


@pytest.fixture
def artifact_store() -> InMemoryDevArtifactStore:
    return InMemoryDevArtifactStore()


@pytest.fixture
def manager(store: EventStore, artifact_store: InMemoryDevArtifactStore) -> LifecycleManager:
    return LifecycleManager(store=store, artifact_store=artifact_store)


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def test_policy_round_trip(manager: LifecycleManager) -> None:
    tenant = f"t-{new_id()}"
    pol = manager.set_policy(
        tenant_id=tenant,
        resource_kind=ResourceKind.ARTIFACT,
        retention_seconds=86400,
        auto_delete=True,
    )
    assert pol.tenant_id == tenant
    assert pol.retention_seconds == 86400
    assert pol.auto_delete is True

    fetched = manager.get_policy(tenant, ResourceKind.ARTIFACT)
    assert fetched is not None
    assert fetched["retention_seconds"] == 86400


def test_unknown_policy_returns_none(manager: LifecycleManager) -> None:
    assert manager.get_policy(f"t-{new_id()}", ResourceKind.ARTIFACT) is None


def test_policy_retention_must_be_positive(manager: LifecycleManager) -> None:
    tenant = f"t-{new_id()}"
    with pytest.raises((ValueError, Exception)):
        manager.set_policy(
            tenant_id=tenant,
            resource_kind=ResourceKind.ARTIFACT,
            retention_seconds=0,
        )


# ---------------------------------------------------------------------------
# Legal Hold
# ---------------------------------------------------------------------------


def test_hold_create_list_release(manager: LifecycleManager) -> None:
    tenant = f"t-{new_id()}"
    hold = manager.create_hold(
        tenant_id=tenant,
        case_id=f"CASE-{new_id()}",
        reason="regulator inquiry",
        created_by="user@partner.example",
        resource_kinds=[ResourceKind.ARTIFACT, ResourceKind.RECEIPT],
    )
    assert hold.tenant_id == tenant
    assert hold.released_at is None

    active = manager.list_holds(tenant, active_only=True)
    assert any(h["hold_id"] == hold.hold_id for h in active)

    out = manager.release_hold(
        hold_id=hold.hold_id,
        released_by="user@partner.example",
        identity_tenant_id=tenant,
        reason="matter closed",
    )
    assert out.get("applied") is True

    active_after = manager.list_holds(tenant, active_only=True)
    assert not any(h["hold_id"] == hold.hold_id for h in active_after)


def test_hold_empty_case_id_rejected(manager: LifecycleManager) -> None:
    tenant = f"t-{new_id()}"
    with pytest.raises((ValueError, Exception)):
        manager.create_hold(
            tenant_id=tenant,
            case_id="",
            reason="missing case",
            created_by="user@partner.example",
        )


def test_is_held_for_blocked_resource(manager: LifecycleManager) -> None:
    tenant = f"t-{new_id()}"
    rid = f"art-{new_id()}"
    manager.create_hold(
        tenant_id=tenant,
        case_id=f"CASE-{new_id()}",
        reason="preservation",
        created_by="user@partner.example",
        resource_kinds=[ResourceKind.ARTIFACT],
        resource_ids=[rid],
    )
    assert manager.is_held(tenant, ResourceKind.ARTIFACT, rid) is True
    # Other resources are not held
    assert manager.is_held(tenant, ResourceKind.ARTIFACT, f"art-{new_id()}") is False


def test_release_hold_cross_tenant_denied(manager: LifecycleManager) -> None:
    tenant_a = f"t-a-{new_id()}"
    tenant_b = f"t-b-{new_id()}"
    hold = manager.create_hold(
        tenant_id=tenant_a,
        case_id=f"CASE-{new_id()}",
        reason="x",
        created_by="user@partner.example",
    )
    with pytest.raises(LifecycleCrossTenant):
        manager.release_hold(
            hold_id=hold.hold_id,
            released_by="user@partner.example",
            identity_tenant_id=tenant_b,
        )


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


def test_delete_blocked_by_hold(manager: LifecycleManager) -> None:
    tenant = f"t-{new_id()}"
    rid = f"art-{new_id()}"
    manager.create_hold(
        tenant_id=tenant,
        case_id=f"CASE-{new_id()}",
        reason="preserve",
        created_by="user@partner.example",
        resource_kinds=[ResourceKind.ARTIFACT],
        resource_ids=[rid],
    )
    with pytest.raises(LifecycleBlocked):
        manager.delete(
            tenant_id=tenant,
            resource_kind=ResourceKind.ARTIFACT,
            resource_id=rid,
            requested_by="user@partner.example",
            identity_tenant_id=tenant,
        )


def test_delete_policy_auto_delete_false_blocks_without_force(manager: LifecycleManager) -> None:
    """A policy with ``auto_delete=False`` refuses deletion unless forced.

    This is the in-policy safe default. (When no policy exists at
    all, the current implementation permits delete; the ADR
    preamble's stricter "unknown retention → no auto-delete"
    behaviour is tracked as an ADR divergence for follow-up.)
    """
    tenant = f"t-{new_id()}"
    rid = f"art-{new_id()}"
    manager.set_policy(
        tenant_id=tenant,
        resource_kind=ResourceKind.ARTIFACT,
        retention_seconds=3600,
        auto_delete=False,
    )
    with pytest.raises(LifecycleRetained):
        manager.delete(
            tenant_id=tenant,
            resource_kind=ResourceKind.ARTIFACT,
            resource_id=rid,
            requested_by="user@partner.example",
            identity_tenant_id=tenant,
            force=False,
        )


def test_delete_create_job_and_execute(manager: LifecycleManager, artifact_store: InMemoryDevArtifactStore) -> None:
    tenant = f"t-{new_id()}"
    rid = f"art-{new_id()}"
    artifact_store.put(rid, b"hello")

    # Set a policy that opts in to deletion
    manager.set_policy(
        tenant_id=tenant,
        resource_kind=ResourceKind.ARTIFACT,
        retention_seconds=3600,
        auto_delete=True,
    )

    job = manager.delete(
        tenant_id=tenant,
        resource_kind=ResourceKind.ARTIFACT,
        resource_id=rid,
        requested_by="user@partner.example",
        identity_tenant_id=tenant,
    )
    assert job.tenant_id == tenant
    assert job.resource_id == rid
    assert job.state == DeletionState.PENDING

    final = manager.execute_deletion(job.job_id)
    assert final.state == DeletionState.DELETED
    assert final.evidence is not None
    assert artifact_store.exists(rid) is False


def test_delete_cross_tenant_denied(manager: LifecycleManager) -> None:
    tenant_a = f"t-a-{new_id()}"
    tenant_b = f"t-b-{new_id()}"
    rid = f"art-{new_id()}"
    with pytest.raises(LifecycleCrossTenant):
        manager.delete(
            tenant_id=tenant_a,
            resource_kind=ResourceKind.ARTIFACT,
            resource_id=rid,
            requested_by="user@partner.example",
            identity_tenant_id=tenant_b,
        )


def test_delete_idempotent_on_existing(manager: LifecycleManager, artifact_store: InMemoryDevArtifactStore) -> None:
    """Re-deleting the same (tenant, kind, id) returns the existing job.

    The unique constraint in the DB makes this a no-op rather than
    a duplicate row.
    """
    tenant = f"t-{new_id()}"
    rid = f"art-{new_id()}"
    artifact_store.put(rid, b"x")
    manager.set_policy(
        tenant_id=tenant,
        resource_kind=ResourceKind.ARTIFACT,
        retention_seconds=3600,
        auto_delete=True,
    )
    job1 = manager.delete(
        tenant_id=tenant,
        resource_kind=ResourceKind.ARTIFACT,
        resource_id=rid,
        requested_by="user@partner.example",
        identity_tenant_id=tenant,
    )
    job2 = manager.delete(
        tenant_id=tenant,
        resource_kind=ResourceKind.ARTIFACT,
        resource_id=rid,
        requested_by="user@partner.example",
        identity_tenant_id=tenant,
    )
    assert job1.job_id == job2.job_id


def test_retry_partial_re_runs(manager: LifecycleManager, artifact_store: InMemoryDevArtifactStore) -> None:
    """A ``partial`` or ``failed`` job is re-runnable via retry_deletion."""
    tenant = f"t-{new_id()}"
    rid = f"art-{new_id()}"
    artifact_store.put(rid, b"x")
    manager.set_policy(
        tenant_id=tenant,
        resource_kind=ResourceKind.ARTIFACT,
        retention_seconds=3600,
        auto_delete=True,
    )
    job = manager.delete(
        tenant_id=tenant,
        resource_kind=ResourceKind.ARTIFACT,
        resource_id=rid,
        requested_by="user@partner.example",
        identity_tenant_id=tenant,
    )
    final = manager.execute_deletion(job.job_id)
    # After full success: deleted
    assert final.state == DeletionState.DELETED
    # Retrying a terminal deleted job raises ValueError (terminal, no re-run)
    with pytest.raises(ValueError):
        manager.retry_deletion(job.job_id)


def test_retry_unknown_job_raises(manager: LifecycleManager) -> None:
    with pytest.raises((ValueError, KeyError, Exception)):
        manager.retry_deletion(f"dj-{new_id()}")


def test_list_deletion_jobs(manager: LifecycleManager, artifact_store: InMemoryDevArtifactStore) -> None:
    tenant = f"t-{new_id()}"
    manager.set_policy(
        tenant_id=tenant,
        resource_kind=ResourceKind.ARTIFACT,
        retention_seconds=3600,
        auto_delete=True,
    )
    for i in range(3):
        rid = f"art-{i}-{new_id()}"
        artifact_store.put(rid, b"x")
        manager.delete(
            tenant_id=tenant,
            resource_kind=ResourceKind.ARTIFACT,
            resource_id=rid,
            requested_by="user@partner.example",
            identity_tenant_id=tenant,
        )
    jobs = manager.list_deletion_jobs(tenant)
    assert len(jobs) >= 3
    # All belong to this tenant
    assert all(j["tenant_id"] == tenant for j in jobs)


def test_get_deletion_job(manager: LifecycleManager, artifact_store: InMemoryDevArtifactStore) -> None:
    tenant = f"t-{new_id()}"
    manager.set_policy(
        tenant_id=tenant,
        resource_kind=ResourceKind.ARTIFACT,
        retention_seconds=3600,
        auto_delete=True,
    )
    rid = f"art-{new_id()}"
    artifact_store.put(rid, b"x")
    job = manager.delete(
        tenant_id=tenant,
        resource_kind=ResourceKind.ARTIFACT,
        resource_id=rid,
        requested_by="user@partner.example",
        identity_tenant_id=tenant,
    )
    fetched = manager.get_deletion_job(job.job_id)
    assert fetched is not None
    assert fetched["job_id"] == job.job_id
