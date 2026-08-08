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
from orchestra.core.schema import AuditEvent, NodeRunState, SignedReceipt, TaskRunState

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
