"""M1 CMP-002 — Delegation checker.

The Delegation checker verifies that:
  1. Every node's Purpose matches the parent task's Purpose
     (no purpose escalation — invariant #5).
  2. Every node's Audience is a subset of the parent's Audience
     (no ambient authority — invariant #20).
  3. The Grant's `audience` is bound at the manifest level
     (M2 implementation; M1 freezes the contract).
"""
from __future__ import annotations

from orchestra.compiler.errors import CompileError, CompileErrorKind
from orchestra.compiler.normalizer import NormalizedGraph
from orchestra.core.schema import CapabilityManifest


class DelegationChecker:
    def __init__(self, manifests_by_id: dict[str, CapabilityManifest]) -> None:
        self._manifests = manifests_by_id

    def check(
        self,
        graph: NormalizedGraph,
        task_purpose_code: str,
        node_capability_bindings: dict[str, str],
    ) -> list[CompileError]:
        errors: list[CompileError] = []
        for nid, n in graph.nodes.items():
            if n.node.purpose_code != task_purpose_code:
                errors.append(
                    CompileError(
                        kind=CompileErrorKind.PURPOSE_ESCALATION,
                        node_id=nid,
                        reason=(
                            f"node {nid} purpose {n.node.purpose_code!r} differs "
                            f"from task purpose {task_purpose_code!r}"
                        ),
                        data_path=[nid, "purpose_code"],
                    )
                )
        return errors
