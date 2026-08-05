"""Eligible Set computation.

Given a :class:`NodeSpec` and a :class:`ManifestStore`, return every
manifest that *could* be bound to that node, plus a deterministic ordering
key the Router consumes. The plan (§0.1.1 P0) requires the Router to only
choose from this set — it must never expand the candidate set on its own.
"""
from __future__ import annotations

from dataclasses import dataclass

from orchestra.core.errors import ContractViolation
from orchestra.core.schema import (
    CapabilityKind,
    CapabilityManifest,
    NodeSpec,
)
from orchestra.registry.manifest_store import ManifestStore


@dataclass(frozen=True)
class EligibleEntry:
    capability_id: str
    manifest: CapabilityManifest
    score: float
    reasons: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "capability_id": self.capability_id,
            "manifest_id": self.manifest.manifest_id(),
            "kind": self.manifest.kind.value,
            "score": self.score,
            "reasons": list(self.reasons),
            "cost_estimate_usd": self.manifest.cost_estimate_usd,
            "p50_latency_ms": self.manifest.p50_latency_ms,
        }


@dataclass(frozen=True)
class EligibleSet:
    node_id: str
    entries: tuple[EligibleEntry, ...]

    def capability_ids(self) -> list[str]:
        return [e.capability_id for e in self.entries]

    def is_empty(self) -> bool:
        return not self.entries

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "entries": [e.to_dict() for e in self.entries],
        }


def _score_manifest(manifest: CapabilityManifest) -> tuple[float, tuple[str, ...]]:
    """Deterministic scoring. P0 favours lower cost + lower latency.

    Returned tuple is ``(score, reasons)``. The score is *higher is better*
    so the Router can sort ``entries`` by ``-score`` and still be
    deterministic. We bias local / a2a slightly because they avoid egress
    cost in the demo, but the bias is small enough that an explicit
    ``cost_estimate_usd=0`` choice still wins.
    """
    reasons: list[str] = []
    score = 0.0
    # Lower cost is better.
    score += max(0.0, 1.0 - manifest.cost_estimate_usd)
    reasons.append(f"cost={manifest.cost_estimate_usd}")
    # Lower latency is better.
    score += max(0.0, 1.0 - manifest.p50_latency_ms / 5000.0)
    reasons.append(f"p50={manifest.p50_latency_ms}ms")
    # Slight trust bonus for in-tenant kinds.
    if manifest.kind in (CapabilityKind.LOCAL_MODEL, CapabilityKind.A2A_AGENT):
        score += 0.05
        reasons.append("in-tenant-trust+0.05")
    return score, tuple(reasons)


def compute_eligible_set(
    node: NodeSpec,
    store: ManifestStore,
) -> EligibleSet:
    """Return every manifest that is *structurally* eligible.

    This is the "syntactic" eligibility: kind match, no effect escalation.
    The PDP then layers policy on top.
    """
    if not node.eligible_capability_kinds:
        raise ContractViolation(
            f"node {node.node_id!r} declares no eligible_capability_kinds"
        )
    out: list[EligibleEntry] = []
    for manifest in store.all():
        if manifest.kind not in node.eligible_capability_kinds:
            continue
        if manifest.capability_id == node.fallback_capability_id:
            # Fallback lives in a separate slot in the Plan; not eligible as
            # the primary.
            continue
        # Effect escalation check (syntactic).
        node_effects = {e.kind for e in node.declared_effects}
        cap_effects = {e.kind for e in manifest.declared_effects}
        if not cap_effects.issubset(node_effects):
            continue
        score, reasons = _score_manifest(manifest)
        out.append(
            EligibleEntry(
                capability_id=manifest.capability_id,
                manifest=manifest,
                score=score,
                reasons=reasons,
            )
        )
    out.sort(key=lambda e: (-e.score, e.capability_id))
    return EligibleSet(node_id=node.node_id, entries=tuple(out))
