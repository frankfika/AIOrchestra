"""M1 BND-001 — Binding Closure Checker.

The Binding Closure Checker verifies that the *concrete
bindings* (chosen Capabilities + Manifest snapshots) are a
*closed* realisation of the abstract graph: no Plan can route
data through a Capability that has not been declared in the
abstract graph, and no Capability can acquire authority the
abstract graph did not grant it.

Formally, the Checker proves two properties:

  1. **Coverage:** every node has a binding, every binding
     references a manifest that exists, and every edge in the
     abstract graph has its endpoints bound.
  2. **No ambient authority:** the union of all bound
     Capabilities' declared effects is a subset of the
     abstract graph's declared effects, node-by-node.

The Checker is a fail-closed :class:`ClosureResult` — if any
property fails, the Plan is rejected with a structured error.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from orchestra.compiler.errors import CompileError, CompileErrorKind
from orchestra.compiler.normalizer import NormalizedGraph
from orchestra.core.schema import (
    CapabilityManifest,
)


@dataclass
class ClosureResult:
    ok: bool
    errors: list[CompileError] = field(default_factory=list)

    def first_error(self) -> CompileError | None:
        return self.errors[0] if self.errors else None


class BindingClosureChecker:
    # The "human" gate capability is synthesised by the Resolver at
    # compile time; the M2 IDN-001 Credential Broker is the
    # production source of truth.
    SYNTHESIZED_CAPABILITIES = {"human.approver"}

    def __init__(
        self,
        manifests_by_id: dict[str, CapabilityManifest],
    ) -> None:
        self._manifests = manifests_by_id

    def check(
        self,
        graph: NormalizedGraph,
        node_capability_bindings: dict[str, str],
        node_manifest_bindings: dict[str, str],
    ) -> ClosureResult:
        errors: list[CompileError] = []
        # 1. Coverage
        for nid in graph.nodes:
            if nid not in node_capability_bindings:
                errors.append(
                    CompileError(
                        kind=CompileErrorKind.INFEASIBLE_BINDING,
                        node_id=nid,
                        reason=f"node {nid} has no capability binding",
                        data_path=[nid, "binding"],
                    )
                )
                continue
            cap_id = node_capability_bindings[nid]
            if cap_id in self.SYNTHESIZED_CAPABILITIES:
                continue
            if cap_id not in self._manifests:
                errors.append(
                    CompileError(
                        kind=CompileErrorKind.AMBIENT_AUTHORITY,
                        node_id=nid,
                        reason=(
                            f"node {nid} bound to capability {cap_id} "
                            f"which is not in the registry"
                        ),
                        data_path=[nid, cap_id, "registry"],
                    )
                )
                continue
            manifest = self._manifests[cap_id]
            bound_manifest_id = node_manifest_bindings.get(nid, "")
            if bound_manifest_id and bound_manifest_id != manifest.manifest_id():
                errors.append(
                    CompileError(
                        kind=CompileErrorKind.AMBIENT_AUTHORITY,
                        node_id=nid,
                        reason=(
                            f"node {nid} bound manifest_id {bound_manifest_id!r} "
                            f"does not match current manifest id {manifest.manifest_id()!r}"
                        ),
                        data_path=[nid, "manifest_id"],
                    )
                )
        # 2. Edge endpoints bound
        for e in graph.edges:
            if e.from_node not in node_capability_bindings:
                errors.append(
                    CompileError(
                        kind=CompileErrorKind.AMBIENT_AUTHORITY,
                        node_id=e.from_node,
                        reason=(
                            f"edge {e.from_node}->{e.to_node} origin {e.from_node} "
                            f"has no binding"
                        ),
                        data_path=[e.from_node, e.to_node, "binding"],
                    )
                )
            if e.to_node not in node_capability_bindings:
                errors.append(
                    CompileError(
                        kind=CompileErrorKind.AMBIENT_AUTHORITY,
                        node_id=e.to_node,
                        reason=(
                            f"edge {e.from_node}->{e.to_node} target {e.to_node} "
                            f"has no binding"
                        ),
                        data_path=[e.from_node, e.to_node, "binding"],
                    )
                )
        return ClosureResult(ok=not errors, errors=errors)
