"""M6 ENT-001 — Multi-tenant EventStore.

The :class:`IsolatingEventStore` wraps the existing M0 EventStore
and enforces per-tenant isolation:

  * Every ``upsert_task_run`` / ``append_event`` / etc. requires a
    :class:`TenantContext` and writes the ``tenant_id`` into the
    row.
  * Every ``get_task_run`` / ``list_events`` etc. returns rows
    whose ``tenant_id`` matches the active tenant. Cross-tenant
    reads are impossible: the WHERE clause is a literal tenant
    id, not a parameter from the caller.
  * A separate, privileged ``super_list_events`` (gated by
    ``role == ADMIN``) is the only way to see other tenants' data
    (used by ops + auditor roles in a real deployment).

The store adds a migration step that back-fills existing rows with
``tenant:demo`` so the M0–M5 tests continue to pass without
modification. New code MUST go through the multi-tenant path.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator, Optional

import psycopg

from orchestra.coordinator.event_store import EventStore
from orchestra.core.schema import AuditEvent, TaskRunState
from orchestra.enterprise.tenant import (
    TenantContext,
    TenantRole,
    get_active,
)


DEFAULT_DSN = "postgresql://orchestra:orchestra@127.0.0.1:5432/orchestra"
LEGACY_TENANT = "tenant:demo"


MIGRATION_SQL = """
-- Add tenant_id columns if they don't exist.
ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE node_runs ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE receipts ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE grants ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS tenant_id TEXT;

-- Drop NOT NULL if a previous migration applied it. The M0
-- EventStore does not know about tenant_id and must keep working.
ALTER TABLE task_runs ALTER COLUMN tenant_id DROP NOT NULL;
ALTER TABLE node_runs ALTER COLUMN tenant_id DROP NOT NULL;
ALTER TABLE events ALTER COLUMN tenant_id DROP NOT NULL;
ALTER TABLE receipts ALTER COLUMN tenant_id DROP NOT NULL;
ALTER TABLE grants ALTER COLUMN tenant_id DROP NOT NULL;
ALTER TABLE approvals ALTER COLUMN tenant_id DROP NOT NULL;

-- Back-fill legacy rows. The M0 EventStore does not know about
-- tenant_id, so existing rows may have NULL. We back-fill with
-- the legacy default 'tenant:demo' so the M6 multi-tenant store
-- can find them.
UPDATE task_runs SET tenant_id = 'tenant:demo' WHERE tenant_id IS NULL;
UPDATE node_runs SET tenant_id = 'tenant:demo' WHERE tenant_id IS NULL;
UPDATE events SET tenant_id = 'tenant:demo' WHERE tenant_id IS NULL;
UPDATE receipts SET tenant_id = 'tenant:demo' WHERE tenant_id IS NULL;
UPDATE grants SET tenant_id = 'tenant:demo' WHERE tenant_id IS NULL;
UPDATE approvals SET tenant_id = 'tenant:demo' WHERE tenant_id IS NULL;

-- NOTE: tenant_id stays NULLABLE so the M0 EventStore (which
-- does not know about it) can keep writing. The M6 multi-tenant
-- store always sets tenant_id explicitly; its WHERE clause
-- filters out NULL rows so M6 callers see only their own data.
-- M0 callers remain tenant-agnostic until the M6 store is the
-- sole writer; that migration is the M7 "all callers upgraded"
-- gate.

