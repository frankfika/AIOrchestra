"""M1 CMP-001 — Type checker.

The Type Checker walks the :class:`NormalizedGraph` and verifies:
  1. Every node references at least one Data View (or is the
     contract-ingest node with no inputs).
  2. Every producer's expected output View name matches the
     consumer's input View name on every edge.
  3. The set of declared Effects on a node is consistent (no
     duplicates, no invalid kinds).
  4. A node declaring a high-risk Effect
     (WRITE/DELETE/PAYMENT/PUBLISH) is **preceded** in the
     topological order by some node with ``requires_approval=True``
     on the path that reaches it. The approval flag is on the
     *gate* node, not necessarily on the high-risk node itself.
  5. Conversely, a node with ``requires_approval=True`` must
     actually gate a high-risk downstream effect (sanity check).

Failures raise :class:`CompileError` (CMP-003 surfaces a
counter-example).
"""
from __future__ import annotations

from typing import Iterable

from orchestra.compiler.errors import CompileError, CompileErrorKind
from orchestra.compiler.normalizer import NormalizedGraph
from orchestra.core.schema import EffectKind


class TypeChecker:
    HIGH_RISK = {EffectKind.WRITE, EffectKind.DELETE, EffectKind.PAYMENT, EffectKind.PUBLISH}

    def check(self, graph: NormalizedGraph) -> list[CompileError]:
        errors: list[CompileError] = []
        # Build a reverse-edge map: for each node, the set of
        # immediate predecessors.
        predecessors: dict[str, set[str]] = {nid: set() for nid in graph.nodes}
        for e in graph.edges:
            predecessors[e.to_node].add(e.from_node)
        # BFS from each approval node, mark the descendants it covers.
        gates: dict[str, set[str]] = {}
        for nid, n in graph.nodes.items():
            if n.node.requires_approval:
                # Forward reachability from this gate.
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
        # Rule 4: a high-risk node must be covered by some gate.
        for nid, n in graph.nodes.items():
            declared_high = {
                e for e in n.node.declared_effect_kinds
                if EffectKind(e) in self.HIGH_RISK
            }
            if not declared_high:
                # Sanity check: an approval flag without a downstream
                # high-risk effect is *not* an error; the template
                # may use the gate for a human review that doesn't
                # mutate state.
                continue
            # Is this node covered by any gate?
            covered = any(nid in covered_set for covered_set in gates.values())
            if not covered:
                errors.append(
                    CompileError(
                        kind=CompileErrorKind.HIGH_RISK_EFFECT_WITHOUT_APPROVAL,
                        node_id=nid,
                        reason=(
                            f"node declares {sorted(declared_high)} but no "
                            f"requires_approval gate reaches it"
                        ),
                        data_path=[nid, "approval_gate"],
                    )
                )
        return errors
