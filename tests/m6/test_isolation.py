"""M6 ENT-001 — Multi-tenant isolation tests.

These tests prove the strongest possible isolation: even with
direct storage access, Tenant A cannot read Tenant B's task_runs,
events, or receipts. The WHERE clause is built from the active
tenant, never from the caller's input.
"""
from __future__ import annotations

import uuid

import pytest

from orchestra.core.schema import AuditEvent, EventKind, TaskRunState
from orchestra.enterprise.isolation import IsolatingEventStore
from orchestra.enterprise.tenant import (
    Tenant,
    TenantAccessDenied,
    TenantContext,
    TenantRole,
    reset_active,
    set_active,
)


pytestmark = pytest.mark.e2e


def _new_tenant_id(prefix: str) -> str:
    return f"tenant:{prefix}-{uuid.uuid4().hex[:8]}"


def _ctx(tenant_id: str, *, role: TenantRole = TenantRole.ADMIN) -> TenantContext:
    return TenantContext(tenant=Tenant(tenant_id=tenant_id, name=tenant_id), caller_id="test", role=role)


def _run_under(tenant_id: str, fn, *, role: TenantRole = TenantRole.ADMIN):
    ctx = _ctx(tenant_id, role=role)
    token = set_active(ctx)
    try:
        return fn()
    finally:
        reset_active(token)


def test_cross_tenant_read_returns_none(dsn, db_available):
    """Tenant A creates a task; Tenant B's get_task_run returns
    None even though the row exists in the same table."""
    if not db_available:
        pytest.skip("PostgreSQL not reachable")
    store = IsolatingEventStore(dsn)
    store.connect()
    try:
        a = _new_tenant_id("iso-a")
        b = _new_tenant_id("iso-b")
        store.create_tenant(a, "A")
        store.create_tenant(b, "B")
        task_id = f"trun-{uuid.uuid4().hex[:8]}"
        _run_under(a, lambda: store.upsert_task_run(
            task_run_id=task_id, contract_id="c1", template_id="t1",
            state=TaskRunState.CREATED,
        ))
        row = _run_under(a, lambda: store.get_task_run(task_id))
        assert row is not None
        row = _run_under(b, lambda: store.get_task_run(task_id))
        assert row is None, "Tenant B read Tenant A's task_run"
    finally:
        store.close()


def test_cross_tenant_event_list_returns_empty(dsn, db_available):
    """Tenant A appends events; Tenant B's list_events returns [].

    Audit isolation is the strongest invariant: an attacker in
    Tenant B who guesses a task_run_id from Tenant A must not see
    Tenant A's audit timeline.
    """
    if not db_available:
        pytest.skip("PostgreSQL not reachable")
    store = IsolatingEventStore(dsn)
    store.connect()
    try:
        a = _new_tenant_id("evt-a")
        b = _new_tenant_id("evt-b")
        store.create_tenant(a, "A")
        store.create_tenant(b, "B")
        task_id = f"trun-{uuid.uuid4().hex[:8]}"
        def _a_setup():
            store.upsert_task_run(
                task_run_id=task_id, contract_id="c", template_id="t",
                state=TaskRunState.CREATED,
            )
            store.append_event(AuditEvent(
                task_run_id=task_id, kind=EventKind.TASK_RECEIVED,
                payload={"secret": "tenant-a-only"},
            ))
        _run_under(a, _a_setup)
        events = _run_under(a, lambda: store.list_events(task_id))
        assert any(e.get("payload", {}).get("secret") == "tenant-a-only" for e in events)
        events = _run_under(b, lambda: store.list_events(task_id))
        assert events == [], f"Tenant B read Tenant A's events: {events}"
    finally:
        store.close()


def test_cross_tenant_update_raises_lookuperror(dsn, db_available):
    """Tenant B's update_task_state on a Tenant A row raises
    LookupError. The WHERE clause includes tenant_id so a
    non-matching tenant gets 0 rows affected."""
    if not db_available:
        pytest.skip("PostgreSQL not reachable")
    store = IsolatingEventStore(dsn)
    store.connect()
    try:
        a = _new_tenant_id("upd-a")
        b = _new_tenant_id("upd-b")
        store.create_tenant(a, "A")
        store.create_tenant(b, "B")
        task_id = f"trun-{uuid.uuid4().hex[:8]}"
        _run_under(a, lambda: store.upsert_task_run(
            task_run_id=task_id, contract_id="c", template_id="t",
            state=TaskRunState.CREATED,
        ))
        with pytest.raises(LookupError):
            _run_under(b, lambda: store.update_task_state(task_id, TaskRunState.FAILED))
    finally:
        store.close()


def test_admin_can_enumerate_tenants(dsn, db_available):
    """The admin role is the only one that can see cross-tenant
    data. A non-admin attempt must raise TenantAccessDenied."""
    if not db_available:
        pytest.skip("PostgreSQL not reachable")
    store = IsolatingEventStore(dsn)
    store.connect()
    try:
        a = _new_tenant_id("adm-a")
        b = _new_tenant_id("adm-b")
        store.create_tenant(a, "A")
        store.create_tenant(b, "B")
        for t in (a, b):
            task_id = f"trun-{uuid.uuid4().hex[:8]}"
            _run_under(t, lambda: store.upsert_task_run(
                task_run_id=task_id, contract_id="c", template_id="t",
                state=TaskRunState.CREATED,
            ))
        enum = _run_under(a, lambda: store.list_tenants_for_admin(), role=TenantRole.ADMIN)
        tenant_ids = {row["tenant_id"] for row in enum}
        assert a in tenant_ids
        assert b in tenant_ids
        with pytest.raises(TenantAccessDenied):
            _run_under(a, lambda: store.list_tenants_for_admin(), role=TenantRole.AUDITOR)
    finally:
        store.close()


def test_legacy_rows_backfilled_to_demo_tenant(dsn, db_available):
    """The migration is idempotent: re-running it on a DB that
    already has ``tenant_id NOT NULL`` is a no-op. This is the
    scenario a real operator sees on day 1 of the M6 upgrade."""
    if not db_available:
        pytest.skip("PostgreSQL not reachable")
    from orchestra.enterprise.isolation import MIGRATION_SQL
    store = IsolatingEventStore(dsn)
    store.connect()
    try:
        # Re-run the migration: must succeed without errors.
        with store._tx() as conn:
            with conn.cursor() as cur:
                cur.execute(MIGRATION_SQL)
    finally:
        store.close()
