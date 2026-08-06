"""LIT-002: Static Capability Registry + OPA-style Policy + Router.

The registry is the *static* layer of the demo: manifests, the single
policy bundle, the eligible-set computation, and the deterministic Router.
P0 must be reproducible, so all of these are pure functions of their
inputs — no time-based or random tie-breakers.
"""
from orchestra.registry.bootstrap import load_default_manifests, load_default_policy
from orchestra.registry.eligible import EligibleSet, compute_eligible_set
from orchestra.registry.manifest_store import ManifestStore
from orchestra.registry.policy import PolicyDecision, PolicyEngine
from orchestra.registry.router import Router, RoutingResult

__all__ = [
    "ManifestStore",
    "PolicyEngine",
    "PolicyDecision",
    "EligibleSet",
    "compute_eligible_set",
    "Router",
    "RoutingResult",
    "load_default_manifests",
    "load_default_policy",
]
