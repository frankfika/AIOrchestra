"""P0 in-process Policy Engine.

The dev plan (§0.1.1 P0 row) fixes a *single* OPA-style policy for P0. The
production code in M1+ will swap this for the real OPA Rego backend behind
the same :class:`PolicyEngine` interface. To make the swap mechanical, the
engine's input shape matches what we'd send to ``POST /v1/data/.../allow``
and the output is a structured decision with a human-readable reason.

The policy bundle is a list of rules. Each rule has:

  - ``when``: a predicate over the request
  - ``decision``: ``allow`` or ``deny``
  - ``reason``: human-readable string
  - ``invariant``: the invariant number this rule enforces (e.g. ``"1"``)

Rules are evaluated **in order**; the first match wins. If no rule matches,
the default is ``deny`` (fail-closed, see plan §0.1.2 row "Authority Epoch"
and invariant #8). This is non-negotiable: an empty policy must never
silently allow.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from orchestra.core.errors import ContractViolation
from orchestra.core.schema import (
    CapabilityManifest,
    DataClassification,
    NodeSpec,
    SecurityLabel,
)


@dataclass(frozen=True)
class PolicyRequest:
    """The input to a PDP decision.

    Kept small on purpose: a real PDP would receive the full Plan + Manifest
    snapshot + run state. P0 only needs the binding we're trying to
    authorize.
    """

    node: NodeSpec
    capability: CapabilityManifest
    data_label: SecurityLabel
    purpose_code: str
    region: str
    budget_remaining_usd: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node.node_id,
            "node_requires_approval": self.node.requires_approval,
            "node_eligible_kinds": [k.value for k in self.node.eligible_capability_kinds],
            "node_effects": [e.model_dump() for e in self.node.declared_effects],
            "capability_id": self.capability.capability_id,
            "capability_kind": self.capability.kind.value,
            "capability_endpoint": self.capability.endpoint,
            "capability_effects": [e.model_dump() for e in self.capability.declared_effects],
            "data_classification": self.data_label.classification.value,
            "data_residency": self.data_label.residency,
            "purpose_code": self.purpose_code,
            "region": self.region,
            "budget_remaining_usd": self.budget_remaining_usd,
        }


@dataclass(frozen=True)
class PolicyDecision:
    allow: bool
    reason: str
    invariant: str
    rule_id: str


class PolicyEngine:
    """Single-bundle policy engine.

    A rule is a tuple ``(rule_id, predicate, decision)``. The first rule
    whose predicate returns True wins. If none match, default-deny.
    """

    DEFAULT_DENY_REASON = "no policy rule matched (default deny)"

    def __init__(self, rules: Iterable[dict[str, Any]] | None = None) -> None:
        self._rules: list[dict[str, Any]] = list(rules or [])

    def add_rule(self, rule: dict[str, Any]) -> None:
        if not {"id", "when", "decision"}.issubset(rule):
            raise ContractViolation(
                f"policy rule must have id, when, decision keys: {rule!r}"
            )
        self._rules.append(rule)

    def decide(self, req: PolicyRequest) -> PolicyDecision:
        ctx = req.to_dict()
        for rule in self._rules:
            try:
                if bool(rule["when"](ctx)):
                    return PolicyDecision(
                        allow=(rule["decision"] == "allow"),
                        reason=rule.get("reason", ""),
                        invariant=rule.get("invariant", ""),
                        rule_id=rule["id"],
                    )
            except Exception as e:  # noqa: BLE001
                # A buggy predicate is treated as "no match" so the next rule
                # can fire. We still record the error in the reason so the
                # audit timeline can surface it.
                return PolicyDecision(
                    allow=False,
                    reason=f"rule {rule['id']!r} raised {type(e).__name__}: {e}",
                    invariant=rule.get("invariant", ""),
                    rule_id=rule["id"],
                )
        return PolicyDecision(
            allow=False,
            reason=self.DEFAULT_DENY_REASON,
            invariant="8",
            rule_id="default-deny",
        )


# ---------------------------------------------------------------------------
# Built-in P0 rule predicates
# ---------------------------------------------------------------------------


def _is_public_model(cap: dict[str, Any]) -> bool:
    return cap["capability_kind"] == "public-model"


def _is_local_or_a2a(cap: dict[str, Any]) -> bool:
    return cap["capability_kind"] in ("local-model", "a2a-agent")


def _has_payment_or_publish_effect(node: dict[str, Any]) -> bool:
    for e in node["node_effects"]:
        if e["kind"] in ("payment", "publish"):
            return True
    return False


def _is_write_node(node: dict[str, Any]) -> bool:
    for e in node["node_effects"]:
        if e["kind"] in ("write", "delete", "payment", "publish"):
            return True
    return False


def _data_class_is(c: DataClassification | str) -> Any:
    target = c.value if isinstance(c, DataClassification) else c
    return lambda ctx: ctx["data_classification"] == target


def default_p0_rules() -> list[dict[str, Any]]:
    """The single P0 policy bundle.

    Order matters: the most specific deny rules come first. See the
    ``invariant`` field for traceability to the 26-invariants matrix.
    """
    return [
        # Invariant #1: Restricted data must NEVER reach a public Adapter.
        {
            "id": "no-restricted-to-public",
            "invariant": "1",
            "decision": "deny",
            "reason": "restricted data must not flow to a public-model adapter",
            "when": lambda c: (
                _data_class_is(DataClassification.RESTRICTED)(c) and _is_public_model(c)
            ),
        },
        # Invariant #1: Internal data is allowed to reach a public Adapter
        # only when the node has been schema-projected. P0 only allows this
        # for the dedicated ``public-research`` node, which is the only node
        # in the contract-review template that has a public Adapter in its
        # eligible set.
        {
            "id": "internal-to-public-needs-public-research",
            "invariant": "1",
            "decision": "deny",
            "reason": (
                "internal data can only flow to public-model via a public-research node"
            ),
            "when": lambda c: (
                _data_class_is(DataClassification.INTERNAL)(c)
                and _is_public_model(c)
                and c["node_id"] != "public_research"
            ),
        },
        # Invariant #3: Capabilities must be in the node's eligible kinds.
        {
            "id": "kind-mismatch",
            "invariant": "3",
            "decision": "deny",
            "reason": "capability kind not in node's eligible set",
            "when": lambda c: c["capability_kind"] not in c["node_eligible_kinds"],
        },
        # Invariant #20: the Adapter cannot introduce new Effects the
        # node didn't declare.
        {
            "id": "effect-escalation",
            "invariant": "20",
            "decision": "deny",
            "reason": "capability declares effects the node did not declare",
            "when": lambda c: any(
                e["kind"] not in {n["kind"] for n in c["node_effects"]}
                for e in c["capability_effects"]
            ),
        },
        # Region/residency check: ``local`` and ``public`` are both
        # wildcards (data with no geographic restriction). Any other
        # residency (e.g. ``cn``, ``us``) must match the run region.
        {
            "id": "residency-mismatch",
            "invariant": "13",
            "decision": "deny",
            "reason": "data residency and run region are incompatible",
            "when": lambda c: c["data_residency"] not in (
                "local",
                "public",
                c["region"],
            ),
        },
        # Budget guard.
        {
            "id": "budget-exceeded",
            "invariant": "10",
            "decision": "deny",
            "reason": "capability cost exceeds remaining budget",
            "when": lambda c: c["budget_remaining_usd"] < 0,
        },
        # Default: high-risk Effects require an approval node downstream
        # (this is checked at compile time, not in the PDP).
        # Allow everything else.
        {
            "id": "default-allow",
            "invariant": "0",
            "decision": "allow",
            "reason": "no deny rule matched",
            "when": lambda c: True,
        },
    ]
