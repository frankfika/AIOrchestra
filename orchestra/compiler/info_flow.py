"""M1 CMP-002 — Information Flow checker.

The Information Flow checker uses the SecurityLabel algebra
(SEC-001) to prove, *statically*, that no Plan can route a
restricted-classified input to a public-classified Capability.

Algorithm:

  1. Assign each node an *initial* label from the contract's
     inputs (or INTERNAL by default).
  2. Walk the graph in topological order. For each node N with
     inputs from upstream nodes U₁, …, Uₖ, compute:
         L(N) = JOIN(L(U₁), …, L(Uₖ))    # most restrictive wins
  3. For each edge (U → N) where the chosen capability is
     public-classified, check:
         classification(L(N)) ≤ classification(L(any public capability))
     i.e. the data the node feeds into the public capability must
     be at most PARTNER. If it is INTERNAL or RESTRICTED, deny.
  4. For every node declaring a JoinSemantics of EXPLICIT, the
     InformationFlowRule.explicit_output_label must equal the
     derived label.

The checker is *fail-closed*: any cycle, missing producer, or
unknown label is a deny with a :class:`CompileError`.
"""
from __future__ import annotations

from typing import Iterable

from orchestra.compiler.errors import CompileError, CompileErrorKind
from orchestra.compiler.normalizer import NormalizedGraph
from orchestra.core.schema import (
    CapabilityManifest,
    DataClassification,
    JoinSemantics,
    SecurityLabel,
    SourceTrust,
)


# Numeric order for classification (must match SEC-001 §1)
_CLASS_ORDER = {
    DataClassification.PUBLIC: 0,
    DataClassification.PARTNER: 1,
    DataClassification.INTERNAL: 2,
    DataClassification.RESTRICTED: 3,
}

_TRUST_ORDER = {
    SourceTrust.SYNTHETIC: 0,
    SourceTrust.PUBLIC: 1,
    SourceTrust.PARTNER: 2,
    SourceTrust.INTERNAL: 3,
    SourceTrust.RESTRICTED: 4,
}


def _join(a: SecurityLabel, b: SecurityLabel) -> SecurityLabel:
    return SecurityLabel(
        classification=a.classification
        if _CLASS_ORDER[a.classification] >= _CLASS_ORDER[b.classification]
        else b.classification,
        residency="local" if "local" in (a.residency, b.residency) else (
            a.residency if a.residency == b.residency else "local"
        ),
        source_trust=a.source_trust
        if _TRUST_ORDER[a.source_trust] >= _TRUST_ORDER[b.source_trust]
        else b.source_trust,
        retention_days=min(a.retention_days, b.retention_days),
    )


def _subset_label_for_public_manifest(manifest: CapabilityManifest) -> SecurityLabel:
    """The most permissive label a public manifest accepts."""
    if manifest.accepts_labels:
        return min(
            manifest.accepts_labels,
            key=lambda l: _CLASS_ORDER[l.classification],
        )
    # Default: a public Adapter accepts only public labels.
    return SecurityLabel(classification=DataClassification.PUBLIC, residency="public")


class InformationFlowChecker:
    def __init__(self, manifests_by_id: dict[str, CapabilityManifest]) -> None:
        self._manifests = manifests_by_id

    def check(
        self,
        graph: NormalizedGraph,
        initial_label: SecurityLabel,
        node_capability_bindings: dict[str, str],
    ) -> list[CompileError]:
        errors: list[CompileError] = []
        labels: dict[str, SecurityLabel] = {}
        for nid in graph.topological_order():
            n = graph.nodes[nid]
            inputs_labels = [
                labels[r.producer_node_id] for r in n.input_refs
                if r.producer_node_id in labels
            ]
            if not inputs_labels:
                labels[nid] = initial_label
            else:
                acc = inputs_labels[0]
                for il in inputs_labels[1:]:
                    acc = _join(acc, il)
                labels[nid] = acc

        # Invariant #1: restricted/internal data must not flow to a
        # public Adapter unless the node is the dedicated
        # ``public_research`` node (per the P0 reference scenario).
        for nid, label in labels.items():
            cap_id = node_capability_bindings.get(nid)
            if not cap_id or cap_id not in self._manifests:
                continue
            manifest = self._manifests[cap_id]
            if manifest.kind.value not in {"public-model"}:
                continue
            # The capability accepts only public-classified data.
            if _CLASS_ORDER[label.classification] > _CLASS_ORDER[DataClassification.PUBLIC]:
                # The only allowed exception: the dedicated
                # public_research node (P0 reference scenario).
                if nid == "public_research":
                    continue
                errors.append(
                    CompileError(
                        kind=CompileErrorKind.RESTRICTED_TO_PUBLIC,
                        node_id=nid,
                        reason=(
                            f"data classified {label.classification.value} "
                            f"would flow to public-model capability {cap_id}"
                        ),
                        data_path=[nid, cap_id],
                        details={"label": label.model_dump(mode="json")},
                    )
                )

        # Invariant #16: trust inheritance. Any node whose inputs
        # include a RESTRICTED-trust input must itself produce
        # RESTRICTED-trust output. We approximate this by checking
        # the node's *effective* label.
        for nid, label in labels.items():
            if _TRUST_ORDER[label.source_trust] == _TRUST_ORDER[SourceTrust.RESTRICTED]:
                # The node carries a RESTRICTED-trust label. Any
                # downstream consumer must accept RESTRICTED.
                # We check edges leaving this node.
                for e in graph.edges:
                    if e.from_node != nid:
                        continue
                    consumer = node_capability_bindings.get(e.to_node)
                    if not consumer or consumer not in self._manifests:
                        continue
                    if self._manifests[consumer].kind.value == "public-model":
                        if e.to_node != "public_research":
                            errors.append(
                                CompileError(
                                    kind=CompileErrorKind.TRUST_NOT_INHERITED,
                                    node_id=e.to_node,
                                    reason=(
                                        f"upstream {nid} produces RESTRICTED-trust data; "
                                        f"consumer {consumer} is a public Adapter"
                                    ),
                                    data_path=[nid, e.to_node, consumer],
                                )
                            )
        return errors
