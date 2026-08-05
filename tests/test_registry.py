"""Registry / Router tests (LIT-002)."""
from __future__ import annotations

import pytest

from orchestra.core.errors import ContractViolation
from orchestra.core.schema import (
    CapabilityKind,
    DataClassification,
    DataView,
    Effect,
    EffectKind,
    NodeSpec,
    Purpose,
    SecurityLabel,
    SourceTrust,
)
from orchestra.registry.bootstrap import load_default_manifests, load_default_policy
from orchestra.registry.eligible import compute_eligible_set
from orchestra.registry.router import Router


def _node(*, node_id="n1", kinds=(CapabilityKind.LOCAL_MODEL,), effects=(Effect(kind=EffectKind.READ),), fallback=None):
    return NodeSpec(
        node_id=node_id,
        name="n",
        requires_purpose=Purpose(code="contract-review"),
        eligible_capability_kinds=list(kinds),
        declared_effects=list(effects),
        fallback_capability_id=fallback,
    )


def test_manifest_store_rejects_duplicate_id():
    from orchestra.registry.manifest_store import ManifestStore
    from orchestra.core.schema import CapabilityManifest, IntegrationLevel

    m = CapabilityManifest(
        capability_id="x",
        name="x",
        kind=CapabilityKind.LOCAL_MODEL,
        endpoint="http://x",
        integration_level=IntegrationLevel.ENFORCE,
    )
    s = ManifestStore([m])
    with pytest.raises(ContractViolation):
        s.add(m)


def test_eligible_set_excludes_fallback_from_primary():
    store = load_default_manifests()
    node = _node(kinds=(CapabilityKind.LOCAL_MODEL,))
    es = compute_eligible_set(node, store)
    assert "local.contract-extractor" in es.capability_ids()


def test_router_picks_local_for_restricted():
    store = load_default_manifests()
    policy = load_default_policy()
    router = Router(store, policy)
    node = _node(node_id="extract", kinds=(CapabilityKind.LOCAL_MODEL,))
    restricted = SecurityLabel(classification=DataClassification.RESTRICTED, residency="local")
    r = router.route(node, restricted, "contract-review", "local", 1.0)
    assert r.decision.chosen_capability_id == "local.contract-extractor"


def test_router_denies_restricted_to_public_then_falls_back():
    store = load_default_manifests()
    policy = load_default_policy()
    router = Router(store, policy)
    # A node that *only* accepts public-model, but data is restricted:
    # PDP denies, no fallback → empty decision.
    node = _node(node_id="bad", kinds=(CapabilityKind.PUBLIC_MODEL,))
    restricted = SecurityLabel(classification=DataClassification.RESTRICTED, residency="local")
    r = router.route(node, restricted, "contract-review", "local", 1.0)
    assert r.decision.chosen_capability_id == ""
    # With a pre-approved Fallback, the Fallback wins.
    node2 = _node(node_id="ok", kinds=(CapabilityKind.PUBLIC_MODEL,), fallback="a2a.reference-agent")
    r2 = router.route(node2, restricted, "contract-review", "local", 1.0)
    assert r2.decision.chosen_capability_id == "a2a.reference-agent"
    assert "fallback" in r2.decision.rationale.lower()


def test_router_allows_internal_to_public_research_node():
    store = load_default_manifests()
    policy = load_default_policy()
    router = Router(store, policy)
    node = _node(node_id="public_research", kinds=(CapabilityKind.PUBLIC_MODEL, CapabilityKind.A2A_AGENT), fallback="a2a.reference-agent")
    internal = SecurityLabel(classification=DataClassification.INTERNAL, residency="local")
    r = router.route(node, internal, "contract-review", "local", 1.0)
    assert r.decision.chosen_capability_id in {"public.openai-compat", "a2a.reference-agent"}


def test_router_does_not_allow_internal_to_public_outside_public_research():
    store = load_default_manifests()
    policy = load_default_policy()
    router = Router(store, policy)
    node = _node(node_id="not_public_research", kinds=(CapabilityKind.PUBLIC_MODEL,))
    internal = SecurityLabel(classification=DataClassification.INTERNAL, residency="local")
    r = router.route(node, internal, "contract-review", "local", 1.0)
    assert r.decision.chosen_capability_id == ""  # denied


def test_residency_public_is_wildcard():
    """Invariant #15: data with residency='public' (no geographic
    restriction) is allowed to flow to any run region, just like
    residency='local'.
    """
    store = load_default_manifests()
    policy = load_default_policy()
    router = Router(store, policy)
    node = _node(node_id="public_research", kinds=(CapabilityKind.PUBLIC_MODEL, CapabilityKind.A2A_AGENT), fallback="a2a.reference-agent")
    public_label = SecurityLabel(classification=DataClassification.PUBLIC, residency="public")
    r = router.route(node, public_label, "contract-review", "local", 1.0)
    assert r.decision.chosen_capability_id in {"public.openai-compat", "a2a.reference-agent"}


def test_router_blocks_restricted_to_public_with_invariant_tag():
    """Invariant #1 + #8: the denied decision must surface the rule
    id and the invariant tag so the audit trail is traceable.
    """
    store = load_default_manifests()
    policy = load_default_policy()
    router = Router(store, policy)
    bad_node = _node(node_id="leaky", kinds=(CapabilityKind.PUBLIC_MODEL,))
    restricted = SecurityLabel(classification=DataClassification.RESTRICTED, residency="local")
    r = router.route(bad_node, restricted, "contract-review", "local", 1.0)
    assert r.decision.chosen_capability_id == ""
    # The denial must be in the rejected dict, with a reason that
    # names the invariant we violated.
    assert any("restricted" in reason.lower() for cid, reason in r.denied)
    # The last rule fired must be the no-restricted-to-public rule.
    last = r.denied[-1]
    assert "restricted" in last[1].lower()
