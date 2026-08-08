"""PostgreSQL-backed Event Store for P0.

P0 uses a single PostgreSQL instance (per the dev plan: "复用 PostgreSQL").
The schema is intentionally simple — there is no Merkle log and no
multi-tenant row-level security (those are M5/M6). The store exposes an
async-friendly API; the Coordinator calls it inline.

Tables:
- ``task_runs``:    one row per submitted Contract.
- ``node_runs``:    one row per node execution.
- ``events``:       append-only audit events with a monotonic ``seq``.
- ``receipts``:     COSE-like envelopes keyed by ``(task_run_id, node_run_id)``.
- ``grants``:       issued Node Grants (so an auditor can replay auth).
- ``approvals``:    human approval records.

Connection: the store reads ``DATABASE_URL`` from the env, defaulting to a
local Postgres. If Postgres is unreachable, the store raises
:class:`EventStoreUnavailable`; the demo and tests must handle that
explicitly (no silent SQLite fallback — that would be a hidden swap).
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row

from orchestra.core.errors import OrchestraError
from orchestra.core.ids import new_id
from orchestra.core.schema import (
    ApprovalRecord,
    AuditEvent,
    BreakGlassRequest,
    BreakGlassState,
    DeletionJob,
    DeletionState,
    LegalHold,
    LifecyclePolicy,
    NodeRunState,
    ResourceKind,
    SignedReceipt,
    TaskRunState,
)

DEFAULT_DSN = "postgresql://orchestra:orchestra@127.0.0.1:5432/orchestra"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS task_runs (
    task_run_id   TEXT PRIMARY KEY,
    contract_id   TEXT NOT NULL,
    template_id   TEXT NOT NULL,
    state         TEXT NOT NULL,
    plan_id       TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS node_runs (
    node_run_id   TEXT PRIMARY KEY,
    task_run_id   TEXT NOT NULL REFERENCES task_runs(task_run_id) ON DELETE CASCADE,
    node_id       TEXT NOT NULL,
    state         TEXT NOT NULL,
    capability_id TEXT,
    manifest_id   TEXT,
    started_at    TIMESTAMPTZ,
    ended_at      TIMESTAMPTZ,
    output        JSONB
);
CREATE INDEX IF NOT EXISTS node_runs_by_task ON node_runs(task_run_id);

CREATE TABLE IF NOT EXISTS events (
    event_id      TEXT PRIMARY KEY,
    task_run_id   TEXT REFERENCES task_runs(task_run_id) ON DELETE CASCADE,
    node_run_id   TEXT REFERENCES node_runs(node_run_id) ON DELETE CASCADE,
    seq           BIGINT NOT NULL,
    kind          TEXT NOT NULL,
    occurred_at   TIMESTAMPTZ NOT NULL,
    actor         TEXT NOT NULL,
    payload       JSONB NOT NULL,
    prev_event_id TEXT
);
CREATE INDEX IF NOT EXISTS events_by_task_seq ON events(task_run_id, seq);
CREATE INDEX IF NOT EXISTS events_by_node ON events(node_run_id);

CREATE TABLE IF NOT EXISTS receipts (
    receipt_id    TEXT PRIMARY KEY,
    task_run_id   TEXT NOT NULL REFERENCES task_runs(task_run_id) ON DELETE CASCADE,
    node_run_id   TEXT NOT NULL REFERENCES node_runs(node_run_id) ON DELETE CASCADE,
    node_id       TEXT NOT NULL,
    envelope      JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS grants (
    grant_id      TEXT PRIMARY KEY,
    task_run_id   TEXT NOT NULL,
    node_run_id   TEXT NOT NULL,
    node_id       TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    capability_id TEXT NOT NULL,
    manifest_id   TEXT NOT NULL,
    data_view     JSONB NOT NULL,
    purpose       JSONB NOT NULL,
    issued_at     TIMESTAMPTZ NOT NULL,
    not_before    TIMESTAMPTZ,
    expires_at    TIMESTAMPTZ NOT NULL,
    audience      TEXT,
    signature     TEXT
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id   TEXT PRIMARY KEY,
    task_run_id   TEXT NOT NULL,
    node_id       TEXT NOT NULL,
    requested_at  TIMESTAMPTZ NOT NULL,
    decided_at    TIMESTAMPTZ,
    decision      TEXT,
    decided_by    TEXT,
    rationale     TEXT DEFAULT ''
);
-- M24 — Persistent approval workflow (ADR-0013): add columns to
-- ``approvals`` for tenant scoping, version-stamped atomic CAS,
-- two-person control, and identity binding. ``ADD COLUMN IF NOT
-- EXISTS`` keeps existing installs working.
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT '';
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS version BIGINT NOT NULL DEFAULT 0;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS state TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS required_approvers INT NOT NULL DEFAULT 1;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS requested_by TEXT NOT NULL DEFAULT '';
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS ticket TEXT;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS decision_payload JSONB;
-- M24: keep the index non-unique to avoid conflicts with legacy
-- rows that pre-date M24. Uniqueness is enforced at the
-- application layer (engine._approval_events + create_approval's
-- ON CONFLICT clause).
CREATE INDEX IF NOT EXISTS approvals_task_node ON approvals(task_run_id, node_id);
CREATE INDEX IF NOT EXISTS approvals_state ON approvals(state) WHERE state = 'pending';

-- M24 — Append-only approver log (ADR-0013). One row per
-- approve/reject decision; for break-glass there are two rows
-- per approval. ``UNIQUE (approval_id, decision_seq)`` makes the
-- second approver's race atomic.
CREATE TABLE IF NOT EXISTS approval_decisions (
    decision_id     TEXT PRIMARY KEY,
    approval_id     TEXT NOT NULL REFERENCES approvals(approval_id) ON DELETE CASCADE,
    decision_seq    INT NOT NULL,
    decided_by      TEXT NOT NULL,
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    decision        TEXT NOT NULL,
    rationale       TEXT DEFAULT '',
    identity_tenant TEXT NOT NULL DEFAULT '',
    UNIQUE (approval_id, decision_seq)
);

-- M24 — Break-glass requests (ADR-0012).
CREATE TABLE IF NOT EXISTS break_glass_requests (
    request_id        TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    task_run_id       TEXT,
    purpose           TEXT NOT NULL,
    effect            JSONB NOT NULL,
    resource_scope    JSONB NOT NULL,
    ticket            TEXT,
    requested_by      TEXT NOT NULL,
    requested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    state             TEXT NOT NULL DEFAULT 'requested',
    first_approver    TEXT,
    first_approved_at TIMESTAMPTZ,
    second_approver   TEXT,
    second_approved_at TIMESTAMPTZ,
    window_seconds    INT NOT NULL DEFAULT 900,
    activated_at      TIMESTAMPTZ,
    expires_at        TIMESTAMPTZ,
    revoked_by        TEXT,
    revoked_at        TIMESTAMPTZ,
    revoke_reason     TEXT
);
CREATE INDEX IF NOT EXISTS break_glass_state ON break_glass_requests(state) WHERE state IN ('requested', 'first-approved', 'active');
CREATE INDEX IF NOT EXISTS break_glass_tenant ON break_glass_requests(tenant_id, requested_at DESC);

-- M24 — Retention and Legal Hold (ADR-0014).
CREATE TABLE IF NOT EXISTS lifecycle_policies (
    policy_id         TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    resource_kind     TEXT NOT NULL,
    retention_seconds BIGINT NOT NULL,
    auto_delete       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, resource_kind)
);

CREATE TABLE IF NOT EXISTS legal_holds (
    hold_id        TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    case_id        TEXT NOT NULL,
    reason         TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by     TEXT NOT NULL,
    released_at    TIMESTAMPTZ,
    released_by    TEXT,
    release_reason TEXT,
    UNIQUE (tenant_id, case_id)
);
CREATE INDEX IF NOT EXISTS legal_holds_tenant ON legal_holds(tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS legal_hold_resources (
    hold_id        TEXT NOT NULL REFERENCES legal_holds(hold_id) ON DELETE CASCADE,
    resource_kind  TEXT NOT NULL,
    resource_id    TEXT NOT NULL,
    PRIMARY KEY (hold_id, resource_kind, resource_id)
);
CREATE INDEX IF NOT EXISTS legal_hold_resources_lookup ON legal_hold_resources(resource_kind, resource_id);

CREATE TABLE IF NOT EXISTS deletion_jobs (
    job_id            TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    resource_kind     TEXT NOT NULL,
    resource_id       TEXT NOT NULL,
    state             TEXT NOT NULL DEFAULT 'pending',
    attempt           INT NOT NULL DEFAULT 0,
    max_attempts      INT NOT NULL DEFAULT 3,
    requested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    requested_by      TEXT NOT NULL,
    completed_at      TIMESTAMPTZ,
    last_error        TEXT,
    deletion_evidence JSONB,
    UNIQUE (tenant_id, resource_kind, resource_id)
);
CREATE INDEX IF NOT EXISTS deletion_jobs_state ON deletion_jobs(state) WHERE state IN ('pending', 'running', 'partial');
CREATE INDEX IF NOT EXISTS deletion_jobs_tenant ON deletion_jobs(tenant_id, requested_at DESC);
"""


