"""M1 RSL-001 — Resolver.

The Resolver takes a :class:`NormalizedGraph` and returns a
:class:`ResolverResult`:
  - ``bindings``: node_id → capability_id (one per node)
  - ``manifests_by_id``: the manifests the bindings reference
  - ``errors``: any policy denials or eligible-set failures

The Resolver differs from the P0 Router in three ways:
  1. It is **plan-time** (called by the Trust Compiler) rather
     than run-time. The Router is the run-time component.
  2. It emits an explicit *Eligible Set* per node, not just the
     chosen capability. The Compiler logs the set so an
     auditor can see what was *available* at compile time.
  3. It supports **Plan Amendment** (CMP-003 re-compile): if a
     chosen capability later becomes unavailable, the
     Resolver's ``amend`` method produces a new
     ResolverResult.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orchestra.compiler.errors import CompileError, CompileErrorKind
from orchestra.compiler.normalizer import NormalizedGraph
from orchestra.core.schema import (
    CapabilityKind,
    Effect,
    EffectKind,
    NodeSpec,
    SecurityLabel,
)
from orchestra.registry.bootstrap import (
    load_default_manifests,
    load_default_policy,
)
from orchestra.registry.eligible import EligibleSet
from orchestra.registry.policy import PolicyEngine
from orchestra.registry.router import Router


@dataclass
class ResolverResult:
    ok: bool
    bindings: dict[str, str] = field(default_factory=dict)
    manifest_bindings: dict[str, str] = field(default_factory=dict)
    eligible_sets: dict[str, EligibleSet] = field(default_factory=dict)
    errors: list[CompileError] = field(default_factory=list)
    fallback_used: dict[str, str] = field(default_factory=dict)

    def first_error(self) -> CompileError | None:
        return self.errors[0] if self.errors else None


class Resolver:
    """Plan-time Resolver.

    Uses the in-process P0 Router for the routing logic; M1+
    can swap in a different policy engine (POL-001 OPA
    backend) without changing this class.
    """

    def __init__(
        self,
        store: Any = None,
        policy: PolicyEngine | None = None,
        router: Router | None = None,
    ) -> None:
        self._store = store or load_default_manifests()
        self._policy = policy or load_default_policy()
        self._router = router or Router(self._store, self._policy)

    def resolve(
        self,
        graph: NormalizedGraph,
        data_label: SecurityLabel,
        region: str = "local",
        budget_usd: float = 1.0,
        template: TaskTemplate | None = None,
    ) -> ResolverResult:
        bindings: dict[str, str] = {}
        manifest_bindings: dict[str, str] = {}
        eligible_sets: dict[str, EligibleSet] = {}
        errors: list[CompileError] = []
        fallback_used: dict[str, str] = {}
        # The P0 Router expects a NodeSpec (with declared_effects as
        # Effect objects). We rebuild a NodeSpec from the
        # NormalizedGraph's CandidateNode for routing.
        for nid in graph.topological_order():
            n = graph.nodes[nid]
            # The "human" approval is a special capability we
            # synthesise at compile time; the M2 Credential
            # Broker (IDN-001) is the source of truth in
            # production.
            if "human" in n.node.eligible_capability_kinds:
                bindings[nid] = "human.approver"
                manifest_bindings[nid] = "manifest:human-approver"
                eligible_sets[nid] = EligibleSet(node_id=nid, entries=())
                continue
            # Rebuild a NodeSpec for the Router.
            kinds = [
                CapabilityKind(k) for k in n.node.eligible_capability_kinds
                if k in {ck.value for ck in CapabilityKind}
            ]
            effects = [Effect(kind=EffectKind(k)) for k in n.node.declared_effect_kinds if k in {ek.value for ek in EffectKind}]
            spec: NodeSpec
            if template is not None:
                spec = template.node(n.node.node_id)
            else:
                from orchestra.core.schema import Purpose as _Purpose
                spec = NodeSpec(
                    node_id=n.node.node_id,
                    name=n.node.name,
                    requires_purpose=_Purpose(code=n.node.requires_purpose),
                    eligible_capability_kinds=kinds,
                    declared_effects=effects,
                    requires_approval=n.node.requires_approval,
                    fallback_capability_id=n.node.fallback_capability_id,
                    timeout_ms=n.node.timeout_ms,
                )
            es = self._router.eligible_set(spec)
            eligible_sets[nid] = es
            if not es.entries:
                errors.append(
                    CompileError(
                        kind=CompileErrorKind.INFEASIBLE_BINDING,
                        node_id=nid,
                        reason=(
                            f"no eligible capability for node {nid} "
                            f"(eligible kinds: {n.node.eligible_capability_kinds})"
                        ),
                        data_path=[nid, "eligible_set"],
                    )
                )
                continue
            r = self._router.route(
                node=spec,
                data_label=data_label,
                purpose_code=graph.purpose,
                region=region,
                budget_remaining_usd=budget_usd,
            )
            if r.decision.chosen_capability_id:
                bindings[nid] = r.decision.chosen_capability_id
                manifest_bindings[nid] = r.decision.chosen_manifest_id
                if r.decision.chosen_capability_id == n.node.fallback_capability_id:
                    fallback_used[nid] = r.decision.chosen_capability_id
            else:
                errors.append(
                    CompileError(
                        kind=CompileErrorKind.INFEASIBLE_BINDING,
                        node_id=nid,
                        reason=(
                            f"all eligible candidates denied; "
                            f"last reason: {r.decision.rationale}"
                        ),
                        data_path=[nid, "policy"],
                    )
                )
        return ResolverResult(
            ok=not errors,
            bindings=bindings,
            manifest_bindings=manifest_bindings,
            eligible_sets=eligible_sets,
            errors=errors,
            fallback_used=fallback_used,
        )

    def amend(
        self,
        previous: ResolverResult,
        unavailable_capability_id: str,
        graph: NormalizedGraph,
        data_label: SecurityLabel,
        region: str = "local",
        budget_usd: float = 1.0,
        template: TaskTemplate | None = None,
    ) -> ResolverResult:
        """Plan Amendment (invariant #6): re-resolve when a chosen
        capability becomes unavailable.
        """
        affected = [
            nid for nid, cap in previous.bindings.items()
            if cap == unavailable_capability_id
        ]
        if not affected:
            return previous
        # Remove affected nodes from previous bindings; let resolve
        # pick new ones.
        new_bindings = {k: v for k, v in previous.bindings.items() if k not in affected}
        new_manifest_bindings = {
            k: v for k, v in previous.manifest_bindings.items() if k not in affected
        }
        # Re-run the full resolve; the previous result is
        # re-derived so the Eligible Set is up to date.
        new_result = self.resolve(graph, data_label, region, budget_usd, template=template)
        new_result.bindings = {**new_bindings, **new_result.bindings}
        new_result.manifest_bindings = {**new_manifest_bindings, **new_result.manifest_bindings}
        return new_result
