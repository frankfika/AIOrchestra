"""M24 DLM-001 — Admin API for Retention + Legal Hold lifecycle (ADR-0014).

Mounted by :func:`orchestra.api.app.create_app` via
:func:`build_admin_lifecycle_router`. The router is wired into
the main app behind a thin wrapper that resolves the
LifecycleManager from the app state. The wire shape is:

  * ``POST   /admin/retention/policy``        — set / upsert a LifecyclePolicy
  * ``GET    /admin/retention/policy/{t}/{k}`` — read a policy
  * ``POST   /admin/holds``                    — create a Legal Hold
  * ``GET    /admin/holds?tenant_id=...``      — list holds
  * ``DELETE /admin/holds/{hold_id}``          — release a hold
  * ``POST   /admin/deletion-jobs``            — request a deletion
  * ``GET    /admin/deletion-jobs?tenant_id=...`` — list jobs
  * ``GET    /admin/deletion-jobs/{job_id}``   — show one job
  * ``POST   /admin/deletion-jobs/{job_id}/execute``
  * ``POST   /admin/deletion-jobs/{job_id}/retry``

Identity is taken from the ``X-Orchestra-Actor`` header (the
dev path convention). Tenant scoping is enforced at the
manager level: the ``tenant_id`` in the body must match the
identity's tenant when the identity is provided, otherwise
the call is denied with ``LifecycleCrossTenant``.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from orchestra.core.schema import DeletionState, ResourceKind
from orchestra.enterprise.lifecycle import (
    LifecycleBlocked,
    LifecycleCrossTenant,
    LifecycleError,
    LifecycleManager,
    LifecycleRetained,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


_VALID_KINDS = {k.value for k in ResourceKind}


class PolicySetRequest(BaseModel):
    tenant_id: str
    resource_kind: str
    retention_seconds: int = Field(ge=1)
    auto_delete: bool = False


class HoldCreateRequest(BaseModel):
    tenant_id: str
    case_id: str
    reason: str
    created_by: str = "api"
    resource_kinds: list[str] | None = None
    resource_ids: list[str] | None = None


class HoldReleaseRequest(BaseModel):
    released_by: str = "api"
    reason: str = ""


class DeletionCreateRequest(BaseModel):
    tenant_id: str
    resource_kind: str
    resource_id: str
    requested_by: str = "api"
    force: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_actor(actor: str | None, tenant_id: str) -> str | None:
    """The dev path takes the actor's tenant from the request
    body (it's an admin operation; the actor is the partner's
    service account). When ``X-Orchestra-Actor`` carries an
    explicit ``tenant:xxx`` claim, the manager cross-checks.
    """
    if not actor:
        return None
    # Convention: an actor string of the form ``tenant:foo``
    # encodes the tenant. Anything else (e.g. ``alice@acme``)
    # is treated as "no tenant" and is checked against the
    # body's tenant_id by the manager.
    if actor.startswith("tenant:") and actor != tenant_id:
        return actor
    return None


def _kind_or_400(s: str) -> ResourceKind:
    if s not in _VALID_KINDS:
        raise HTTPException(422, f"unknown resource_kind {s!r}; valid: {sorted(_VALID_KINDS)}")
    return ResourceKind(s)


def _lifecycle_error_to_http(exc: LifecycleError) -> HTTPException:
    if isinstance(exc, LifecycleBlocked):
        return HTTPException(
            status_code=409,
            detail={
                "type": "urn:orchestra:problem:lifecycle_blocked",
                "title": "Deletion blocked by a Legal Hold.",
                "status": 409,
                "hold_id": exc.hold_id,
                "case_id": exc.case_id,
                "resource_kind": exc.resource_kind.value,
                "resource_id": exc.resource_id,
            },
        )
    if isinstance(exc, LifecycleRetained):
        return HTTPException(
            status_code=409,
            detail={
                "type": "urn:orchestra:problem:lifecycle_retained",
                "title": "Deletion refused: the lifecycle policy retains this resource.",
                "status": 409,
                "resource_kind": exc.resource_kind.value,
                "resource_id": exc.resource_id,
            },
        )
    if isinstance(exc, LifecycleCrossTenant):
        return HTTPException(
            status_code=403,
            detail={
                "type": "urn:orchestra:problem:cross_tenant",
                "title": "Cross-tenant operation denied.",
                "status": 403,
                "expected_tenant": exc.expected_tenant,
                "actual_tenant": exc.actual_tenant,
            },
        )
    return HTTPException(500, str(exc))


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_admin_lifecycle_router(
    *, manager_provider: Callable[[], LifecycleManager]
) -> APIRouter:
    router = APIRouter()

    def _mgr() -> LifecycleManager:
        return manager_provider()

    # ----- policy ----------------------------------------------------------

    @router.post(
        "/admin/retention/policy",
        summary="Set / upsert a LifecyclePolicy",
        tags=["Admin"],
    )
    def policy_set(
        body: PolicySetRequest,
        x_orchestra_actor: str | None = Header(default=None, alias="X-Orchestra-Actor"),
    ) -> dict[str, Any]:
        kind = _kind_or_400(body.resource_kind)
        try:
            pol = _mgr().set_policy(
                tenant_id=body.tenant_id,
                resource_kind=kind,
                retention_seconds=body.retention_seconds,
                auto_delete=body.auto_delete,
            )
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {
            "policy_id": pol.policy_id,
            "tenant_id": pol.tenant_id,
            "resource_kind": pol.resource_kind.value,
            "retention_seconds": pol.retention_seconds,
            "auto_delete": pol.auto_delete,
            "created_at": pol.created_at,
        }

    @router.get(
        "/admin/retention/policy/{tenant_id}/{resource_kind}",
        summary="Read a LifecyclePolicy",
        tags=["Admin"],
    )
    def policy_get(tenant_id: str, resource_kind: str) -> dict[str, Any]:
        kind = _kind_or_400(resource_kind)
        row = _mgr().get_policy(tenant_id, kind)
        if row is None:
            raise HTTPException(404, f"no policy for {tenant_id}/{resource_kind}")
        return row

    # ----- holds -----------------------------------------------------------

    @router.post(
        "/admin/holds",
        summary="Create a Legal Hold",
        tags=["Admin"],
    )
    def hold_create(
        body: HoldCreateRequest,
        x_orchestra_actor: str | None = Header(default=None, alias="X-Orchestra-Actor"),
    ) -> dict[str, Any]:
        if not body.case_id or not body.case_id.strip():
            raise HTTPException(422, "case_id must be a non-empty string")
        kinds: list[ResourceKind] | None = None
        if body.resource_kinds:
            kinds = [_kind_or_400(k) for k in body.resource_kinds]
        actor_tenant = _resolve_actor(x_orchestra_actor, body.tenant_id)
        try:
            hold = _mgr().create_hold(
                tenant_id=body.tenant_id,
                case_id=body.case_id,
                reason=body.reason,
                created_by=body.created_by,
                resource_kinds=kinds,
                resource_ids=body.resource_ids,
                identity_tenant_id=actor_tenant,
            )
        except LifecycleError as e:
            raise _lifecycle_error_to_http(e)
        return {
            "hold_id": hold.hold_id,
            "tenant_id": hold.tenant_id,
            "case_id": hold.case_id,
            "reason": hold.reason,
            "created_by": hold.created_by,
            "created_at": hold.created_at,
            "resource_kinds": [k.value for k in hold.resource_kinds],
            "resource_ids": hold.resource_ids,
        }

    @router.get(
        "/admin/holds",
        summary="List Legal Holds (active by default)",
        tags=["Admin"],
    )
    def hold_list(
        tenant_id: str = Query(..., description="tenant scope"),
        active_only: bool = Query(default=True),
    ) -> dict[str, Any]:
        rows = _mgr().list_holds(tenant_id, active_only=active_only)
        return {"tenant_id": tenant_id, "count": len(rows), "holds": rows}

    @router.delete(
        "/admin/holds/{hold_id}",
        summary="Release a Legal Hold",
        tags=["Admin"],
    )
    def hold_release(
        hold_id: str,
        body: HoldReleaseRequest,
        x_orchestra_actor: str | None = Header(default=None, alias="X-Orchestra-Actor"),
    ) -> dict[str, Any]:
        # For hold release the actor's tenant must match the
        # hold's tenant. The actor's tenant is supplied via
        # the X-Orchestra-Actor header (or the body, if the
        # caller prefers). When the header carries a
        # ``tenant:foo`` claim, we use it; otherwise the
        # cross-tenant check happens at the store level
        # (we look up the hold's tenant first).
        identity_tenant_id = x_orchestra_actor or body.released_by
        try:
            result = _mgr().release_hold(
                hold_id=hold_id,
                released_by=body.released_by,
                identity_tenant_id=identity_tenant_id,
                reason=body.reason,
            )
        except LifecycleError as e:
            raise _lifecycle_error_to_http(e)
        if not result.get("applied"):
            reason = result.get("reason", "unknown")
            if reason == "cross_tenant":
                raise HTTPException(403, "cross-tenant: hold belongs to a different tenant")
            if reason == "not_found":
                raise HTTPException(404, f"hold {hold_id} not found")
            if reason == "already_released":
                raise HTTPException(409, f"hold {hold_id} is already released")
            raise HTTPException(400, reason)
        return {"hold_id": hold_id, "status": "released"}

    # ----- deletion jobs ---------------------------------------------------

    @router.post(
        "/admin/deletion-jobs",
        summary="Create a DeletionJob (idempotent on (tenant, kind, id))",
        tags=["Admin"],
    )
    def deletion_create(
        body: DeletionCreateRequest,
        x_orchestra_actor: str | None = Header(default=None, alias="X-Orchestra-Actor"),
    ) -> dict[str, Any]:
        kind = _kind_or_400(body.resource_kind)
        actor_tenant = _resolve_actor(x_orchestra_actor, body.tenant_id)
        try:
            job = _mgr().delete(
                tenant_id=body.tenant_id,
                resource_kind=kind,
                resource_id=body.resource_id,
                requested_by=body.requested_by,
                identity_tenant_id=actor_tenant,
                force=body.force,
            )
        except LifecycleError as e:
            raise _lifecycle_error_to_http(e)
        return _job_to_dict(job)

    @router.get(
        "/admin/deletion-jobs",
        summary="List DeletionJobs for a tenant",
        tags=["Admin"],
    )
    def deletion_list(
        tenant_id: str = Query(..., description="tenant scope"),
        state: str | None = Query(default=None),
    ) -> dict[str, Any]:
        st = None
        if state is not None:
            try:
                st = DeletionState(state)
            except ValueError:
                raise HTTPException(422, f"unknown state {state!r}")
        rows = _mgr().list_deletion_jobs(tenant_id, state=st)
        return {"tenant_id": tenant_id, "count": len(rows), "jobs": rows}

    @router.get(
        "/admin/deletion-jobs/{job_id}",
        summary="Show one DeletionJob",
        tags=["Admin"],
    )
    def deletion_get(job_id: str) -> dict[str, Any]:
        row = _mgr().get_deletion_job(job_id)
        if row is None:
            raise HTTPException(404, f"deletion job {job_id} not found")
        return row

    @router.post(
        "/admin/deletion-jobs/{job_id}/execute",
        summary="Execute a DeletionJob (transition pending/running → deleted/partial/failed)",
        tags=["Admin"],
    )
    def deletion_execute(job_id: str) -> dict[str, Any]:
        try:
            job = _mgr().execute_deletion(job_id)
        except LifecycleError as e:
            raise _lifecycle_error_to_http(e)
        return _job_to_dict(job)

    @router.post(
        "/admin/deletion-jobs/{job_id}/retry",
        summary="Retry a partial / failed DeletionJob",
        tags=["Admin"],
    )
    def deletion_retry(job_id: str) -> dict[str, Any]:
        try:
            job = _mgr().retry_deletion(job_id)
        except LifecycleError as e:
            raise _lifecycle_error_to_http(e)
        except ValueError as e:
            raise HTTPException(409, str(e))
        return _job_to_dict(job)

    return router


def _job_to_dict(job) -> dict[str, Any]:
    out: dict[str, Any] = {
        "job_id": job.job_id,
        "tenant_id": job.tenant_id,
        "resource_kind": job.resource_kind.value,
        "resource_id": job.resource_id,
        "state": job.state.value,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "requested_at": job.requested_at,
        "requested_by": job.requested_by,
        "completed_at": job.completed_at,
        "last_error": job.last_error,
    }
    if job.evidence is not None:
        out["evidence"] = job.evidence.model_dump(mode="json")
    return out
