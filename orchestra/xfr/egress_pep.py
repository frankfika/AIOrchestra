"""M3 XFR-001 — Egress PEP.

The Egress PEP wraps every public Adapter call. It enforces:

  1. A :class:`FieldManifest` is supplied. The Coordinator passes
     one per (node, capability) at Plan time; the PEP refuses to
     forward a call without a manifest.
  2. The manifest is current (matches the manifest_id the
     Capability published).
  3. The projected payload obeys the manifest's allowed_fields
     and byte_budget.

The PEP records an ``io.sent`` event with the projected payload
**digest** (never the raw payload) so the audit timeline shows
*what* left the tenant without leaking it.

M13 — when constructed with ``metrics=``, the PEP increments
``orchestra_egress_pep_projection_total`` on success,
``orchestra_egress_pep_denied_total`` on denial, and observes
``orchestra_egress_pep_projection_bytes``. Without metrics, the
PEP behaves exactly as before.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from orchestra.core.errors import OrchestraError
from orchestra.core.schema import (
    AuditEvent,
    EventKind,
    FieldManifest,
)
from orchestra.xfr.projector import EgressBudgetExceeded, FieldProjector

if TYPE_CHECKING:  # pragma: no cover
    from orchestra.observability.metrics import Metrics


class EgressDenied(OrchestraError):
    """The Egress PEP refused to forward the call."""


class EgressPEP:
    def __init__(
        self,
        *,
        manifest_lookup: Callable[[str, str], FieldManifest | None],
        # (capability_id, view_name) -> FieldManifest
        metrics: Metrics | None = None,
    ) -> None:
        self._lookup = manifest_lookup
        self._projector = FieldProjector()
        # M13 — claim the relevant metrics if the caller passed one.
        # The Counter / Histogram objects are idempotent (Metrics.counter
        # returns the same instance for the same name), so wiring them
        # once in __init__ is safe even if multiple PEPs share a registry.
        self._metrics = metrics
        if metrics is not None:
            self._m_projection = metrics.counter(
                "orchestra_egress_pep_projection_total",
                "Total EgressPEP projections.",
                labels=("capability", "view"),
            )
            self._m_denied = metrics.counter(
                "orchestra_egress_pep_denied_total",
                "Total EgressPEP denials.",
                labels=("capability", "view"),
            )
            self._m_bytes = metrics.histogram(
                "orchestra_egress_pep_projection_bytes",
                "Projected payload bytes.",
                labels=("capability",),
            )
        else:
            self._m_projection = None
            self._m_denied = None
            self._m_bytes = None

    def project(
        self,
        *,
        capability_id: str,
        view_name: str,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Apply the manifest and return (projected_payload, manifest_dict)."""
        manifest = self._lookup(capability_id, view_name)
        if manifest is None:
            if self._m_denied is not None:
                self._m_denied.inc(capability=capability_id, view=view_name)
            raise EgressDenied(f"no FieldManifest for capability {capability_id} view {view_name}")
        try:
            result = self._projector.project(manifest, payload)
        except EgressBudgetExceeded as e:
            if self._m_denied is not None:
                self._m_denied.inc(capability=capability_id, view=view_name)
            raise EgressDenied(str(e)) from e
        # Record the projection. Bytes is the JSON-encoded size of the
        # projected payload — the same shape the audit timeline records.
        if self._m_projection is not None:
            self._m_projection.inc(capability=capability_id, view=view_name)
        if self._m_bytes is not None:
            projected_bytes = len(
                json.dumps(result.projected, sort_keys=True, ensure_ascii=False).encode("utf-8")
            )
            self._m_bytes.observe(projected_bytes, capability=capability_id)
        return result.projected, manifest.model_dump(mode="json")

    def audit_event(
        self,
        *,
        task_run_id: str,
        node_run_id: str,
        capability_id: str,
        view_name: str,
        projected: dict[str, Any],
        manifest: dict[str, Any],
        dropped: list[str],
        projected_bytes: int,
    ) -> AuditEvent:
        """Build the io.sent audit event for an egress call.

        The event payload carries the projected digest (NOT the
        raw payload) and the dropped-field list so the audit
        timeline proves *exactly* what left the tenant.
        """
        from orchestra.core.ids import digest_json

        return AuditEvent(
            task_run_id=task_run_id,
            node_run_id=node_run_id,
            kind=EventKind.IO_SENT,
            payload={
                "node_id": capability_id,  # the audit uses capability_id as the "node" of the egress
                "capability_id": capability_id,
                "view_name": view_name,
                "manifest_id": manifest.get("manifest_id"),
                "projected_digest": digest_json(projected),
                "projected_bytes": projected_bytes,
                "dropped_fields": dropped,
            },
        )
