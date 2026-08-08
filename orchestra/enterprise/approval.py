"""M24 W2 — ApprovalService (ADR-0013).

A thin wrapper around the EventStore's persistent-approval methods.
The service is stateless; PostgreSQL is the source of truth.

The engine consumes this service to:

* register a new approval row when a node needs a human decision
  (``create_for_node``),
* resolve a decision once an approver clicks the button
  (``decide``),
* re-create in-process :class:`asyncio.Event` wake-up handles on
  startup so a process restart mid-task does not silently drop
  pending approvals (``reload_pending_for_tenant``).

The default ``Coordinator._default_approval_handler`` still waits
on an in-process :class:`asyncio.Event`; the service is the
*authority* — the event is just a wake-up cache.
"""
from __future__ import annotations

from typing import Any

from orchestra.coordinator.event_store import EventStore
from orchestra.core.schema import ApprovalRecord


class ApprovalService:
    """Stateless façade over :class:`EventStore` for persistent approvals.

    The dev path constructs this with the same :class:`EventStore`
    the Coordinator already uses; production swaps the store for a
    PG-replicated one without changing the service surface.
    """

    def __init__(self, store: EventStore) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_for_node(
        self,
        *,
        task_run_id: str,
        node_id: str,
        tenant_id: str,
        requested_by: str,
        ticket: str | None = None,
        required_approvers: int = 1,
    ) -> ApprovalRecord:
        """Register a pending approval row.

        Idempotent on ``(task_run_id, node_id)`` — the underlying
        ``create_approval`` returns the existing row when one is
        already there, so the engine can re-call this from a
        restart path without double-inserting.
        """
        return self._store.create_approval(
            task_run_id=task_run_id,
            node_id=node_id,
            tenant_id=tenant_id,
            requested_by=requested_by,
            required_approvers=required_approvers,
            ticket=ticket,
        )

    # ------------------------------------------------------------------
    # Decide
    # ------------------------------------------------------------------

    def decide(
        self,
        *,
        approval_id: str,
        decision: str,
        decided_by: str,
        identity_tenant_id: str,
        rationale: str = "",
    ) -> dict[str, Any]:
        """Atomically apply an approve/reject decision.

        Returns the dict from
        :meth:`EventStore.record_approval_decision` (keys:
        ``applied``, ``state``, ``version``, ``decisions_seen``,
        ``reason``). The engine uses ``state`` to decide whether
        to wake the in-process event; the API surfaces the
        ``reason`` as a Problem Details extension.
        """
        return self._store.record_approval_decision(
            approval_id=approval_id,
            decision=decision,
            decided_by=decided_by,
            identity_tenant_id=identity_tenant_id,
            rationale=rationale,
        )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, approval_id: str) -> dict[str, Any] | None:
        """Read a single approval row (or None when missing)."""
        return self._store.get_approval(approval_id)

    def reload_pending_for_tenant(self, tenant_id: str) -> list[dict[str, Any]]:
        """Return every pending approval for a tenant.

        The engine calls this on startup to re-attach
        :class:`asyncio.Event` wake-up handles to approvals that
        were in-flight when the process last died.
        """
        return list(self._store.list_approvals_for_tenant(tenant_id, state="pending"))

    def list_for_tenant(
        self, tenant_id: str, state: str | None = None
    ) -> list[dict[str, Any]]:
        return list(self._store.list_approvals_for_tenant(tenant_id, state=state))

    def list_decisions(self, approval_id: str) -> list[dict[str, Any]]:
        return list(self._store.list_approval_decisions(approval_id))
