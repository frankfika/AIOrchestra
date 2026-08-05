"""M1 CMP-001 — Normalizer.

The Normalizer takes a :class:`CandidateGraph` and produces a
:class:`NormalizedGraph` in which:
  - every edge's `from_node` has an entry in `value_refs` of the
    `to_node` (the dataflow is explicit, not implicit)
  - every node has a default :class:`InformationFlowRule` if
    none was declared
  - duplicate edges are merged
  - `when="always"` is the default

The Normalizer does not decide anything; it prepares the graph
for the type checker and the Info Flow checker.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orchestra.compiler.parser import (
    CandidateEdge,
    CandidateGraph,
    CandidateNode,
)
from orchestra.core.schema import (
    JoinSemantics,
    ValueRef,
)


@dataclass
class NormalizedNode:
    """A node with explicit inputs (ValueRef list) and a default label rule."""

    node: CandidateNode
    input_refs: list[ValueRef] = field(default_factory=list)
    join_rule: JoinSemantics = JoinSemantics.JOIN
    explicit_output_label: dict[str, Any] | None = None


@dataclass
class NormalizedGraph:
    template_id: str
    template_version: str
    nodes: dict[str, NormalizedNode]
    edges: list[CandidateEdge]
    purpose: str

    def topological_order(self) -> list[str]:
        from collections import defaultdict, deque

        indeg: dict[str, int] = defaultdict(int)
        for n in self.nodes:
            indeg[n] = 0
        for e in self.edges:
            indeg[e.to_node] += 1
        q = deque(n for n in self.nodes if indeg[n] == 0)
        order: list[str] = []
        while q:
            u = q.popleft()
            order.append(u)
            for e in self.edges:
                if e.from_node == u:
                    indeg[e.to_node] -= 1
                    if indeg[e.to_node] == 0:
                        q.append(e.to_node)
        if len(order) != len(self.nodes):
            raise RuntimeError("normalized graph has a cycle (impossible after parse)")
        return order


class Normalizer:
    def normalize(self, graph: CandidateGraph) -> NormalizedGraph:
        # Merge duplicate edges.
        seen = set()
        edges: list[CandidateEdge] = []
        for e in graph.edges:
            k = (e.from_node, e.to_node, e.when)
            if k in seen:
                continue
            seen.add(k)
            edges.append(e)

        # Build input_refs per node.
        ref_by_node: dict[str, list[ValueRef]] = {nid: [] for nid in graph.nodes}
        for e in edges:
            ref_by_node[e.to_node].append(
                ValueRef(
                    producer_node_id=e.from_node,
                    producer_output="*",
                    view_name=(
                        graph.nodes[e.from_node].requires_views[0].name
                        if graph.nodes[e.from_node].requires_views
                        else None
                    ),
                )
            )

        nodes = {
            nid: NormalizedNode(
                node=graph.nodes[nid],
                input_refs=ref_by_node[nid],
                join_rule=JoinSemantics.JOIN,
            )
            for nid in graph.nodes
        }
        return NormalizedGraph(
            template_id=graph.template_id,
            template_version=graph.template_version,
            nodes=nodes,
            edges=edges,
            purpose=graph.purpose,
        )
