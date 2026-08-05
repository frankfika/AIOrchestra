"""M3 XFR-001 — Field Projector."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from orchestra.core.errors import OrchestraError
from orchestra.core.ids import digest_json
from orchestra.core.schema import FieldManifest


@dataclass
class ProjectionResult:
    projected: dict[str, Any]
    dropped_fields: list[str]
    projected_bytes: int
    digest: str


class FieldProjector:
    """Apply a :class:`FieldManifest` to a payload.

    The projection is deterministic. Two runs with the same
    manifest + payload produce byte-identical projected payloads.
    """

    def project(self, manifest: FieldManifest, payload: dict[str, Any]) -> ProjectionResult:
        if not isinstance(payload, dict):
            raise OrchestraError("payload must be a dict")
        allowed = set(manifest.allowed_fields)
        projected: dict[str, Any] = {}
        dropped: list[str] = []
        for k, v in payload.items():
            if k in allowed:
                projected[k] = self._apply_redactions(manifest, k, v)
            else:
                dropped.append(k)
        # Deterministic ordering.
        ordered = {k: projected[k] for k in sorted(projected)}
        serialised = json.dumps(ordered, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        if manifest.byte_budget is not None and len(serialised.encode("utf-8")) > manifest.byte_budget:
            raise EgressBudgetExceeded(
                f"projected payload {len(serialised.encode('utf-8'))} > "
                f"byte_budget {manifest.byte_budget}"
            )
        return ProjectionResult(
            projected=ordered,
            dropped_fields=sorted(dropped),
            projected_bytes=len(serialised.encode("utf-8")),
            digest=digest_json(ordered),
        )

    def _apply_redactions(self, manifest: FieldManifest, key: str, value: Any) -> Any:
        for rule in manifest.redaction_rules:
            if rule.get("field") != key:
                continue
            op = rule.get("op")
            if op == "drop":
                return None
            if op == "hash":
                return digest_json(value)
            if op == "tokenize":
                return f"<token:{digest_json(value)[:8]}>"
            if isinstance(op, str) and op.startswith("partial-"):
                try:
                    n = int(op.split("-", 1)[1])
                except ValueError:
                    continue
                if isinstance(value, str) and len(value) > n:
                    return value[:n] + "…"
        return value


class EgressBudgetExceeded(OrchestraError):
    """The projected payload exceeded the manifest's byte_budget."""
