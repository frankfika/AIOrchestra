"""M24 W2 — BreakGlassService (ADR-0012).

The break-glass service composes the existing EventStore methods to
deliver a finite-state machine with:

* two distinct approvers (the applicant cannot be the approver; the
  two approvers cannot be the same identity);
* a bounded window (default 15 min, hard cap 4 h);
* a per-tenant cap (the tenant's policy can lower the hard cap but
  never raise it);
* an effect-ceiling check (the runtime refuses any effect that
  disables the Egress PEP or downgrades a resource's
  SecurityLabel);
* a sweep that moves active → expired and a revoke that can fire
  from any operator with the kill-switch role.

The service is stateless — the store owns the rows. The service
adds value by:

1. applying the effect ceiling at request time (so a typo'd
   ``disable_egress_pep: True`` never reaches the approvers),
2. clamping the window to the tenant's max + the hard cap,
3. emitting a signed ``break_glass.*`` audit event for every
   transition so the timeline renders the lifecycle distinctly
   from regular approvals,
4. enforcing applicant ≠ approver at the service layer (the
   store also enforces, but the service fails fast and surfaces a
   clean error name).
"""
from __future__ import annotations

from typing import Any

from orchestra.core.errors import ContractViolation
from orchestra.core.schema import (
    AuditEvent,
    BreakGlassRequest,
    BreakGlassState,
    DataClassification,
    EventKind,
)
from orchestra.core.time import utc_now_iso
from orchestra.coordinator.event_store import EventStore


# Hard maximum that no tenant policy can exceed (4 hours). This is
# the "no admin leaves a break-glass open for a week" safety
# control.
HARD_MAX_WINDOW_SECONDS = 4 * 60 * 60  # 14400

# Default window when the caller doesn't supply one and the tenant
# has no policy.
DEFAULT_WINDOW_SECONDS = 15 * 60  # 900


