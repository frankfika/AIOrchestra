"""M1 CMP-001 — Parser.

The Parser takes a :class:`TaskTemplate` + :class:`InitialInputs`
and produces a *Candidate Graph*: the abstract graph the rest of
the Compiler reasons about. The Candidate Graph is not yet a
:class:`ExecutionPlan` — it has not been bound to Capabilities,
Signed, or Resolved. It is the input to :class:`Normalizer`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orchestra.core.errors import ContractViolation
from orchestra.core.schema import (
    DataView,
    TaskContract,
    TaskTemplate,
    ValueRef,
)


@dataclass
class CandidateNode:
    """A normalised node before capability binding."""

    node_id: str
    name: str
    purpose_code: str
    requires_purpose: str
    requires_views: list[DataView]
    eligible_capability_kinds: list[str]
    declared_effect_kinds: list[str]
    requires_approval: bool
    fallback_capability_id: str | None
    timeout_ms: int
    requirements: list[dict[str, Any]] = field(default_factory=list)
    output_label_rule: str = "join"


@dataclass
class CandidateEdge:
    from_node: str
    to_node: str
    when: str = "always"


@dataclass
class CandidateGraph:
    template_id: str
    template_version: str
    nodes: dict[str, CandidateNode]
    edges: list[CandidateEdge]
    inputs: list[DataView]
    purpose: str

    def topological_order(self) -> list[str]:
        """Return nodes in a topological order. Raises
        :class:`ContractViolation` on cycle.
        """
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
            raise ContractViolation(
                f"candidate graph for template {self.template_id} has a cycle"
            )
        return order


class Parser:
    """Parse a TaskTemplate + initial inputs into a CandidateGraph."""

    def parse(
        self, template: TaskTemplate, contract: TaskContract
    ) -> CandidateGraph:
        # Validate that every node's purpose is one of the template's
        # required purposes.
        for n in template.nodes:
            if n.requires_purpose.code not in {
                p.code for p in template.required_purposes
            } and template.required_purposes:
                # The template may declare a single required_purpose
                # but individual nodes may also be purpose-specific.
                # M1 is strict: every node's purpose must be in
                # ``required_purposes`` OR be the singleton wildcard.
                if {p.code for p in template.required_purposes} != {n.requires_purpose.code}:
                    raise ContractViolation(
                        f"node {n.node_id!r} purpose {n.requires_purpose.code!r} not in "
                        f"template.required_purposes"
                    )

        nodes: dict[str, CandidateNode] = {}
        for n in template.nodes:
            nodes[n.node_id] = CandidateNode(
                node_id=n.node_id,
                name=n.name,
                purpose_code=n.requires_purpose.code,
                requires_purpose=n.requires_purpose.code,
                requires_views=list(n.requires_views),
                eligible_capability_kinds=[k.value for k in n.eligible_capability_kinds],
                declared_effect_kinds=[e.kind.value for e in n.declared_effects],
                requires_approval=n.requires_approval,
                fallback_capability_id=n.fallback_capability_id,
                timeout_ms=n.timeout_ms,
                requirements=[],
                output_label_rule="join",
            )
        edges = [
            CandidateEdge(
                from_node=e.from_node, to_node=e.to_node, when=e.when
            )
            for e in template.edges
        ]
        # Validate edge endpoints.
        for e in edges:
            if e.from_node not in nodes:
                raise ContractViolation(
                    f"edge from_node {e.from_node!r} not in template.nodes"
                )
            if e.to_node not in nodes:
                raise ContractViolation(
                    f"edge to_node {e.to_node!r} not in template.nodes"
                )

        # Cycle check.
        CandidateGraph(
            template_id=template.template_id,
            template_version=template.version,
            nodes=nodes,
            edges=edges,
            inputs=list(contract.inputs),
            purpose=contract.purpose.code,
        ).topological_order()

        return CandidateGraph(
            template_id=template.template_id,
            template_version=template.version,
            nodes=nodes,
            edges=edges,
            inputs=list(contract.inputs),
            purpose=contract.purpose.code,
        )
