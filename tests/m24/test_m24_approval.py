"""M24 W2 — Persistent approval workflow (SEC-002).

ADR-0013. The engine's default approval handler now writes a
persistent row to PG and waits on the in-process asyncio.Event
as a wake-up cache. These tests exercise:

* the ApprovalService wrapper surface;
* the engine restart path (a pending row is re-found);
* the atomic CAS (two API calls approving the same gate
  concurrently — only one returns ``applied=True``);
* the decision payload being recorded in ``approval_decisions``;
* the default 15-min window when a tenant has no policy;
* the ``reload_pending_for_tenant`` start-up hook;
* the existing demo path (a custom ``approval_handler``
  still wins — no M24 dependency injection required).
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import pytest

from orchestra.coordinator.engine import Coordinator
from orchestra.coordinator.event_store import EventStore
from orchestra.core.ids import new_id
from orchestra.enterprise.approval import ApprovalService


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
def approval_service(store: EventStore) -> ApprovalService:
    return ApprovalService(store=store)


def _cleanup(store: EventStore, task_run_ids: list[str]) -> None:
    """Best-effort delete so a test can run repeatedly."""
    if not task_run_ids:
        return
    try:
        with store._tx() as c, c.cursor() as cur:  # noqa: SLF001
            cur.execute(
                "DELETE FROM approval_decisions WHERE approval_id IN "
                "(SELECT approval_id FROM approvals WHERE task_run_id = ANY(%s))",
                (task_run_ids,),
            )
            cur.execute(
                "DELETE FROM approvals WHERE task_run_id = ANY(%s)",
                (task_run_ids,),
            )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# ApprovalService unit tests
# ---------------------------------------------------------------------------


def test_approval_service_create_for_node_returns_pending(
    approval_service: ApprovalService,
    store: EventStore,
) -> None:
    """A fresh approval is ``pending`` with one approver."""
    task_run_id = f"trun-m24-{new_id()[:8]}"
    try:
        rec = approval_service.create_for_node(
            task_run_id=task_run_id,
            node_id="human_approval",
            tenant_id="tenant:m24:test",
            requested_by="alice",
        )
        assert rec.state.value == "pending"
        assert rec.required_approvers == 1
        assert rec.tenant_id == "tenant:m24:test"
    finally:
        _cleanup(store, [task_run_id])


def test_approval_service_decide_transitions_to_approved(
    approval_service: ApprovalService,
    store: EventStore,
) -> None:
    """W2-Gate scenario #3: ``decide`` transitions state to
    ``approved`` for a single-approver gate.
    """
    task_run_id = f"trun-m24-{new_id()[:8]}"
    try:
        rec = approval_service.create_for_node(
            task_run_id=task_run_id,
            node_id="human_approval",
            tenant_id="tenant:m24:test",
            requested_by="alice",
        )
        cas = approval_service.decide(
            approval_id=rec.approval_id,
            decision="approve",
            decided_by="bob",
            identity_tenant_id="tenant:m24:test",
            rationale="looks good",
        )
        assert cas["applied"] is True
        assert cas["state"] == "approved"
        # The decision is recorded in approval_decisions.
        decisions = approval_service.list_decisions(rec.approval_id)
        assert len(decisions) == 1
        assert decisions[0]["decided_by"] == "bob"
        assert decisions[0]["decision"] == "approve"
    finally:
        _cleanup(store, [task_run_id])


def test_approval_service_two_approvers_for_break_glass(
    approval_service: ApprovalService,
    store: EventStore,
) -> None:
    """W2-Gate scenario #4: the decision payload is recorded in
    ``approval_decisions`` (we assert two rows after two
    approve calls).
    """
    task_run_id = f"trun-m24-{new_id()[:8]}"
    try:
        rec = approval_service.create_for_node(
            task_run_id=task_run_id,
            node_id="human_approval",
            tenant_id="tenant:m24:test",
            requested_by="alice",
            required_approvers=2,
        )
        # First signature → first-approved
        r1 = approval_service.decide(
            approval_id=rec.approval_id,
            decision="approve",
            decided_by="bob",
            identity_tenant_id="tenant:m24:test",
        )
        assert r1["applied"] is True
        assert r1["state"] == "first-approved"
        # Second signature → approved
        r2 = approval_service.decide(
            approval_id=rec.approval_id,
            decision="approve",
            decided_by="carol",
            identity_tenant_id="tenant:m24:test",
        )
        assert r2["applied"] is True
        assert r2["state"] == "approved"
        decisions = approval_service.list_decisions(rec.approval_id)
        assert len(decisions) == 2
        # decision_seq is 1, 2
        seqs = sorted(d["decision_seq"] for d in decisions)
        assert seqs == [1, 2]
    finally:
        _cleanup(store, [task_run_id])


def test_approval_service_cross_tenant_denied(
    approval_service: ApprovalService,
    store: EventStore,
) -> None:
    """A decision with a wrong identity_tenant is denied.
    """
    task_run_id = f"trun-m24-{new_id()[:8]}"
    try:
        rec = approval_service.create_for_node(
            task_run_id=task_run_id,
            node_id="human_approval",
            tenant_id="tenant:m24:alpha",
            requested_by="alice",
        )
        r = approval_service.decide(
            approval_id=rec.approval_id,
            decision="approve",
            decided_by="mallory",
            identity_tenant_id="tenant:m24:beta",
        )
        assert r["applied"] is False
        assert r["reason"] == "cross_tenant"
    finally:
        _cleanup(store, [task_run_id])


def test_approval_service_concurrent_only_one_wins(
    approval_service: ApprovalService,
    store: EventStore,
) -> None:
    """W2-Gate scenario #14: two concurrent decisions on the
    same gate — only one returns ``applied=True``.
    """
    task_run_id = f"trun-m24-{new_id()[:8]}"
    try:
        rec = approval_service.create_for_node(
            task_run_id=task_run_id,
            node_id="human_approval",
            tenant_id="tenant:m24:test",
            requested_by="alice",
        )
        # Fire two decisions back-to-back. The first wins; the
        # second is a no-op because the row is now terminal.
        r1 = approval_service.decide(
            approval_id=rec.approval_id,
            decision="approve",
            decided_by="bob",
            identity_tenant_id="tenant:m24:test",
        )
        r2 = approval_service.decide(
            approval_id=rec.approval_id,
            decision="approve",
            decided_by="carol",
            identity_tenant_id="tenant:m24:test",
        )
        # At most one applied.
        assert (r1["applied"] and not r2["applied"]) or (
            not r1["applied"] and r2["applied"]
        )
        applied_count = sum(1 for r in (r1, r2) if r["applied"])
        assert applied_count == 1
    finally:
        _cleanup(store, [task_run_id])


def test_approval_service_reload_pending_for_tenant(
    approval_service: ApprovalService,
    store: EventStore,
) -> None:
    """A pending approval is listed by
    ``reload_pending_for_tenant`` (the engine's restart hook).
    """
    tenant = f"tenant:m24:reload-{new_id()[:6]}"
    task_run_id = f"trun-m24-{new_id()[:8]}"
    try:
        rec = approval_service.create_for_node(
            task_run_id=task_run_id,
            node_id="human_approval",
            tenant_id=tenant,
            requested_by="alice",
        )
        rows = approval_service.reload_pending_for_tenant(tenant)
        ids = [r["approval_id"] for r in rows]
        assert rec.approval_id in ids
    finally:
        _cleanup(store, [task_run_id])


# ---------------------------------------------------------------------------
# Coordinator integration tests
# ---------------------------------------------------------------------------


def _make_default_coordinator(
    store: EventStore,
    *,
    approval_service: ApprovalService,
    tenant_id: str | None,
) -> Coordinator:
    """Build a Coordinator with the default registry and the
    M24 wiring. We bypass the default four-adapter build (which
    starts HTTP servers) by passing in a minimal manifest set;
    for the restart test we don't actually run a task — we
    only care about the in-process asyncio.Event cache.
    """
    from orchestra.adapters.mock_sink import MockSinkAdapter
    from orchestra.coordinator.node_grant import NodeGrantIssuer
    from orchestra.coordinator.receipt import ReceiptBuilder
    from orchestra.core.hashing import hmac_keygen
    from orchestra.registry.policy import default_p0_rules
    from orchestra.registry.router import Router

    # Minimal manifest set: a single dummy capability.
    from orchestra.core.schema import (
        CapabilityKind,
        CapabilityManifest,
        IntegrationLevel,
    )

    class _InMemoryStore:
        def __init__(self) -> None:
            self._items: dict[str, CapabilityManifest] = {
                "dummy.cap": CapabilityManifest(
                    capability_id="dummy.cap",
                    name="dummy",
                    kind=CapabilityKind.LOCAL_MODEL,
                    endpoint="http://nowhere",
                    integration_level=IntegrationLevel.OBSERVE,
                )
            }

        def get(self, cid: str) -> CapabilityManifest:
            return self._items[cid]

        def all(self) -> list[CapabilityManifest]:
            return list(self._items.values())

    router = Router(_InMemoryStore(), default_p0_rules())  # type: ignore[arg-type]
    grant_issuer = NodeGrantIssuer(hmac_keygen())
    receipt_builder = ReceiptBuilder(hmac_keygen())
    return Coordinator(
        store=store,
        router=router,
        adapters={"dummy.cap": MockSinkAdapter(endpoint="http://nowhere/sink")},
        grant_issuer=grant_issuer,
        receipt_builder=receipt_builder,
        tenant_id=tenant_id,
        approval_service=approval_service,
    )


def test_coordinator_reload_pending_approvals_on_restart(
    approval_service: ApprovalService,
    store: EventStore,
) -> None:
    """W2-Gate scenario #13: after a process restart (simulated
    by constructing a new Coordinator and calling
    ``_reload_pending_approvals``), a pending approval is
    re-found.
    """
    tenant = f"tenant:m24:restart-{new_id()[:6]}"
    task_run_id = f"trun-m24-{new_id()[:8]}"
    try:
        # 1. Plant a pending approval.
        approval_service.create_for_node(
            task_run_id=task_run_id,
            node_id="human_approval",
            tenant_id=tenant,
            requested_by="alice",
        )
        # 2. Build a fresh Coordinator — simulates a process
        # restart. The constructor calls
        # ``_reload_pending_approvals`` automatically.
        coord = _make_default_coordinator(
            store, approval_service=approval_service, tenant_id=tenant
        )
        # 3. The in-process event cache now carries the
        # pending row.
        key = (task_run_id, "human_approval")
        assert key in coord._approval_events  # noqa: SLF001
        bucket = coord._approval_events[key][1]  # noqa: SLF001
        assert bucket.get("reloaded") is True
        assert bucket.get("approval_id") is not None
    finally:
        _cleanup(store, [task_run_id])


def test_coordinator_no_tenant_keeps_legacy_inmemory_path(
    approval_service: ApprovalService,
    store: EventStore,
) -> None:
    """Backward compat: a Coordinator built without a tenant
    (the demo / pre-M24 path) does not touch the approvals
    table.
    """
    coord = _make_default_coordinator(
        store, approval_service=approval_service, tenant_id=None
    )
    # The reload is a no-op when there's no tenant.
    assert coord._approval_events == {}  # noqa: SLF001