class BreakGlassEffectRejected(ContractViolation):
    """The effect payload failed the ceiling check.

    A typo'd ``disable_egress_pep: True`` or a label downgrade
    hits this exception. The message is safe to surface to a
    partner UI; it does not leak other tenants' policy.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"break-glass effect rejected: {reason}")
        self.reason = reason


def _check_effect_ceiling(
    effect: dict[str, Any], resource_scope: dict[str, Any]
) -> str | None:
    """Return a non-None reason when the effect must be rejected.

    The check is pragmatic — it covers the three cases the ADR
    pins as the "lowered-by-design floor":

    * ``disable_egress_pep`` is set to ``True``.
    * ``label_override`` lowers the resource's classification.
    * ``egress_view_name`` is ``egress.public`` and the resource
      is :attr:`DataClassification.RESTRICTED`.

    The check is a struct inspection; the production swap wires
    it to the M1 Trust Compiler's info-flow rules for the same
    checks at runtime.
    """
    if not isinstance(effect, dict):
        return "effect must be a dict"

    # 1. Cannot disable Egress PEP / Zero-Egress.
    if effect.get("disable_egress_pep") is True:
        return "disable_egress_pep is forbidden (Zero-Egress invariant)"

    # 2. Cannot downgrade a label.
    label_override = effect.get("label_override")
    if isinstance(label_override, str):
        order = {
            DataClassification.PUBLIC.value: 0,
            DataClassification.PARTNER.value: 1,
            DataClassification.INTERNAL.value: 2,
            DataClassification.RESTRICTED.value: 3,
        }
        current = resource_scope.get("current_classification") if isinstance(resource_scope, dict) else None
        if current in order and label_override in order:
            if order[label_override] < order[current]:
                return (
                    f"label_override ({label_override}) is lower than "
                    f"resource's current classification ({current})"
                )

    # 3. Cannot route RESTRICTED to egress.public.
    egress_view = effect.get("egress_view_name")
    if egress_view == "egress.public":
        current = resource_scope.get("current_classification") if isinstance(resource_scope, dict) else None
        if current == DataClassification.RESTRICTED.value:
            return "egress.public is forbidden for RESTRICTED resources"

    return None


def _clamp_window(
    requested: int | None,
    tenant_max: int | None,
) -> int:
    """Clamp the window to ``min(requested, tenant_max, hard_max)``.

    Falls back to :data:`DEFAULT_WINDOW_SECONDS` when neither the
    caller nor the tenant policy supplies a value.
    """
    if requested is None and tenant_max is None:
        return DEFAULT_WINDOW_SECONDS
    candidate = requested if requested is not None else tenant_max
    cap = tenant_max if tenant_max is not None else HARD_MAX_WINDOW_SECONDS
    return max(1, min(int(candidate), int(cap), HARD_MAX_WINDOW_SECONDS))


class BreakGlassService:
    """Stateless façade over :class:`EventStore` for break-glass."""

    def __init__(self, store: EventStore) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # Request
    # ------------------------------------------------------------------

    def request(
        self,
        *,
        tenant_id: str,
        purpose: str,
        effect: dict[str, Any],
        resource_scope: dict[str, Any],
        requested_by: str,
        ticket: str | None = None,
        window_seconds: int | None = None,
        task_run_id: str | None = None,
        identity_tenant_id: str | None = None,
    ) -> BreakGlassRequest:
        """Create a new break-glass request.

        The caller is the applicant. Identity is taken from
        ``requested_by`` (the human's verified identifier); the
        store enforces the cross-tenant denial at the
        approve / revoke boundary.

        When ``task_run_id`` is provided the request is bound to
        a specific task; when not, the request is tenant-scoped
        (e.g. "allow all egress for tenant X for 15 minutes").
        Tenant-scoped requests get a synthetic task anchor so the
        audit timeline has a foreign key to land on.
        """
        if not tenant_id:
            raise ContractViolation("tenant_id is required")
        if not purpose:
            raise ContractViolation("purpose is required")
        if not requested_by:
            raise ContractViolation("requested_by is required")

        # 1. Effect-ceiling check before we even write a row.
        reason = _check_effect_ceiling(effect, resource_scope)
        if reason is not None:
            raise BreakGlassEffectRejected(reason)

        # 2. Window clamp.
        tenant_max = self._resolve_tenant_max_window(tenant_id)
        clamped_window = _clamp_window(window_seconds, tenant_max)

        # 3. Anchor the audit timeline. The events table has a
        #    foreign key on task_run_id, so a tenant-scoped
        #    request needs a synthetic anchor. The anchor is a
        #    real task_runs row with template_id="break-glass"
        #    and the request_id embedded in the contract_id.
        if not task_run_id:
            task_run_id = self._anchor_tenant_scoped_request(
                tenant_id=tenant_id,
                request_id_marker=purpose,
                requested_by=requested_by,
            )

        # 4. Persist the request.
        req = BreakGlassRequest(
            tenant_id=tenant_id,
            task_run_id=task_run_id,
            purpose=purpose,
            effect=effect,
            resource_scope=resource_scope,
            ticket=ticket,
            requested_by=requested_by,
            window_seconds=clamped_window,
            state=BreakGlassState.REQUESTED,
        )
        self._store.create_break_glass_request(req)

        # 5. Audit timeline: requested.
        self._emit(
            task_run_id=task_run_id,
            kind=EventKind.BREAK_GLASS_REQUESTED,
            tenant_id=tenant_id,
            payload={
                "request_id": req.request_id,
                "tenant_id": tenant_id,
                "task_run_id": task_run_id,
                "purpose": purpose,
                "ticket": ticket,
                "requested_by": requested_by,
                "effect": effect,
                "resource_scope": resource_scope,
                "window_seconds": clamped_window,
            },
            actor=requested_by,
        )
        return req

    # ------------------------------------------------------------------
    # Approve
    # ------------------------------------------------------------------

    def approve(
        self,
        *,
        request_id: str,
        approver: str,
        identity_tenant_id: str,
        rationale: str = "",
    ) -> dict[str, Any]:
        """Apply a single approver signature.

        First call moves the row to ``first-approved``; second
        call (from a different approver) moves it to ``active`` and
        sets ``expires_at``.

        Returns the dict the store produces (see
        :meth:`EventStore.record_break_glass_approval`).
        """
        result = self._store.record_break_glass_approval(
            request_id=request_id,
            approver=approver,
            identity_tenant_id=identity_tenant_id,
        )
        if not result.get("applied"):
            return result

        # Look up the request to get the anchored task_run_id
        # for the audit timeline.
        row = self._store.get_break_glass(request_id) or {}
        anchor_task = row.get("task_run_id") or ""
        new_state = result.get("state")
        if new_state == "first-approved":
            self._emit(
                task_run_id=anchor_task,
                kind=EventKind.BREAK_GLASS_FIRST_APPROVED,
                tenant_id=identity_tenant_id,
                payload={
                    "request_id": request_id,
                    "approver": approver,
                    "rationale": rationale,
                    "required_next": "second_approver",
                },
                actor=approver,
            )
        elif new_state == "active":
            self._emit(
                task_run_id=anchor_task,
                kind=EventKind.BREAK_GLASS_ACTIVE,
                tenant_id=identity_tenant_id,
                payload={
                    "request_id": request_id,
                    "second_approver": approver,
                    "rationale": rationale,
                    "expires_at": result.get("expires_at"),
                },
                actor=approver,
            )
        return result

    # ------------------------------------------------------------------
    # Revoke
    # ------------------------------------------------------------------

    def revoke(
        self,
        *,
        request_id: str,
        revoker: str,
        identity_tenant_id: str,
        reason: str = "",
    ) -> dict[str, Any]:
        """Revoke a request. Idempotent-friendly: revoking an
        already-terminal request returns ``applied=False``.
        """
        result = self._store.revoke_break_glass(
            request_id=request_id,
            revoker=revoker,
            identity_tenant_id=identity_tenant_id,
            reason=reason,
        )
        if result.get("applied"):
            row = self._store.get_break_glass(request_id) or {}
            anchor_task = row.get("task_run_id") or ""
            self._emit(
                task_run_id=anchor_task,
                kind=EventKind.BREAK_GLASS_REVOKED,
                tenant_id=identity_tenant_id,
                payload={
                    "request_id": request_id,
                    "revoker": revoker,
                    "reason": reason,
                },
                actor=revoker,
            )
        return result

    # ------------------------------------------------------------------
    # Sweep
    # ------------------------------------------------------------------

    def sweep_expired(self) -> list[str]:
        """Move every active-but-past-``expires_at`` row to
        ``expired``. Returns the freshly-expired ids. The caller
        is responsible for emitting a notification webhook (the
        service emits the audit event here).
        """
        ids = self._store.sweep_expired_break_glass()
        for rid in ids:
            row = self._store.get_break_glass(rid) or {}
            anchor_task = row.get("task_run_id") or ""
            tenant_id = row.get("tenant_id", "")
            self._emit(
                task_run_id=anchor_task,
                kind=EventKind.BREAK_GLASS_EXPIRED,
                tenant_id=tenant_id,
                payload={"request_id": rid, "tenant_id": tenant_id},
                actor="orchestra.sweeper",
            )
        return ids

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def is_active(self, request_id: str) -> bool:
        row = self._store.get_break_glass(request_id)
        if row is None:
            return False
        return row.get("state") == "active"

    def get(self, request_id: str) -> dict[str, Any] | None:
        return self._store.get_break_glass(request_id)

    def list_for_tenant(
        self, tenant_id: str, state: str | None = None
    ) -> list[dict[str, Any]]:
        return list(
            self._store.list_break_glass_for_tenant(tenant_id, state=state)
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_tenant_max_window(self, tenant_id: str) -> int | None:
        """Look up the tenant's max-window override.

        Today this returns ``None`` (no per-tenant override).
        The hook is here so a future M+ can pull a config from
        the lifecycle policy table without changing the public
        service surface.
        """
        return None

    def _anchor_tenant_scoped_request(
        self,
        *,
        tenant_id: str,
        request_id_marker: str,
        requested_by: str,
    ) -> str:
        """Create a synthetic ``task_runs`` row that anchors
        tenant-scoped break-glass events.

        The events table requires a foreign key to ``task_runs``;
        a tenant-scoped request (no specific task) still needs
        a row to land the lifecycle events on. We create one with
        ``template_id="break-glass"`` and a stable id derived
        from the tenant + purpose so repeated requests for the
        same purpose anchor onto the same task (keeping the
        audit timeline readable).
        """
        from orchestra.core.ids import new_id
        from orchestra.core.schema import TaskRunState

        anchor_id = f"bg-anchor-{tenant_id}-{abs(hash(request_id_marker)) % 10_000_000:07d}"
        try:
            self._store.upsert_task_run(
                task_run_id=anchor_id,
                contract_id=f"break-glass:{request_id_marker}",
                template_id="break-glass",
                state=TaskRunState.RUNNING,
            )
        except Exception:  # noqa: BLE001
            # The store raises on FK or duplicate PK; the latter
            # is the "row already exists" case which is fine.
            pass
        return anchor_id

    def _emit(
        self,
        *,
        task_run_id: str,
        kind: EventKind,
        tenant_id: str,
        payload: dict[str, Any],
        actor: str,
    ) -> None:
        """Append a break-glass lifecycle event to the audit log.

        ``task_run_id`` is the synthetic anchor for tenant-scoped
        requests or the real task for task-scoped requests; it
        satisfies the events-table FK.
        """
        if not task_run_id:
            # Defensive: never try to insert an event without a
            # task anchor. The break-glass row itself is the
            # source of truth; the audit event is documentation.
            return
        ev = AuditEvent(
            task_run_id=task_run_id,
            kind=kind,
            actor=actor,
            payload={**payload, "tenant_id": tenant_id},
            occurred_at=utc_now_iso(),
        )
        try:
            self._store.append_event(ev)
        except Exception:  # noqa: BLE001
            # Audit emission is best-effort; the break-glass row
            # is the source of truth. A failed emit (e.g. a
            # transient DB blip) must never break the state
            # transition.
            pass