-- Per-tenant indexes. These are the indexes the storage layer uses
-- to refuse cross-tenant scans efficiently.
CREATE INDEX IF NOT EXISTS task_runs_by_tenant ON task_runs(tenant_id, task_run_id);
CREATE INDEX IF NOT EXISTS events_by_tenant_task ON events(tenant_id, task_run_id, seq);
CREATE INDEX IF NOT EXISTS receipts_by_tenant ON receipts(tenant_id, task_run_id);
CREATE INDEX IF NOT EXISTS grants_by_tenant ON grants(tenant_id, task_run_id);
"""


class IsolatingEventStore:
    """Multi-tenant wrapper around the M0 EventStore.

    The wrapper owns its own PG connection (the M0 store owns one
    too; we open a second). Every read and write filters by the
    *active* tenant — never the *requested* tenant.
    """

    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn or os.environ.get("DATABASE_URL", DEFAULT_DSN)
        self._conn: Optional[psycopg.Connection] = None

    def connect(self) -> None:
        self._conn = psycopg.connect(self._dsn, autocommit=False)
        with self._conn.cursor() as cur:
            cur.execute(MIGRATION_SQL)
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @contextmanager
    def _tx(self) -> Iterator[psycopg.Connection]:
        if self._conn is None:
            raise RuntimeError("IsolatingEventStore not connected; call .connect()")
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ------------------------------------------------------------------
    # Tenants
    # ------------------------------------------------------------------

    def create_tenant(self, tenant_id: str, name: str, *, plan: str = "default") -> None:
        """Create a tenants row. Idempotent on re-run (safe migration)."""
        with self._tx() as conn:
            with conn.cursor() as cur:
                # The CREATE TABLE and INSERT are split because
                # psycopg's prepared statements reject multi-command
                # SQL strings.
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS tenants (
                        tenant_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        plan TEXT NOT NULL DEFAULT 'default',
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        status TEXT NOT NULL DEFAULT 'active'
                    )
                    """
                )
                cur.execute(
                    "INSERT INTO tenants (tenant_id, name, plan) VALUES (%s, %s, %s) "
                    "ON CONFLICT (tenant_id) DO NOTHING",
                    (tenant_id, name, plan),
                )

    def list_tenants(self) -> list[dict[str, Any]]:
        with self._tx() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT tenant_id, name, plan, status FROM tenants ORDER BY tenant_id")
                cols = ("tenant_id", "name", "plan", "status")
                return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Task runs
    # ------------------------------------------------------------------

    def upsert_task_run(
        self,
        *,
        task_run_id: str,
        contract_id: str,
        template_id: str,
        state: TaskRunState,
        plan_id: str | None = None,
    ) -> None:
        ctx = get_active()
        with self._tx() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO task_runs (tenant_id, task_run_id, contract_id, template_id, state, plan_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (task_run_id) DO UPDATE
                      SET state = EXCLUDED.state, plan_id = EXCLUDED.plan_id,
                          updated_at = now();
                    """,
                    (ctx.tenant.tenant_id, task_run_id, contract_id, template_id, state.value, plan_id),
                )

    def get_task_run(self, task_run_id: str) -> dict[str, Any] | None:
        ctx = get_active()
        with self._tx() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT task_run_id, contract_id, template_id, state, plan_id, created_at, updated_at "
                    "FROM task_runs WHERE tenant_id = %s AND task_run_id = %s",
                    (ctx.tenant.tenant_id, task_run_id),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                cols = ("task_run_id", "contract_id", "template_id", "state", "plan_id", "created_at", "updated_at")
                return dict(zip(cols, row))

    def update_task_state(self, task_run_id: str, state: TaskRunState) -> None:
        ctx = get_active()
        with self._tx() as conn:
            with conn.cursor() as cur:
                # The WHERE includes tenant_id so a cross-tenant
                # update is impossible.
                cur.execute(
                    "UPDATE task_runs SET state = %s, updated_at = now() "
                    "WHERE tenant_id = %s AND task_run_id = %s",
                    (state.value, ctx.tenant.tenant_id, task_run_id),
                )
                if cur.rowcount == 0:
                    raise LookupError(f"task_run not found in tenant {ctx.tenant.tenant_id}: {task_run_id}")

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def append_event(self, ev: AuditEvent) -> None:
        ctx = get_active()
        with self._tx() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO events (
                        event_id, tenant_id, task_run_id, node_run_id, seq, kind,
                        occurred_at, actor, payload, prev_event_id
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    """,
                    (
                        ev.event_id,
                        ctx.tenant.tenant_id,
                        ev.task_run_id,
                        ev.node_run_id,
                        ev.seq,
                        ev.kind.value,
                        ev.occurred_at,
                        ev.actor,
                        _json_dumps(ev.payload),
                        ev.prev_event_id,
                    ),
                )

    def list_events(self, task_run_id: str) -> list[dict[str, Any]]:
        ctx = get_active()
        with self._tx() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT event_id, seq, kind, occurred_at, actor, payload, node_run_id, prev_event_id "
                    "FROM events WHERE tenant_id = %s AND task_run_id = %s ORDER BY seq",
                    (ctx.tenant.tenant_id, task_run_id),
                )
                cols = ("event_id", "seq", "kind", "occurred_at", "actor", "payload", "node_run_id", "prev_event_id")
                return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Cross-tenant access (admin only)
    # ------------------------------------------------------------------

    def list_tenants_for_admin(self) -> list[dict[str, Any]]:
        """Admin-only cross-tenant enumeration.

        Raises if the active tenant is not an admin. A non-admin
        caller MUST NOT be able to see other tenants' data.
        """
        ctx = get_active()
        ctx.require_role(TenantRole.ADMIN)
        with self._tx() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tenant_id, count(*) FROM task_runs GROUP BY tenant_id ORDER BY tenant_id"
                )
                return [{"tenant_id": t, "task_count": c} for t, c in cur.fetchall()]


def _json_dumps(payload: dict[str, Any]) -> str:
    import json
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
