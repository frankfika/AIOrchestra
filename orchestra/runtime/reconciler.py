"""M2 RUN-002 — Reconciler.

The :class:`Reconciler` periodically walks the Event Store and
asks: *for every Node Run whose last event is older than the
Lease TTL, what is its current state?*

The Reconciler implements the plan's "Unknown handling" rule
(see the Fencing module docstring and the dev plan §0.1.2 row
"Retry"). It never *blindly* retries a Node Run that has an
ambiguous outcome; it asks the Adapter (or the human approver)
to confirm before issuing a new Lease.

A :class:`ReconcilerReport` is the structured output the
Coordinator surfaces to the audit timeline.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC

from orchestra.core.ids import new_id
from orchestra.core.time import utc_now_iso
from orchestra.runtime.lease import Lease, LeaseState


@dataclass
class ReconcilerReport:
    report_id: str
    generated_at: str
    unknown_node_runs: list[str] = field(default_factory=list)
    expired_leases: list[str] = field(default_factory=list)
    revoked_leases: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)


class Reconciler:
    """Pure-function reconciler.

    Takes the current leases + node-runs and produces a
    :class:`ReconcilerReport`. The Coordinator calls this
    after every dispatch flush and at a fixed interval.
    """

    def __init__(self, lease_ttl_seconds: int = 600) -> None:
        self._ttl = lease_ttl_seconds

    def reconcile(
        self,
        leases: list[Lease],
        node_run_states: dict[str, str],
        now_iso: str | None = None,
    ) -> ReconcilerReport:
        from datetime import datetime

        now = datetime.fromisoformat(now_iso.replace("Z", "+00:00")) if now_iso else datetime.now(UTC)
        report = ReconcilerReport(
            report_id=new_id(),
            generated_at=utc_now_iso(),
        )
        for lease in leases:
            if lease.state == LeaseState.REVOKED:
                report.revoked_leases.append(lease.lease_id)
                report.actions.append(
                    f"lease {lease.lease_id} was revoked; node {lease.node_run_id} "
                    f"must reach a terminal state without re-acquiring a lease"
                )
                continue
            if lease.state in (LeaseState.PENDING, LeaseState.ACTIVE):
                # Check expiry.
                expiry = datetime.fromisoformat(lease.expires_at.replace("Z", "+00:00"))
                if expiry <= now:
                    report.expired_leases.append(lease.lease_id)
                    ns = node_run_states.get(lease.node_run_id, "unknown")
                    if ns in ("running", "awaiting-approval"):
                        report.unknown_node_runs.append(lease.node_run_id)
                        report.actions.append(
                            f"node {lease.node_run_id} is in Unknown "
                            f"(lease {lease.lease_id} expired without "
                            f"completion); requires explicit human / adapter decision"
                        )
        return report
