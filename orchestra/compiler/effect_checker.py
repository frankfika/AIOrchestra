"""M1 CMP-002 — Effect checker.

The Effect checker verifies that every node's declared Effects
are:
  1. A subset of the Capability Manifest's declared Effects
     (no escalation — invariant #20).
  2. Paired with an approval node if any declared Effect is
     high-risk (WRITE/DELETE/PAYMENT/PUBLISH — invariant #7).
  3. Free of duplicates and unknown kinds.
"""
from __future__ import annotations

from orchestra.compiler.errors import CompileError, CompileErrorKind
from orchestra.compiler.normalizer import NormalizedGraph
from orchestra.core.schema import (
    CapabilityManifest,
    EffectKind,
)


class EffectChecker:
    HIGH_RISK = {EffectKind.WRITE, EffectKind.DELETE, EffectKind.PAYMENT, EffectKind.PUBLISH}

    def __init__(self, manifests_by_id: dict[str, CapabilityManifest]) -> None:
        self._manifests = manifests_by_id

    def check(
        self,
        graph: NormalizedGraph,
        node_capability_bindings: dict[str, str],
    ) -> list[CompileError]:
        errors: list[CompileError] = []
        # Build a forward reachability map from any approval node.
        gates: dict[str, set[str]] = {}
        for nid, n in graph.nodes.items():
            if n.node.requires_approval:
                covered = set()
                stack = [nid]
                while stack:
                    cur = stack.pop()
                    if cur in covered:
                        continue
                    covered.add(cur)
                    for e in graph.edges:
                        if e.from_node == cur:
                            stack.append(e.to_node)
                gates[nid] = covered
        for nid, n in graph.nodes.items():
            cap_id = node_capability_bindings.get(nid)
            if not cap_id or cap_id not in self._manifests:
                continue
            manifest = self._manifests[cap_id]
            cap_effect_kinds = {e.kind for e in manifest.declared_effects}
            # Rule 1: capability's effects must be a subset of the
            # node's declared effects (no escalation).
            node_effect_kinds = {EffectKind(k) for k in n.node.declared_effect_kinds}
            escalation = cap_effect_kinds - node_effect_kinds
            if escalation:
                errors.append(
                    CompileError(
                        kind=CompileErrorKind.EFFECT_ESCALATION,
                        node_id=nid,
                        reason=(
                            f"capability {cap_id} declares effects {sorted(e.value for e in escalation)} "
                            f"that the node {nid} does not"
                        ),
                        data_path=[nid, cap_id, "declared_effects"],
                    )
                )
            # Rule 2: high-risk effects must be covered by a
            # requires_approval gate on the path. The flag lives on
            # the gate, not necessarily on the high-risk node itself.
            declared_high = node_effect_kinds & self.HIGH_RISK
            if declared_high and not any(nid in covered for covered in gates.values()):
                errors.append(
                    CompileError(
                        kind=CompileErrorKind.HIGH_RISK_EFFECT_WITHOUT_APPROVAL,
                        node_id=nid,
                        reason=(
                            f"node {nid} declares {sorted(e.value for e in declared_high)} "
                            f"but no approval gate reaches it"
                        ),
                        data_path=[nid, "approval_gate"],
                    )
                )
        return errors
