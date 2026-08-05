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
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

from orchestra.core.errors import OrchestraError
from orchestra.core.schema import (
    AuditEvent,
    EventKind,
    FieldManifest,
)
from orchestra.xfr.projector import FieldProjector, EgressBudgetExceeded


class EgressDenied(OrchestraError):
    """The Egress PEP refused to forward the call."""


class EgressPEP:
    def __init__(
        self,
        *,
        manifest_lookup: Callable[[str, str], Optional[FieldManifest]],
        # (capability_id, view_name) -> FieldManifest
    ) -> None:
        self._lookup = manifest_lookup
        self._projector = FieldProjector()

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
            raise EgressDenied(
                f"no FieldManifest for capability {capability_id} view {view_name}"
            )
        try:
            result = self._projector.project(manifest, payload)
        except EgressBudgetExceeded as e:
            raise EgressDenied(str(e)) from e
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