class EventStoreUnavailable(OrchestraError):
    """Raised when Postgres cannot be reached. Callers must surface this."""


class EventStore:
    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn or os.environ.get("DATABASE_URL", DEFAULT_DSN)
        self._conn: psycopg.Connection | None = None

    def connect(self) -> None:
        try:
            self._conn = psycopg.connect(self._dsn, autocommit=False)
            with self._conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
            self._conn.commit()
        except psycopg.OperationalError as e:
            raise EventStoreUnavailable(
                f"cannot connect to PostgreSQL at {self._dsn}: {e}"
            ) from e

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @contextmanager
    def _tx(self) -> Iterator[psycopg.Connection]:
        if self._conn is None:
            raise EventStoreUnavailable("EventStore not connected; call .connect()")
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # -- task_runs ---------------------------------------------------------

    def upsert_task_run(
        self,
        task_run_id: str,
        contract_id: str,
        template_id: str,
        state: TaskRunState,
        plan_id: str | None = None,
    ) -> None:
        with self._tx() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO task_runs (task_run_id, contract_id, template_id, state, plan_id)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (task_run_id) DO UPDATE
                SET state = EXCLUDED.state,
                    plan_id = EXCLUDED.plan_id,
                    updated_at = now()
                """,
                (task_run_id, contract_id, template_id, state.value, plan_id),
            )

    def update_task_state(self, task_run_id: str, state: TaskRunState) -> None:
        with self._tx() as c, c.cursor() as cur:
            cur.execute(
                "UPDATE task_runs SET state=%s, updated_at=now() WHERE task_run_id=%s",
                (state.value, task_run_id),
            )

    def get_task_run(self, task_run_id: str) -> dict[str, Any] | None:
        with self._tx() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM task_runs WHERE task_run_id=%s", (task_run_id,)
            )
            return cur.fetchone()

    def list_recent_task_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """M23 — Return the most recently created task runs.

        Powers the Demo Console's "Recent tasks" panel so a user
        who lost the URL of a task they just submitted can still
        find it. Ordered by ``created_at DESC`` so the newest
        submission always shows at the top of the list.
        """
        with self._tx() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT task_run_id, contract_id, template_id, state, "
                "       created_at, updated_at "
                "FROM task_runs "
                "ORDER BY created_at DESC "
                "LIMIT %s",
                (int(limit),),
            )
            return list(cur.fetchall())

    # -- node_runs ---------------------------------------------------------

    def upsert_node_run(
        self,
        node_run_id: str,
        task_run_id: str,
        node_id: str,
        state: NodeRunState,
        capability_id: str | None = None,
        manifest_id: str | None = None,
    ) -> None:
        with self._tx() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO node_runs
                  (node_run_id, task_run_id, node_id, state, capability_id, manifest_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (node_run_id) DO UPDATE
                SET state = EXCLUDED.state,
                    capability_id = COALESCE(EXCLUDED.capability_id, node_runs.capability_id),
                    manifest_id   = COALESCE(EXCLUDED.manifest_id, node_runs.manifest_id)
                """,
                (node_run_id, task_run_id, node_id, state.value, capability_id, manifest_id),
            )

    def update_node_state(
        self,
        node_run_id: str,
        state: NodeRunState,
        output: Any | None = None,
        started: bool = False,
        ended: bool = False,
    ) -> None:
        sets = ["state=%s"]
        params: list[Any] = [state.value]
        if started:
            sets.append("started_at=now()")
        if ended:
            sets.append("ended_at=now()")
        if output is not None:
            import json as _json

            sets.append("output=%s::jsonb")
            params.append(_json.dumps(output, ensure_ascii=False))
        params.append(node_run_id)
        with self._tx() as c, c.cursor() as cur:
            cur.execute(
                f"UPDATE node_runs SET {', '.join(sets)} WHERE node_run_id=%s",
                params,
            )

    def get_node_run(self, node_run_id: str) -> dict[str, Any] | None:
        with self._tx() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM node_runs WHERE node_run_id=%s", (node_run_id,)
            )
            return cur.fetchone()

    # -- events ------------------------------------------------------------

    def _next_seq(self, task_run_id: str) -> int:
        with self._tx() as c, c.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM events WHERE task_run_id=%s",
                (task_run_id,),
            )
            return int(cur.fetchone()[0])

    def append_event(self, ev: AuditEvent) -> None:
        if ev.seq == 0 and ev.task_run_id:
            ev = ev.model_copy(update={"seq": self._next_seq(ev.task_run_id)})
        import json as _json

        with self._tx() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO events
                  (event_id, task_run_id, node_run_id, seq, kind, occurred_at, actor, payload, prev_event_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    ev.event_id,
                    ev.task_run_id,
                    ev.node_run_id,
                    ev.seq,
                    ev.kind.value,
                    ev.occurred_at,
                    ev.actor,
                    _json.dumps(ev.payload, ensure_ascii=False),
                    ev.prev_event_id,
                ),
            )

    def list_events(
        self, task_run_id: str | None = None, node_run_id: str | None = None
    ) -> list[dict[str, Any]]:
        import json as _json

        clauses: list[str] = []
        params: list[Any] = []
        if task_run_id:
            clauses.append("task_run_id=%s")
            params.append(task_run_id)
        if node_run_id:
            clauses.append("node_run_id=%s")
            params.append(node_run_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._tx() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT * FROM events {where} ORDER BY seq ASC, occurred_at ASC",
                params,
            )
            rows = cur.fetchall()
        for r in rows:
            if isinstance(r.get("payload"), str):
                r["payload"] = _json.loads(r["payload"])
        return rows

    # -- grants ------------------------------------------------------------

    def save_grant(self, grant_payload: dict[str, Any]) -> None:
        import json as _json

        with self._tx() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO grants
                  (grant_id, task_run_id, node_run_id, node_id, task_id,
                   capability_id, manifest_id, data_view, purpose,
                   issued_at, not_before, expires_at, audience, signature)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                        %s, %s, %s, %s, %s)
                """,
                (
                    grant_payload["grant_id"],
                    grant_payload["task_run_id"],
                    grant_payload["node_run_id"],
                    grant_payload.get("node_id", ""),
                    grant_payload.get("task_id", ""),
                    grant_payload["capability_id"],
                    grant_payload["manifest_id"],
                    _json.dumps(grant_payload["data_view"], ensure_ascii=False),
                    _json.dumps(grant_payload["purpose"], ensure_ascii=False),
                    grant_payload["issued_at"],
                    grant_payload.get("not_before"),
                    grant_payload["expires_at"],
                    grant_payload.get("audience"),
                    grant_payload.get("signature"),
                ),
            )

    def list_grants(self, task_run_id: str) -> list[dict[str, Any]]:
        import json as _json

        with self._tx() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM grants WHERE task_run_id=%s ORDER BY issued_at",
                (task_run_id,),
            )
            rows = cur.fetchall()
        for r in rows:
            for k in ("data_view", "purpose"):
                if isinstance(r.get(k), str):
                    r[k] = _json.loads(r[k])
        return rows

    # -- receipts ----------------------------------------------------------

    def save_receipt(self, r: SignedReceipt) -> None:
        import json as _json

        with self._tx() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO receipts (receipt_id, task_run_id, node_run_id, node_id, envelope)
                VALUES (%s, %s, %s, %s, %s::jsonb)
                """,
                (r.receipt_id, r.task_run_id, r.node_run_id, r.node_id, _json.dumps(r.envelope)),
            )

    def get_receipts(self, task_run_id: str) -> list[dict[str, Any]]:
        import json as _json

        with self._tx() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM receipts WHERE task_run_id=%s ORDER BY created_at",
                (task_run_id,),
            )
            rows = cur.fetchall()
        for r in rows:
            if isinstance(r.get("envelope"), str):
                r["envelope"] = _json.loads(r["envelope"])
        return rows

    # -- approvals ---------------------------------------------------------

    def save_approval(self, payload: dict[str, Any]) -> None:
        with self._tx() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO approvals
                  (approval_id, task_run_id, node_id, requested_at, decided_at,
                   decision, decided_by, rationale)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    payload["approval_id"],
                    payload["task_run_id"],
                    payload["node_id"],
                    payload["requested_at"],
                    payload.get("decided_at"),
                    payload.get("decision"),
                    payload.get("decided_by"),
                    payload.get("rationale", ""),
                ),
            )

    def list_approvals(self, task_run_id: str) -> list[dict[str, Any]]:
        with self._tx() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM approvals WHERE task_run_id=%s ORDER BY requested_at",
                (task_run_id,),
            )
            return cur.fetchall()

    # -- M24 — Persistent approval workflow (ADR-0013) --------------------

    def create_approval(
        self,
        *,
        task_run_id: str,
        node_id: str,
        tenant_id: str,
        requested_by: str,
        required_approvers: int = 1,
        ticket: str | None = None,
    ) -> ApprovalRecord:
        """Create a pending ApprovalRecord. Idempotent on
        (task_run_id, node_id): re-creating for the same gate
        returns the existing row.
        """
        import json as _json

        with self._tx() as c, c.cursor(row_factory=dict_row) as cur:
            # M24 — the ``approvals`` table has a non-unique
            # index on ``(task_run_id, node_id)`` (so legacy
            # pre-M24 rows don't conflict with the new schema).
            # Idempotency is enforced at the application layer:
            # SELECT-FOR-UPDATE the existing row first; if
            # missing, INSERT. The lock prevents a racing
            # insert from sneaking through.
            cur.execute(
                """
                SELECT * FROM approvals
                WHERE task_run_id = %s AND node_id = %s
                FOR UPDATE
                """,
                (task_run_id, node_id),
            )
            existing = cur.fetchone()
            if existing is not None:
                row = existing
            else:
                cur.execute(
                    """
                    INSERT INTO approvals
                      (approval_id, task_run_id, node_id, tenant_id, version,
                       state, required_approvers, requested_at, requested_by, ticket)
                    VALUES (%s, %s, %s, %s, 0, 'pending', %s, now(), %s, %s)
                    RETURNING *
                    """,
                    (
                        f"apv:{new_id()[:12]}",
                        task_run_id,
                        node_id,
                        tenant_id,
                        int(required_approvers),
                        requested_by,
                        ticket,
                    ),
                )
                row = cur.fetchone()
        # Coerce to the M24 ApprovalRecord shape. The DB row
        # also carries the legacy P0 columns (decision,
        # decided_by, rationale) that the new model doesn't
        # accept; we drop them. ``requested_at`` comes back as
        # ``datetime`` from psycopg; the model wants a string.
        if isinstance(row.get("decision_payload"), str):
            row["decision_payload"] = _json.loads(row["decision_payload"])
        for legacy in ("decision", "decided_by", "rationale"):
            row.pop(legacy, None)
        if row.get("requested_at") is not None and not isinstance(
            row["requested_at"], str
        ):
            row["requested_at"] = str(row["requested_at"])
        if row.get("decided_at") is not None and not isinstance(
            row["decided_at"], str
        ):
            row["decided_at"] = str(row["decided_at"])
        return ApprovalRecord.model_validate(row)

    def record_approval_decision(
        self,
        *,
        approval_id: str,
        decision: str,  # "approve" | "reject"
        decided_by: str,
        identity_tenant_id: str,
        rationale: str = "",
    ) -> dict[str, Any]:
        """Atomically record a decision and update the parent
        approval. The atomic compare-and-set is the production
        guarantee from ADR-0013.

        Returns a dict with:
          - ``applied``: bool — True if this caller's decision
            was the one that flipped the row.
          - ``state``: the new approval state
          - ``version``: the new version
          - ``reason``: when not applied, why
        """
        import json as _json

        with self._tx() as c, c.cursor(row_factory=dict_row) as cur:
            # 1. Look up the current row to know required_approvers
            #    and existing decisions.
            cur.execute(
                """
                SELECT a.approval_id, a.tenant_id, a.state, a.required_approvers,
                       a.version, a.decision_payload,
                       (SELECT count(*) FROM approval_decisions d
                         WHERE d.approval_id = a.approval_id) AS decisions_seen
                FROM approvals a
                WHERE a.approval_id = %s
                FOR UPDATE
                """,
                (approval_id,),
            )
            row = cur.fetchone()
            if row is None:
                return {"applied": False, "reason": "not_found"}
            if row["state"] not in ("pending", "first-approved"):
                return {"applied": False, "reason": "already_terminal", "state": row["state"]}
            if row["tenant_id"] != identity_tenant_id:
                return {"applied": False, "reason": "cross_tenant"}
            if decision not in ("approve", "reject"):
                return {"applied": False, "reason": "bad_decision"}

            seen = int(row["decisions_seen"])
            new_seq = seen + 1

            # 2. Insert the decision row (UNIQUE on (approval_id, decision_seq)
            #    gives us the atomic guard).
            cur.execute(
                """
                INSERT INTO approval_decisions
                  (decision_id, approval_id, decision_seq, decided_by,
                   decided_at, decision, rationale, identity_tenant)
                VALUES (%s, %s, %s, %s, now(), %s, %s, %s)
                """,
                (
                    f"apvd:{new_id()[:12]}",
                    approval_id,
                    new_seq,
                    decided_by,
                    decision,
                    rationale,
                    identity_tenant_id,
                ),
            )

            # 3. Compute the new approval state.
            required = int(row["required_approvers"])
            if decision == "reject":
                new_state = "rejected"
            elif required == 1:
                new_state = "approved"
            elif required == 2 and new_seq == 1:
                new_state = "first-approved"
            elif required == 2 and new_seq >= 2:
                # Defensive: required_approvers > 2 is not supported.
                new_state = "approved"
            else:
                new_state = row["state"]

            new_payload = {"decision": decision, "by": decided_by, "rationale": rationale}

            cur.execute(
                """
                UPDATE approvals
                SET state = %s,
                    decided_at = now(),
                    decision_payload = %s::jsonb,
                    version = version + 1
                WHERE approval_id = %s
                RETURNING state, version
                """,
                (new_state, _json.dumps(new_payload), approval_id),
            )
            updated = cur.fetchone()
            return {
                "applied": True,
                "state": updated["state"],
                "version": int(updated["version"]),
                "decisions_seen": new_seq,
                "required_approvers": required,
            }

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        with self._tx() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM approvals WHERE approval_id=%s", (approval_id,)
            )
            return cur.fetchone()

    def list_approvals_for_tenant(
        self, tenant_id: str, *, state: str | None = None
    ) -> list[dict[str, Any]]:
        """List pending (or terminal) approvals for a tenant.
        Used by the engine to reload pending approvals on startup.
        """
        with self._tx() as c, c.cursor(row_factory=dict_row) as cur:
            if state is None:
                cur.execute(
                    "SELECT * FROM approvals WHERE tenant_id=%s ORDER BY requested_at",
                    (tenant_id,),
                )
            else:
                cur.execute(
                    "SELECT * FROM approvals WHERE tenant_id=%s AND state=%s ORDER BY requested_at",
                    (tenant_id, state),
                )
            return list(cur.fetchall())

    def list_approval_decisions(self, approval_id: str) -> list[dict[str, Any]]:
        with self._tx() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM approval_decisions WHERE approval_id=%s ORDER BY decision_seq",
                (approval_id,),
            )
            return list(cur.fetchall())

    # -- M24 — Break-glass (ADR-0012) ---------------------------------------

    def create_break_glass_request(self, req: BreakGlassRequest) -> BreakGlassRequest:
        import json as _json

        with self._tx() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO break_glass_requests
                  (request_id, tenant_id, task_run_id, purpose, effect,
                   resource_scope, ticket, requested_by, requested_at,
                   state, window_seconds)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, now(),
                        'requested', %s)
                """,
                (
                    req.request_id,
                    req.tenant_id,
                    req.task_run_id,
                    req.purpose,
                    _json.dumps(req.effect),
                    _json.dumps(req.resource_scope),
                    req.ticket,
                    req.requested_by,
                    int(req.window_seconds),
                ),
            )
        return req

    def get_break_glass(self, request_id: str) -> dict[str, Any] | None:
        with self._tx() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM break_glass_requests WHERE request_id=%s",
                (request_id,),
            )
            return cur.fetchone()

    def list_break_glass_for_tenant(
        self, tenant_id: str, *, state: str | None = None
    ) -> list[dict[str, Any]]:
        with self._tx() as c, c.cursor(row_factory=dict_row) as cur:
            if state is None:
                cur.execute(
                    "SELECT * FROM break_glass_requests WHERE tenant_id=%s "
                    "ORDER BY requested_at DESC",
                    (tenant_id,),
                )
            else:
                cur.execute(
                    "SELECT * FROM break_glass_requests WHERE tenant_id=%s "
                    "AND state=%s ORDER BY requested_at DESC",
                    (tenant_id, state),
                )
            return list(cur.fetchall())

    def record_break_glass_approval(
        self,
        *,
        request_id: str,
        approver: str,
        identity_tenant_id: str,
    ) -> dict[str, Any]:
        """Atomically transition requested → first-approved → active.

        The applicant cannot be the approver. The two approvers must
        be distinct identities. Cross-tenant is denied.
        """
        with self._tx() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT request_id, tenant_id, state, requested_by,
                       first_approver, window_seconds
                FROM break_glass_requests
                WHERE request_id = %s
                FOR UPDATE
                """,
                (request_id,),
            )
            row = cur.fetchone()
            if row is None:
                return {"applied": False, "reason": "not_found"}
            if row["tenant_id"] != identity_tenant_id:
                return {"applied": False, "reason": "cross_tenant"}
            if row["state"] not in ("requested", "first-approved"):
                return {"applied": False, "reason": "not_approvable", "state": row["state"]}
            if row["requested_by"] == approver:
                return {"applied": False, "reason": "applicant_cannot_approve"}
            if row["first_approver"] and row["first_approver"] == approver:
                return {"applied": False, "reason": "already_approved_by_you"}

            if row["state"] == "requested":
                cur.execute(
                    """
                    UPDATE break_glass_requests
                    SET state = 'first-approved',
                        first_approver = %s,
                        first_approved_at = now()
                    WHERE request_id = %s
                    """,
                    (approver, request_id),
                )
                return {
                    "applied": True,
                    "state": "first-approved",
                    "approver": approver,
                    "required_next": "second_approver",
                }
            # second approver: activate
            cur.execute(
                """
                UPDATE break_glass_requests
                SET state = 'active',
                    second_approver = %s,
                    second_approved_at = now(),
                    activated_at = now(),
                    expires_at = now() + (window_seconds * interval '1 second')
                WHERE request_id = %s
                RETURNING expires_at
                """,
                (approver, request_id),
            )
            updated = cur.fetchone()
            return {
                "applied": True,
                "state": "active",
                "approver": approver,
                "expires_at": str(updated["expires_at"]) if updated else None,
            }

    def revoke_break_glass(
        self,
        *,
        request_id: str,
        revoker: str,
        identity_tenant_id: str,
        reason: str = "",
    ) -> dict[str, Any]:
        """Revoke a Break-glass request. Either approver or any
        operator with kill-switch role can call this.
        """
        with self._tx() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT tenant_id, state FROM break_glass_requests
                WHERE request_id = %s
                FOR UPDATE
                """,
                (request_id,),
            )
            row = cur.fetchone()
            if row is None:
                return {"applied": False, "reason": "not_found"}
            if row["tenant_id"] != identity_tenant_id:
                return {"applied": False, "reason": "cross_tenant"}
            if row["state"] in ("expired", "revoked"):
                return {"applied": False, "reason": "already_terminal"}
            cur.execute(
                """
                UPDATE break_glass_requests
                SET state = 'revoked',
                    revoked_by = %s,
                    revoked_at = now(),
                    revoke_reason = %s
                WHERE request_id = %s
                """,
                (revoker, reason, request_id),
            )
            return {"applied": True, "state": "revoked"}

    def sweep_expired_break_glass(self, now: str | None = None) -> list[str]:
        """Mark any 'active' break-glass whose ``expires_at`` has
        passed as 'expired'. Returns the list of freshly expired
        request_ids. Idempotent.
        """
        with self._tx() as c, c.cursor() as cur:
            cur.execute(
                """
                UPDATE break_glass_requests
                SET state = 'expired'
                WHERE state = 'active' AND expires_at IS NOT NULL
                  AND expires_at < now()
                RETURNING request_id
                """
            )
            return [r[0] for r in cur.fetchall()]

    # -- M24 — Retention and Legal Hold (ADR-0014) ------------------------

    def upsert_lifecycle_policy(self, policy: LifecyclePolicy) -> None:
        with self._tx() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO lifecycle_policies
                  (policy_id, tenant_id, resource_kind, retention_seconds, auto_delete)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, resource_kind) DO UPDATE
                  SET retention_seconds = EXCLUDED.retention_seconds,
                      auto_delete = EXCLUDED.auto_delete
                """,
                (
                    policy.policy_id,
                    policy.tenant_id,
                    policy.resource_kind.value,
                    int(policy.retention_seconds),
                    bool(policy.auto_delete),
                ),
            )

    def get_lifecycle_policy(
        self, tenant_id: str, resource_kind: ResourceKind
    ) -> dict[str, Any] | None:
        with self._tx() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM lifecycle_policies "
                "WHERE tenant_id=%s AND resource_kind=%s",
                (tenant_id, resource_kind.value),
            )
            return cur.fetchone()

    def create_legal_hold(
        self,
        hold: LegalHold,
        *,
        resource_pairs: list[tuple[ResourceKind, str]] | None = None,
    ) -> LegalHold:
        with self._tx() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO legal_holds
                  (hold_id, tenant_id, case_id, reason, created_at, created_by)
                VALUES (%s, %s, %s, %s, now(), %s)
                """,
                (hold.hold_id, hold.tenant_id, hold.case_id, hold.reason, hold.created_by),
            )
            if resource_pairs:
                for kind, rid in resource_pairs:
                    cur.execute(
                        """
                        INSERT INTO legal_hold_resources
                          (hold_id, resource_kind, resource_id)
                        VALUES (%s, %s, %s)
                        """,
                        (hold.hold_id, kind.value, rid),
                    )
        return hold

    def release_legal_hold(
        self,
        *,
        hold_id: str,
        released_by: str,
        identity_tenant_id: str,
        reason: str = "",
    ) -> dict[str, Any]:
        with self._tx() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT tenant_id, released_at FROM legal_holds "
                "WHERE hold_id = %s FOR UPDATE",
                (hold_id,),
            )
            row = cur.fetchone()
            if row is None:
                return {"applied": False, "reason": "not_found"}
            if row["tenant_id"] != identity_tenant_id:
                return {"applied": False, "reason": "cross_tenant"}
            if row["released_at"] is not None:
                return {"applied": False, "reason": "already_released"}
            cur.execute(
                """
                UPDATE legal_holds
                SET released_at = now(),
                    released_by = %s,
                    release_reason = %s
                WHERE hold_id = %s
                """,
                (released_by, reason, hold_id),
            )
            return {"applied": True}

    def list_legal_holds(
        self, tenant_id: str, *, active_only: bool = True
    ) -> list[dict[str, Any]]:
        """Return holds for a tenant with the resources
        rolled up as two parallel lists (``resource_kind``
        and ``resource_id``) so a caller can match a
        specific resource without a second query.
        """
        with self._tx() as c, c.cursor(row_factory=dict_row) as cur:
            if active_only:
                cur.execute(
                    "SELECT h.*, "
                    "       COALESCE("
                    "           (SELECT json_agg(json_build_object("
                    "                'resource_kind', r.resource_kind,"
                    "                'resource_id', r.resource_id"
                    "            ) ORDER BY r.resource_kind, r.resource_id) "
                    "            FROM legal_hold_resources r "
                    "            WHERE r.hold_id = h.hold_id),"
                    "           '[]'::json"
                    "       ) AS resources "
                    "FROM legal_holds h "
                    "WHERE h.tenant_id=%s AND h.released_at IS NULL "
                    "ORDER BY h.created_at DESC",
                    (tenant_id,),
                )
            else:
                cur.execute(
                    "SELECT h.*, "
                    "       COALESCE("
                    "           (SELECT json_agg(json_build_object("
                    "                'resource_kind', r.resource_kind,"
                    "                'resource_id', r.resource_id"
                    "            ) ORDER BY r.resource_kind, r.resource_id) "
                    "            FROM legal_hold_resources r "
                    "            WHERE r.hold_id = h.hold_id),"
                    "           '[]'::json"
                    "       ) AS resources "
                    "FROM legal_holds h "
                    "WHERE h.tenant_id=%s "
                    "ORDER BY h.created_at DESC",
                    (tenant_id,),
                )
            rows = list(cur.fetchall())
        # Flatten the resources list into two parallel lists
        # for the legacy callers.
        import json as _json

        for r in rows:
            res = r.pop("resources", None)
            if res is None:
                r["resource_kind"] = []
                r["resource_id"] = []
                continue
            if isinstance(res, str):
                res = _json.loads(res)
            kinds = [item["resource_kind"] for item in res]
            ids = [item["resource_id"] for item in res]
            r["resource_kind"] = kinds
            r["resource_id"] = ids
        return rows

    def is_resource_held(
        self, tenant_id: str, resource_kind: ResourceKind, resource_id: str
    ) -> bool:
        """Return True if a Legal Hold currently covers this
        resource. ``False`` means deletion is allowed (subject
        to the lifecycle policy).
        """
        with self._tx() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM legal_holds h
                JOIN legal_hold_resources r ON r.hold_id = h.hold_id
                WHERE h.tenant_id = %s
                  AND h.released_at IS NULL
                  AND r.resource_kind = %s
                  AND r.resource_id = %s
                LIMIT 1
                """,
                (tenant_id, resource_kind.value, resource_id),
            )
            return cur.fetchone() is not None

    def create_deletion_job(
        self,
        job: DeletionJob,
    ) -> DeletionJob:
        """Idempotent: UNIQUE (tenant_id, resource_kind, resource_id)."""
        import json as _json

        with self._tx() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                INSERT INTO deletion_jobs
                  (job_id, tenant_id, resource_kind, resource_id, state,
                   attempt, max_attempts, requested_at, requested_by)
                VALUES (%s, %s, %s, %s, 'pending', 0, %s, now(), %s)
                ON CONFLICT (tenant_id, resource_kind, resource_id) DO UPDATE
                  SET state = deletion_jobs.state
                RETURNING job_id, state, attempt
                """,
                (
                    job.job_id,
                    job.tenant_id,
                    job.resource_kind.value,
                    job.resource_id,
                    int(job.max_attempts),
                    job.requested_by,
                ),
            )
            row = cur.fetchone()
        # Return the existing or new job with the actual row state.
        return DeletionJob(
            job_id=row["job_id"],
            tenant_id=job.tenant_id,
            resource_kind=job.resource_kind,
            resource_id=job.resource_id,
            state=DeletionState(row["state"]),
            attempt=int(row["attempt"]),
            max_attempts=job.max_attempts,
            requested_by=job.requested_by,
        )

    def update_deletion_job(
        self,
        *,
        job_id: str,
        state: DeletionState,
        last_error: str | None = None,
        evidence: dict[str, Any] | None = None,
        increment_attempt: bool = False,
    ) -> None:
        import json as _json

        with self._tx() as c, c.cursor() as cur:
            cur.execute(
                """
                UPDATE deletion_jobs
                SET state = %s,
                    attempt = attempt + %s,
                    completed_at = CASE
                        WHEN %s IN ('deleted', 'partial', 'failed', 'held') THEN now()
                        ELSE completed_at
                    END,
                    last_error = COALESCE(%s, last_error),
                    deletion_evidence = COALESCE(%s::jsonb, deletion_evidence)
                WHERE job_id = %s
                """,
                (
                    state.value,
                    1 if increment_attempt else 0,
                    state.value,
                    last_error,
                    _json.dumps(evidence) if evidence is not None else None,
                    job_id,
                ),
            )

    def get_deletion_job(self, job_id: str) -> dict[str, Any] | None:
        with self._tx() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM deletion_jobs WHERE job_id=%s", (job_id,))
            return cur.fetchone()

    def list_deletion_jobs(
        self, tenant_id: str, *, state: DeletionState | None = None
    ) -> list[dict[str, Any]]:
        with self._tx() as c, c.cursor(row_factory=dict_row) as cur:
            if state is None:
                cur.execute(
                    "SELECT * FROM deletion_jobs WHERE tenant_id=%s "
                    "ORDER BY requested_at DESC",
                    (tenant_id,),
                )
            else:
                cur.execute(
                    "SELECT * FROM deletion_jobs WHERE tenant_id=%s AND state=%s "
                    "ORDER BY requested_at DESC",
                    (tenant_id, state.value),
                )
            return list(cur.fetchall())

    # -- M24 DLM-001 — redaction primitives used by LifecycleManager --------

    def redact_event_payload(self, tenant_id: str, event_id: str) -> bool:
        """Replace ``events.payload`` with ``{"redacted": true}`` (ADR-0014).

        The audit row stays — the event is part of the permanent
        audit trail, but the payload it carried is replaced with
        a sentinel. Returns True if a row was updated.

        Tenant scope is enforced by the lifecycle manager
        (the caller cross-checks before calling). The
        ``task_runs.tenant_id`` column is nullable in the M0
        schema; the JOIN-clause approach would silently miss
        events whose task is un-tenant-scoped, so we update
        by ``event_id`` alone and rely on the manager's gate.
        """
        import json as _json

        with self._tx() as c, c.cursor() as cur:
            cur.execute(
                """
                UPDATE events
                SET payload = %s::jsonb
                WHERE event_id = %s
                """,
                (_json.dumps({"redacted": True}), event_id),
            )
            return cur.rowcount > 0

    def list_events_for_resource(
        self, tenant_id: str, *, task_run_id: str | None = None
    ) -> list[dict[str, Any]]:
        """M24 — list events scoped to a tenant. Used by the
        event redaction adapter to find every event whose
        payload references a given resource (e.g. a receipt
        id or an artifact id). The dev path filters by
        ``task_run_id``; the production swap walks a
        ``payload @> {"resource_id": ...}`` JSONB index.

        The tenant scope uses a LEFT JOIN on
        ``task_runs.tenant_id`` so we don't silently miss
        events whose task row was created by the M0
        EventStore (which leaves ``tenant_id`` NULL). The
        M6 store back-fills ``tenant_id`` so the same
        query works against multi-tenant data.
        """
        import json as _json

        with self._tx() as c, c.cursor(row_factory=dict_row) as cur:
            if task_run_id is not None:
                cur.execute(
                    """
                    SELECT e.*
                    FROM events e
                    LEFT JOIN task_runs t ON t.task_run_id = e.task_run_id
                    WHERE (t.tenant_id IS NULL OR t.tenant_id = %s)
                      AND e.task_run_id = %s
                    ORDER BY e.seq ASC
                    """,
                    (tenant_id, task_run_id),
                )
            else:
                cur.execute(
                    """
                    SELECT e.*
                    FROM events e
                    LEFT JOIN task_runs t ON t.task_run_id = e.task_run_id
                    WHERE (t.tenant_id IS NULL OR t.tenant_id = %s)
                    ORDER BY e.occurred_at DESC
                    """,
                    (tenant_id,),
                )
            rows = list(cur.fetchall())
        for r in rows:
            if isinstance(r.get("payload"), str):
                r["payload"] = _json.loads(r["payload"])
        return rows

    def get_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        with self._tx() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM receipts WHERE receipt_id=%s", (receipt_id,)
            )
            return cur.fetchone()

    def delete_receipt(self, receipt_id: str) -> bool:
        """Hard-delete a receipt row. The events table keeps
        its own row (the audit trail is permanent) but the
        ``receipt.signed`` event's payload is replaced with
        ``{"redacted": true}`` by the caller.
        """
        with self._tx() as c, c.cursor() as cur:
            cur.execute(
                "DELETE FROM receipts WHERE receipt_id=%s", (receipt_id,)
            )
            return cur.rowcount > 0

    def find_receipts_for_resource(
        self, tenant_id: str, resource_id: str
    ) -> list[dict[str, Any]]:
        """Find receipts whose envelope (or related task) ties
        them to a given resource. The dev path matches
        receipts where ``node_id`` or the envelope's body
        contains the resource id; production swaps for a
        dedicated ``receipt_resources`` join table.
        """
        import json as _json

        with self._tx() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT r.*
                FROM receipts r
                JOIN task_runs t ON t.task_run_id = r.task_run_id
                WHERE t.tenant_id = %s
                  AND (r.node_id = %s OR r.envelope::text LIKE %s)
                """,
                (tenant_id, resource_id, f"%{resource_id}%"),
            )
            rows = list(cur.fetchall())
        for r in rows:
            if isinstance(r.get("envelope"), str):
                r["envelope"] = _json.loads(r["envelope"])
        return rows
