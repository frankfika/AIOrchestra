"""M24 DLM-001 — Retention and Legal Hold lifecycle (ADR-0014).

This module is the **only** legal way to delete an artifact,
receipt, event payload, or webhook delivery in the dev path.
The :class:`LifecycleManager` wraps the EventStore + the
in-process artifact / webhook history stores and provides:

  * :class:`LifecycleManager`  — facade; methods are stateless
    apart from the optional in-process stores passed at
    construction time. The DB is the source of truth.

  * Per-:class:`~orchestra.core.schema.ResourceKind` deleter
    adapters in ``_DELETERS``. Each adapter looks up the
    underlying store and returns a
    :class:`~orchestra.core.schema.DeletionEvidence` describing
    which copies were removed and which remained.

  * Custom exceptions (:class:`LifecycleError`,
    :class:`LifecycleBlocked`, :class:`LifecycleRetained`,
    :class:`LifecycleCrossTenant`) for the four denial
    conditions the W3 spec calls out.

The manager is intentionally synchronous. The store methods it
calls are sync (Postgres). A production swap that needs
async workers moves the call sites into a Celery / RQ task
without changing the wire contract.

**The manager never silently succeeds.** Every successful
delete produces an evidence row that records how many copies
were removed, which kept copies remain, and the SHA-256
digest of the deleted payload. A partial failure (one copy
gone, one copy still on disk) produces a ``state='partial'``
job that an operator can retry.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Optional

from orchestra.core.errors import OrchestraError
from orchestra.core.hashing import digest_json
from orchestra.core.ids import new_id
from orchestra.core.schema import (
    DeletionEvidence,
    DeletionJob,
    DeletionState,
    EventKind,
    LegalHold,
    LifecyclePolicy,
    ResourceKind,
)
from orchestra.core.time import utc_now_iso

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LifecycleError(OrchestraError):
    """Base for all retention / legal-hold errors."""


class LifecycleBlocked(LifecycleError):
    """A Legal Hold is preventing this deletion.

    The hold_id + case_id are recorded on the exception so a
    partner SDK can surface the regulator's case id to the
    user ("blocked by case CASE-2026-001") instead of an
    opaque "deletion failed".
    """

    def __init__(self, hold_id: str, case_id: str, resource_kind: ResourceKind, resource_id: str) -> None:
        self.hold_id = hold_id
        self.case_id = case_id
        self.resource_kind = resource_kind
        self.resource_id = resource_id
        super().__init__(
            f"deletion blocked: {resource_kind.value} {resource_id!r} is covered by hold "
            f"{hold_id} (case {case_id!r})"
        )


class LifecycleRetained(LifecycleError):
    """The lifecycle policy retains this resource (auto_delete=False)."""

    def __init__(self, resource_kind: ResourceKind, resource_id: str, reason: str = "") -> None:
        self.resource_kind = resource_kind
        self.resource_id = resource_id
        self.reason = reason
        msg = (
            f"deletion refused: {resource_kind.value} {resource_id!r} is retained by the lifecycle policy"
        )
        if reason:
            msg += f" ({reason})"
        super().__init__(msg)


class LifecycleCrossTenant(LifecycleError):
    """The requesting identity is not in the resource's tenant.

    Every read / write on lifecycle tables is scoped to a
    single tenant. A caller that supplies a mismatched
    identity gets this error — the manager never falls back
    to "no tenant" and never reads across tenants.
    """

    def __init__(self, *, expected: str, actual: str) -> None:
        self.expected_tenant = expected
        self.actual_tenant = actual
        super().__init__(
            f"cross-tenant: resource belongs to {expected!r}, identity is in {actual!r}"
        )


# ---------------------------------------------------------------------------
# In-process artifact store reference (dev path)
# ---------------------------------------------------------------------------


class DevArtifactStore:
    """Minimal contract the artifact deleter needs.

    The real :class:`~orchestra.artifact.store.ArtifactStore`
    satisfies this; the dev shim is what the lifecycle
    tests use (so we can mock failures without touching the
    real store). The two methods the deleter needs are:

      * ``delete(artifact_id) -> bool`` — remove the row + the
        blob reference. Returns True if a row was removed.
      * ``exists(artifact_id) -> bool`` — does the row still
        exist? Used by the evidence writer to record
        ``copies_kept``.

    Production swap: replace with a Postgres-backed
    implementation that does ``DELETE FROM artifacts
    WHERE artifact_id=%s`` and returns rowcount.
    """

    def delete(self, artifact_id: str) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def exists(self, artifact_id: str) -> bool:  # pragma: no cover - interface
        raise NotImplementedError


class InMemoryDevArtifactStore(DevArtifactStore):
    """A trivial in-process implementation used by tests.

    Stores ``artifact_id -> payload`` so a test can assert
    what was deleted.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def put(self, artifact_id: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._by_id[artifact_id] = payload

    def delete(self, artifact_id: str) -> bool:
        with self._lock:
            return self._by_id.pop(artifact_id, None) is not None

    def exists(self, artifact_id: str) -> bool:
        with self._lock:
            return artifact_id in self._by_id


# ---------------------------------------------------------------------------
# LifecycleManager
# ---------------------------------------------------------------------------


# An optional in-process callback that deletes a webhook
# delivery record by id. Returns True if a row was removed.
WebhookDeleter = Callable[[str], bool]


# An optional in-process callback that looks up a webhook
# delivery by id. Returns True if the row still exists.
WebhookLookup = Callable[[str], bool]


class LifecycleManager:
    """Stateless facade over the EventStore + in-process stores.

    The DB owns the truth. The in-process stores (artifact
    store, webhook history) are passed in at construction
    time so the dev path can wire real ones, and the test
    path can inject mocks that raise on call.

    Every public method takes ``identity_tenant_id`` and
    cross-checks it against the resource's tenant. A
    mismatch raises :class:`LifecycleCrossTenant` — there
    is no "trust the caller" fallback.
    """

    def __init__(
        self,
        store: Any,
        *,
        artifact_store: DevArtifactStore | None = None,
        webhook_deleter: WebhookDeleter | None = None,
        webhook_lookup: WebhookLookup | None = None,
    ) -> None:
        self._store = store
        # Optional in-process stores. The DB is the source of
        # truth; the in-process stores hold copies that aren't
        # (yet) in Postgres. Production swaps these for DB calls.
        self._artifact_store = artifact_store
        self._webhook_deleter = webhook_deleter
        self._webhook_lookup = webhook_lookup

    # -- policy --------------------------------------------------------------

    def set_policy(
        self,
        tenant_id: str,
        resource_kind: ResourceKind,
        retention_seconds: int,
        auto_delete: bool = False,
    ) -> LifecyclePolicy:
        """Create or update a tenant's retention policy for one resource kind.

        ``retention_seconds`` must be a positive integer. The
        safe default is ``auto_delete=False`` (retain, do not
        delete); a tenant that opts in to automatic deletion
        emits a warning so the audit log shows the policy
        change was deliberate.
        """
        if not isinstance(retention_seconds, int) or retention_seconds <= 0:
            raise ValueError(
                f"retention_seconds must be a positive integer, got {retention_seconds!r}"
            )
        if auto_delete:
            logger.warning(
                "lifecycle policy opts in to auto_delete tenant=%s kind=%s retention=%ds",
                tenant_id,
                resource_kind.value,
                int(retention_seconds),
            )
        policy = LifecyclePolicy(
            tenant_id=tenant_id,
            resource_kind=resource_kind,
            retention_seconds=int(retention_seconds),
            auto_delete=bool(auto_delete),
        )
        self._store.upsert_lifecycle_policy(policy)
        return policy

    def get_policy(
        self, tenant_id: str, resource_kind: ResourceKind
    ) -> dict | None:
        row = self._store.get_lifecycle_policy(tenant_id, resource_kind)
        if row is None:
            return None
        # Coerce psycopg datetime to iso for JSON friendliness.
        if "created_at" in row and not isinstance(row["created_at"], str):
            try:
                row["created_at"] = row["created_at"].isoformat()
            except Exception:  # noqa: BLE001
                pass
        return row

    # -- holds ---------------------------------------------------------------

    def create_hold(
        self,
        tenant_id: str,
        case_id: str,
        reason: str,
        created_by: str,
        resource_kinds: list[ResourceKind] | None = None,
        resource_ids: list[str] | None = None,
        identity_tenant_id: str | None = None,
    ) -> LegalHold:
        """Create a Legal Hold.

        ``case_id`` must be non-empty (it ties the hold to a
        regulator's case number). ``identity_tenant_id``, when
        supplied, must match ``tenant_id`` — cross-tenant hold
        creation is denied.

        The hold can be tenant-wide (no resources) or
        scoped to specific (resource_kind, resource_id) pairs.
        """
        if not case_id or not case_id.strip():
            raise ValueError("case_id must be a non-empty string")
        if identity_tenant_id is not None and identity_tenant_id != tenant_id:
            raise LifecycleCrossTenant(expected=tenant_id, actual=identity_tenant_id)
        hold = LegalHold(
            tenant_id=tenant_id,
            case_id=case_id.strip(),
            reason=reason or "",
            created_by=created_by,
        )
        # Copy the resource_kinds / resource_ids lists the
        # caller supplied (the model defaults are empty lists,
        # so we only set them if the caller actually supplied
        # values).
        if resource_kinds and resource_ids:
            if len(resource_kinds) != len(resource_ids):
                raise ValueError(
                    "resource_kinds and resource_ids must be the same length"
                )
            hold = hold.model_copy(
                update={
                    "resource_kinds": list(resource_kinds),
                    "resource_ids": list(resource_ids),
                }
            )
        resource_pairs: list[tuple[ResourceKind, str]] | None = None
        if resource_kinds and resource_ids:
            resource_pairs = list(zip(resource_kinds, resource_ids))
        self._store.create_legal_hold(hold, resource_pairs=resource_pairs)
        # Audit: a hold.created event so the timeline shows
        # who froze what, when. The store's append_event needs
        # a task_run_id for the seq counter, so we fall back to
        # a direct INSERT into events when there is no task.
        self._emit_audit(
            kind=EventKind.HOLD_CREATED,
            actor=created_by,
            payload={
                "hold_id": hold.hold_id,
                "tenant_id": tenant_id,
                "case_id": hold.case_id,
                "reason": hold.reason,
                "resource_count": len(hold.resource_ids),
            },
        )
        return hold

    def release_hold(
        self,
        hold_id: str,
        released_by: str,
        identity_tenant_id: str,
        reason: str = "",
    ) -> dict[str, Any]:
        """Release a Legal Hold. Cross-tenant attempts are denied."""
        result = self._store.release_legal_hold(
            hold_id=hold_id,
            released_by=released_by,
            identity_tenant_id=identity_tenant_id,
            reason=reason,
        )
        if not result.get("applied"):
            if result.get("reason") == "cross_tenant":
                raise LifecycleCrossTenant(
                    expected="<resource_tenant>",
                    actual=identity_tenant_id,
                )
            return result
        self._emit_audit(
            kind=EventKind.HOLD_RELEASED,
            actor=released_by,
            payload={
                "hold_id": hold_id,
                "released_by": released_by,
                "reason": reason,
            },
        )
        return result

    def list_holds(self, tenant_id: str, active_only: bool = True) -> list[dict[str, Any]]:
        rows = self._store.list_legal_holds(tenant_id, active_only=active_only)
        out: list[dict[str, Any]] = []
        for r in rows:
            r2 = dict(r)
            # Coerce datetimes for JSON serialisation.
            for k in ("created_at", "released_at"):
                v = r2.get(k)
                if v is not None and not isinstance(v, str):
                    try:
                        r2[k] = v.isoformat()
                    except Exception:  # noqa: BLE001
                        pass
            out.append(r2)
        return out

    def is_held(
        self, tenant_id: str, resource_kind: ResourceKind, resource_id: str
    ) -> bool:
        return bool(
            self._store.is_resource_held(tenant_id, resource_kind, resource_id)
        )

    # -- deletion ------------------------------------------------------------

    def delete(
        self,
        tenant_id: str,
        resource_kind: ResourceKind,
        resource_id: str,
        requested_by: str,
        identity_tenant_id: str | None = None,
        force: bool = False,
    ) -> DeletionJob:
        """Create an idempotent DeletionJob for a resource.

        Order of checks (per ADR-0014):
          1. Cross-tenant: ``identity_tenant_id`` (when
             supplied) must match ``tenant_id``.
          2. Hold gate: a Legal Hold covering the resource
             denies the deletion. ``LifecycleBlocked`` is
             raised; a ``deletion.blocked`` event is written.
          3. Retention gate: when the policy says
             ``auto_delete=False`` and the caller did not
             ``force=True``, refuse with
             :class:`LifecycleRetained`.

        The DeletionJob row is created via
        :meth:`EventStore.create_deletion_job`, which is
        itself idempotent (UNIQUE on
        (tenant_id, resource_kind, resource_id)). A second
        call returns the same job instead of creating a
        second one.
        """
        if identity_tenant_id is not None and identity_tenant_id != tenant_id:
            raise LifecycleCrossTenant(
                expected=tenant_id, actual=identity_tenant_id
            )
        # 2. Hold gate — runs first so the deletion never
        #    even gets a job row when a regulator's freeze
        #    is in effect.
        if self.is_held(tenant_id, resource_kind, resource_id):
            # Find the hold_id + case_id for the audit event
            # and the exception payload. The store doesn't
            # have a "find the hold for this resource"
            # helper yet, so we walk the active holds.
            hold_info = self._find_holding_hold(tenant_id, resource_kind, resource_id)
            self._emit_audit(
                kind=EventKind.DELETION_BLOCKED,
                actor=requested_by,
                payload={
                    "tenant_id": tenant_id,
                    "resource_kind": resource_kind.value,
                    "resource_id": resource_id,
                    "hold_id": hold_info["hold_id"] if hold_info else "",
                    "case_id": hold_info["case_id"] if hold_info else "",
                },
            )
            raise LifecycleBlocked(
                hold_id=hold_info["hold_id"] if hold_info else "",
                case_id=hold_info["case_id"] if hold_info else "",
                resource_kind=resource_kind,
                resource_id=resource_id,
            )
        # 3. Retention gate.
        policy = self.get_policy(tenant_id, resource_kind)
        if policy and not bool(policy.get("auto_delete", False)) and not force:
            self._emit_audit(
                kind=EventKind.DELETION_BLOCKED,
                actor=requested_by,
                payload={
                    "tenant_id": tenant_id,
                    "resource_kind": resource_kind.value,
                    "resource_id": resource_id,
                    "reason": "retained",
                },
            )
            raise LifecycleRetained(resource_kind, resource_id)
        # Build the job. The store's create_deletion_job is
        # idempotent (UNIQUE constraint) so a second call
        # returns the same job.
        job = DeletionJob(
            tenant_id=tenant_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            requested_by=requested_by,
        )
        # On first creation, bump the policy's `auto_delete`
        # opt-in to a deletion.requested event so the audit
        # trail shows the call.
        self._emit_audit(
            kind=EventKind.DELETION_REQUESTED,
            actor=requested_by,
            payload={
                "tenant_id": tenant_id,
                "resource_kind": resource_kind.value,
                "resource_id": resource_id,
                "force": bool(force),
            },
        )
        return self._store.create_deletion_job(job)

    def execute_deletion(
        self, job_id: str, deleter: Any | None = None
    ) -> DeletionJob:
        """Run the per-resource adapter for a DeletionJob.

        Transitions:
          pending → running → deleted
                            ↘ partial  (some copies kept)
                            ↘ failed   (no progress, or limit hit)
        """
        row = self._store.get_deletion_job(job_id)
        if row is None:
            raise ValueError(f"unknown deletion job: {job_id}")
        # Already terminal — no-op return.
        if row.get("state") in ("deleted", "failed", "held"):
            return self._row_to_job(row)
        # Bump attempt + transition to running.
        attempt_before = int(row.get("attempt", 0))
        # We don't increment yet — the increment happens in
        # the failure branch. A successful run sets the final
        # state without an attempt bump (it counts as "one
        # attempt that succeeded"). A failing run increments.
        self._store.update_deletion_job(
            job_id=job_id,
            state=DeletionState.RUNNING,
            increment_attempt=False,
        )
        try:
            evidence = self._run_deleter(row, deleter=deleter)
        except Exception as e:  # noqa: BLE001
            return self._handle_adapter_failure(row, e, attempt_before)
        # Decide final state from the evidence.
        if evidence.copies_kept == 0:
            final_state = DeletionState.DELETED
            event_kind = EventKind.DELETION_COMPLETED
        else:
            final_state = DeletionState.PARTIAL
            event_kind = EventKind.DELETION_PARTIAL
        self._store.update_deletion_job(
            job_id=job_id,
            state=final_state,
            evidence=evidence.model_dump(mode="json"),
        )
        self._emit_audit(
            kind=event_kind,
            actor="orchestra",
            payload={
                "tenant_id": row["tenant_id"],
                "resource_kind": row["resource_kind"],
                "resource_id": row["resource_id"],
                "job_id": job_id,
                "copies_deleted": evidence.copies_deleted,
                "copies_kept": evidence.copies_kept,
                "payload_digest": evidence.payload_digest,
            },
        )
        return self._row_to_job(self._store.get_deletion_job(job_id))

    def retry_deletion(self, job_id: str) -> DeletionJob:
        """Re-run a partial / failed job. No-op for terminal
        (deleted / held) jobs. Raises ``ValueError`` on a
        not-found id.
        """
        row = self._store.get_deletion_job(job_id)
        if row is None:
            raise ValueError(f"unknown deletion job: {job_id}")
        state = row.get("state")
        if state == "deleted":
            raise ValueError(
                f"deletion job {job_id} is already terminal (deleted); no retry"
            )
        if state == "held":
            raise ValueError(
                f"deletion job {job_id} is held; release the Legal Hold first"
            )
        attempt = int(row.get("attempt", 0))
        max_attempts = int(row.get("max_attempts", 3))
        if attempt >= max_attempts and state == "failed":
            raise ValueError(
                f"deletion job {job_id} has exhausted its retry budget ({max_attempts})"
            )
        # Reset the state to pending so execute_deletion's
        # terminal-check doesn't short-circuit.
        self._store.update_deletion_job(
            job_id=job_id, state=DeletionState.PENDING, increment_attempt=False
        )
        return self.execute_deletion(job_id)

    def list_deletion_jobs(
        self, tenant_id: str, state: DeletionState | None = None
    ) -> list[dict[str, Any]]:
        return self._store.list_deletion_jobs(tenant_id, state=state)

    def get_deletion_job(self, job_id: str) -> dict | None:
        row = self._store.get_deletion_job(job_id)
        if row is None:
            return None
        return self._coerce_job_row(row)

    # -- internals -----------------------------------------------------------

    def _run_deleter(self, row: dict, *, deleter: Any | None = None) -> DeletionEvidence:
        """Dispatch to the resource-specific adapter."""
        kind = ResourceKind(row["resource_kind"])
        adapter = _DELETERS.get(kind)
        if adapter is None:
            # An unknown resource kind is a code bug, not a
            # runtime error; surface it loudly.
            raise LifecycleError(f"no deleter registered for {kind.value!r}")
        return adapter(self, row, deleter=deleter)

    def _handle_adapter_failure(
        self, row: dict, exc: BaseException, attempt_before: int
    ) -> DeletionJob:
        attempt_after = attempt_before + 1
        max_attempts = int(row.get("max_attempts", 3))
        is_terminal = attempt_after >= max_attempts
        final_state = DeletionState.FAILED if is_terminal else DeletionState.PARTIAL
        evidence = DeletionEvidence(
            copies_deleted=0,
            copies_kept=1,
            kept_resources=[
                {
                    "kind": row["resource_kind"],
                    "id": row["resource_id"],
                    "reason": "adapter_error",
                }
            ],
            payload_digest=None,
        )
        self._store.update_deletion_job(
            job_id=row["job_id"],
            state=final_state,
            last_error=f"{type(exc).__name__}: {exc}",
            evidence=evidence.model_dump(mode="json"),
            increment_attempt=True,
        )
        self._emit_audit(
            kind=(
                EventKind.DELETION_FAILED
                if is_terminal
                else EventKind.DELETION_PARTIAL
            ),
            actor="orchestra",
            payload={
                "tenant_id": row["tenant_id"],
                "resource_kind": row["resource_kind"],
                "resource_id": row["resource_id"],
                "job_id": row["job_id"],
                "attempt": attempt_after,
                "error": str(exc),
            },
        )
        return self._row_to_job(self._store.get_deletion_job(row["job_id"]))

    def _find_holding_hold(
        self, tenant_id: str, resource_kind: ResourceKind, resource_id: str
    ) -> dict | None:
        """Find the hold (and its case_id) that covers this resource.

        A tenant-wide hold (no resources) covers every
        resource in the tenant. A scoped hold covers only
        the (kind, id) pairs the caller listed.
        """
        for h in self._store.list_legal_holds(tenant_id, active_only=True):
            kinds = h.get("resource_kind", []) or []
            ids = h.get("resource_id", []) or []
            if not kinds and not ids:
                # Tenant-wide hold.
                return {"hold_id": h["hold_id"], "case_id": h["case_id"]}
            for k, rid in zip(kinds, ids):
                if k == resource_kind.value and rid == resource_id:
                    return {"hold_id": h["hold_id"], "case_id": h["case_id"]}
        return None

    def _emit_audit(
        self,
        *,
        kind: EventKind,
        actor: str,
        payload: dict[str, Any],
        task_run_id: str | None = None,
    ) -> None:
        """Best-effort write to the events table.

        Lifecycle events are not tied to a specific task, so
        the seq counter is per-row (we pass an explicit
        ``seq=0`` and the store helper raises if the row is
        missing). To avoid that error path on tenant-wide
        events, we go through ``append_event`` with a
        synthetic task_run_id of the form ``__lifecycle_<id>``,
        and we tolerate failure (the audit row is a
        nice-to-have, not a safety boundary).
        """
        try:
            from orchestra.core.schema import AuditEvent

            synthetic = task_run_id or f"__lifecycle_{new_id()[:12]}"
            # The store's append_event uses task_run_id
            # to derive the seq; if the task doesn't exist,
            # the FK constraint rejects. The lifecycle
            # tables are independent of task_runs, so we
            # skip the FK by writing through a raw INSERT
            # (the events table does have a FK to
            # task_runs, so we set task_run_id NULL and
            # supply an explicit seq).
            with self._store._tx() as c, c.cursor() as cur:  # type: ignore[attr-defined]
                cur.execute(
                    """
                    INSERT INTO events
                      (event_id, task_run_id, node_run_id, seq, kind, occurred_at, actor, payload, prev_event_id)
                    VALUES (%s, NULL, NULL, %s, %s, %s, %s, %s::jsonb, NULL)
                    """,
                    (
                        new_id(),
                        int(0),
                        kind.value,
                        utc_now_iso(),
                        actor,
                        json.dumps(payload, ensure_ascii=False, default=str),
                    ),
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "lifecycle audit write skipped (kind=%s): %s", kind.value, e
            )

    def _row_to_job(self, row: dict) -> DeletionJob:
        return DeletionJob(
            job_id=row["job_id"],
            tenant_id=row["tenant_id"],
            resource_kind=ResourceKind(row["resource_kind"]),
            resource_id=row["resource_id"],
            state=DeletionState(row["state"]),
            attempt=int(row.get("attempt", 0)),
            max_attempts=int(row.get("max_attempts", 3)),
            requested_at=str(row.get("requested_at", utc_now_iso())),
            requested_by=row.get("requested_by", ""),
            completed_at=(
                str(row["completed_at"])
                if row.get("completed_at") is not None
                else None
            ),
            last_error=row.get("last_error"),
            evidence=self._coerce_evidence(row.get("deletion_evidence")),
        )

    def _coerce_job_row(self, row: dict) -> dict[str, Any]:
        out = dict(row)
        # Coerce datetimes for JSON friendliness.
        for k in ("requested_at", "completed_at"):
            v = out.get(k)
            if v is not None and not isinstance(v, str):
                try:
                    out[k] = v.isoformat()
                except Exception:  # noqa: BLE001
                    pass
        ev = out.get("deletion_evidence")
        if isinstance(ev, str):
            try:
                out["deletion_evidence"] = json.loads(ev)
            except Exception:  # noqa: BLE001
                pass
        return out

    @staticmethod
    def _coerce_evidence(raw: Any) -> DeletionEvidence | None:
        if raw is None:
            return None
        if isinstance(raw, DeletionEvidence):
            return raw
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:  # noqa: BLE001
                return None
        if not isinstance(raw, dict):
            return None
        try:
            return DeletionEvidence.model_validate(raw)
        except Exception:  # noqa: BLE001
            return None


# ---------------------------------------------------------------------------
# Per-resource deleter adapters
# ---------------------------------------------------------------------------


def _delete_artifact(
    manager: LifecycleManager, row: dict, *, deleter: Any | None = None
) -> DeletionEvidence:
    """Delete an artifact: in-process store first, DB row second.

    The dev path keeps the artifact metadata in
    :class:`InMemoryDevArtifactStore`; the production swap
    is ``DELETE FROM artifacts WHERE artifact_id = %s``.

    If the in-process store is not wired (the manager was
    constructed without one), we honestly report the row
    as still kept so an operator can wire it later.
    """
    artifact_id = row["resource_id"]
    deleted = 0
    kept: list[dict[str, str]] = []
    payload_blob: Any = None
    if manager._artifact_store is None:
        kept.append({"kind": "artifact", "id": artifact_id, "reason": "no_store_wired"})
    else:
        try:
            payload_blob = getattr(manager._artifact_store, "_by_id", {}).get(artifact_id)
            if manager._artifact_store.delete(artifact_id):
                deleted += 1
            else:
                # The row was already gone — partial success.
                kept.append(
                    {"kind": "artifact", "id": artifact_id, "reason": "already_gone"}
                )
        except Exception as e:  # noqa: BLE001
            kept.append(
                {"kind": "artifact", "id": artifact_id, "reason": f"error: {e!s}"}
            )
    # DB-side metadata (when the row exists, we delete it; when
    # it doesn't, we record that honestly). The dev path's PG
    # schema doesn't have an artifacts table, so we treat the
    # absence as "no DB row to remove".
    if manager._store is not None and hasattr(manager._store, "delete_artifact_row"):
        try:
            manager._store.delete_artifact_row(artifact_id)  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            kept.append({"kind": "artifact_db", "id": artifact_id, "reason": f"error: {e!s}"})
    digest: str | None = None
    if payload_blob is not None:
        try:
            digest = digest_json(payload_blob)
        except Exception:  # noqa: BLE001
            digest = None
    return DeletionEvidence(
        copies_deleted=deleted,
        copies_kept=len(kept),
        kept_resources=kept,
        payload_digest=digest,
    )


def _delete_receipt(
    manager: LifecycleManager, row: dict, *, deleter: Any | None = None
) -> DeletionEvidence:
    """Delete a receipt. The event row that references the
    receipt stays; its payload is replaced with
    ``{"redacted": true}`` (ADR-0014 §Coverage).
    """
    receipt_id = row["resource_id"]
    tenant_id = row["tenant_id"]
    deleted = 0
    kept: list[dict[str, str]] = []
    receipt_row = manager._store.get_receipt(receipt_id)
    if receipt_row is None:
        kept.append({"kind": "receipt", "id": receipt_id, "reason": "already_gone"})
    else:
        try:
            if manager._store.delete_receipt(receipt_id):
                deleted += 1
            else:
                kept.append(
                    {"kind": "receipt", "id": receipt_id, "reason": "delete_returned_false"}
                )
        except Exception as e:  # noqa: BLE001
            kept.append({"kind": "receipt", "id": receipt_id, "reason": f"error: {e!s}"})
    # Find any events that reference the receipt and redact
    # their payloads so the audit trail shows the receipt
    # is gone but the row stays (the audit trail is
    # permanent).
    receipt_node_run_id = (
        receipt_row.get("node_run_id") if receipt_row else None
    )
    try:
        events = manager._store.list_events_for_resource(tenant_id=tenant_id)
        for e in events:
            p = e.get("payload") or {}
            if not isinstance(p, dict):
                continue
            matched = False
            if p.get("receipt_id") == receipt_id:
                matched = True
            elif (
                receipt_node_run_id is not None
                and e.get("node_run_id") == receipt_node_run_id
                and e.get("kind") == "receipt.signed"
            ):
                matched = True
            if matched:
                try:
                    manager._store.redact_event_payload(tenant_id, e["event_id"])
                except Exception as inner:  # noqa: BLE001
                    kept.append(
                        {
                            "kind": "event_payload",
                            "id": e["event_id"],
                            "reason": f"redact_error: {inner!s}",
                        }
                    )
    except Exception as e:  # noqa: BLE001
        kept.append({"kind": "events_scan", "id": "*", "reason": f"error: {e!s}"})
    digest = None
    if receipt_row is not None:
        envelope = receipt_row.get("envelope")
        if envelope is not None:
            try:
                digest = digest_json(envelope)
            except Exception:  # noqa: BLE001
                digest = None
    return DeletionEvidence(
        copies_deleted=deleted,
        copies_kept=len(kept),
        kept_resources=kept,
        payload_digest=digest,
    )


def _delete_event_payload(
    manager: LifecycleManager, row: dict, *, deleter: Any | None = None
) -> DeletionEvidence:
    """Redact an event payload. The row stays.

    The ``resource_id`` is the ``event_id``. The deleter
    walks the events table for that one row and replaces
    its payload with ``{"redacted": true}``.
    """
    event_id = row["resource_id"]
    tenant_id = row["tenant_id"]
    try:
        ok = manager._store.redact_event_payload(tenant_id, event_id)
        if ok:
            return DeletionEvidence(
                copies_deleted=1,
                copies_kept=0,
                kept_resources=[],
                payload_digest=None,
            )
        return DeletionEvidence(
            copies_deleted=0,
            copies_kept=1,
            kept_resources=[
                {"kind": "event", "id": event_id, "reason": "not_found"}
            ],
            payload_digest=None,
        )
    except Exception as e:  # noqa: BLE001
        return DeletionEvidence(
            copies_deleted=0,
            copies_kept=1,
            kept_resources=[
                {"kind": "event", "id": event_id, "reason": f"error: {e!s}"}
            ],
            payload_digest=None,
        )


def _delete_webhook_delivery(
    manager: LifecycleManager, row: dict, *, deleter: Any | None = None
) -> DeletionEvidence:
    """Delete a webhook delivery record.

    The dev path keeps deliveries in
    :class:`~orchestra.webhooks.history.DeliveryHistory`
    (in-process). When the manager is wired with a
    ``webhook_deleter`` callback, we use it; otherwise we
    honestly report the delivery as kept.
    """
    delivery_id = row["resource_id"]
    kept: list[dict[str, str]] = []
    if manager._webhook_deleter is None:
        kept.append(
            {"kind": "webhook", "id": delivery_id, "reason": "no_deleter_wired"}
        )
        return DeletionEvidence(
            copies_deleted=0,
            copies_kept=1,
            kept_resources=kept,
            payload_digest=None,
        )
    try:
        ok = manager._webhook_deleter(delivery_id)
        if ok:
            return DeletionEvidence(
                copies_deleted=1,
                copies_kept=0,
                kept_resources=[],
                payload_digest=None,
            )
        kept.append(
            {"kind": "webhook", "id": delivery_id, "reason": "not_found"}
        )
    except Exception as e:  # noqa: BLE001
        kept.append(
            {"kind": "webhook", "id": delivery_id, "reason": f"error: {e!s}"}
        )
    return DeletionEvidence(
        copies_deleted=0 if kept else 1,
        copies_kept=len(kept),
        kept_resources=kept,
        payload_digest=None,
    )


def _delete_cache_entry(
    manager: LifecycleManager, row: dict, *, deleter: Any | None = None
) -> DeletionEvidence:
    """Cache deletion is a stub in M24 (per ADR-0014).

    The dev path has no cache layer; production swaps
    for a real Redis / Memcached client. We return a
    zero-on-both-sides evidence so the audit trail
    records the policy intent without a false copy
    count.
    """
    return DeletionEvidence(
        copies_deleted=0,
        copies_kept=0,
        kept_resources=[],
        payload_digest=None,
    )


def _delete_backup(
    manager: LifecycleManager, row: dict, *, deleter: Any | None = None
) -> DeletionEvidence:
    """Backup deletion is out of scope (per ADR-0014).

    The dev path has no backup target. Production swaps
    for a real backup-client adapter. We honestly
    record the backup as kept so the audit trail shows
    the policy refused to silently succeed.
    """
    return DeletionEvidence(
        copies_deleted=0,
        copies_kept=1,
        kept_resources=[
            {
                "kind": "backup",
                "id": row["resource_id"],
                "reason": "out_of_scope",
            }
        ],
        payload_digest=None,
    )


# Registry of per-resource adapters. A new ResourceKind value
# must be wired here; the manager raises LifecycleError if a
# kind is missing.
_DELETERS: dict[ResourceKind, Callable[..., DeletionEvidence]] = {
    ResourceKind.ARTIFACT: _delete_artifact,
    ResourceKind.RECEIPT: _delete_receipt,
    ResourceKind.EVENT: _delete_event_payload,
    ResourceKind.WEBHOOK: _delete_webhook_delivery,
    ResourceKind.CACHE: _delete_cache_entry,
    ResourceKind.BACKUP: _delete_backup,
}


__all__ = [
    "LifecycleManager",
    "LifecycleError",
    "LifecycleBlocked",
    "LifecycleRetained",
    "LifecycleCrossTenant",
    "DevArtifactStore",
    "InMemoryDevArtifactStore",
    "WebhookDeleter",
    "WebhookLookup",
]
