"""Deterministic Router.

The Router picks one Capability from the Eligible Set. It is the *only*
component allowed to bind a Capability to a Node, and the choice is
re-derivable from (manifest snapshot, policy, eligible set) so the
audit trail is reproducible.
"""
from __future__ import annotations

from dataclasses import dataclass

from orchestra.core.schema import (
    CapabilityManifest,
    NodeSpec,
    RoutingDecision,
    SecurityLabel,
)
from orchestra.registry.eligible import EligibleSet, compute_eligible_set
from orchestra.registry.manifest_store import ManifestStore
from orchestra.registry.policy import PolicyDecision, PolicyEngine, PolicyRequest


@dataclass(frozen=True)
class RoutingResult:
    decision: RoutingDecision
    denied: list[tuple[str, str]]  # (capability_id, reason)


def _build_request(
    node: NodeSpec,
    manifest: CapabilityManifest,
    data_label: SecurityLabel,
    purpose_code: str,
    region: str,
    budget_remaining_usd: float,
) -> PolicyRequest:
    return PolicyRequest(
        node=node,
        capability=manifest,
        data_label=data_label,
        purpose_code=purpose_code,
        region=region,
        budget_remaining_usd=budget_remaining_usd,
    )


class Router:
    def __init__(self, store: ManifestStore, policy: PolicyEngine) -> None:
        self._store = store
        self._policy = policy

    def eligible_set(self, node: NodeSpec) -> EligibleSet:
        return compute_eligible_set(node, self._store)

    def route(
        self,
        node: NodeSpec,
        data_label: SecurityLabel,
        purpose_code: str,
        region: str,
        budget_remaining_usd: float,
    ) -> RoutingResult:
        """Pick a capability for the node.

        Algorithm:
        1. Build the Eligible Set.
        2. For each entry (already sorted by score), ask the PDP.
        3. The first ``allow`` wins. If everything is denied, the
           candidate for the pre-approved Fallback is returned as the
           chosen capability only if it is in the *original* set; we then
           set the decision to the Fallback and record every denial.
        4. The RoutingDecision is fully serializable so it can be
           embedded in the Plan and in the audit trail.
        """
        eligible = self.eligible_set(node)
        denied: list[tuple[str, str]] = []
        last_decision: PolicyDecision | None = None
        for entry in eligible.entries:
            req = _build_request(
                node,
                entry.manifest,
                data_label,
                purpose_code,
                region,
                budget_remaining_usd,
            )
            d = self._policy.decide(req)
            last_decision = d
            if d.allow:
                decision = RoutingDecision(
                    node_id=node.node_id,
                    chosen_capability_id=entry.capability_id,
                    chosen_manifest_id=entry.manifest.manifest_id(),
                    eligible_set=eligible.capability_ids(),
                    rejected={cid: r for cid, r in denied},
                    rationale=(
                        f"picked {entry.capability_id}: {d.reason} "
                        f"(score={entry.score:.3f}, rule={d.rule_id})"
                    ),
                )
                return RoutingResult(decision=decision, denied=denied)
            denied.append((entry.capability_id, d.reason))

        # Everything denied. Use the pre-approved Fallback if any.
        if node.fallback_capability_id and node.fallback_capability_id in self._store:
            fb_manifest = self._store.get(node.fallback_capability_id)
            decision = RoutingDecision(
                node_id=node.node_id,
                chosen_capability_id=fb_manifest.capability_id,
                chosen_manifest_id=fb_manifest.manifest_id(),
                eligible_set=eligible.capability_ids(),
                rejected={cid: r for cid, r in denied},
                rationale=(
                    f"all primary candidates denied; using pre-approved "
                    f"fallback {fb_manifest.capability_id}. last reason: "
                    f"{last_decision.reason if last_decision else 'n/a'}"
                ),
            )
            return RoutingResult(decision=decision, denied=denied)

        # No eligible set, no fallback. Surface the last reason.
        reason = (
            last_decision.reason if last_decision else "no eligible candidates"
        )
        decision = RoutingDecision(
            node_id=node.node_id,
            chosen_capability_id="",
            chosen_manifest_id="",
            eligible_set=eligible.capability_ids(),
            rejected={cid: r for cid, r in denied},
            rationale=f"routing failed: {reason}",
        )
        return RoutingResult(decision=decision, denied=denied)
