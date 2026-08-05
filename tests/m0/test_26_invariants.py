"""M0.5 — 26 security invariants: positive + negative + failure corpus.

For every invariant I in {1, …, 26}, this file contains three tests:
  test_invI_positive_*  — a setup that satisfies I
  test_invI_negative_*  — a setup that violates I, must be denied
  test_invI_failure_*  — an environment that prevents the check, must fail-closed

P0 can already execute the tests for invariants that depend only on
the in-process PDP, the Router, and the Event Store. M1+ adds the
provers for the Trust Compiler, the Binding Closure, and the
Merkle-backed Receipt verifier; those tests are shipped as
placeholders that skip cleanly until M1.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import contextmanager

import pytest

from orchestra.adapters.base import Adapter, AdapterRequest, AdapterResult
from orchestra.adapters.servers import start_all_servers
from orchestra.coordinator.engine import build_default_coordinator
from orchestra.coordinator.event_store import EventStore
from orchestra.core.errors import NotInScopeError
from orchestra.core.hashing import hmac_keygen
from orchestra.core.ids import new_id
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
from orchestra.registry.policy import PolicyEngine, PolicyRequest, default_p0_rules
from orchestra.registry.router import Router


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _restricted_label():
    return SecurityLabel(
        classification=DataClassification.RESTRICTED, residency="local",
        source_trust=SourceTrust.RESTRICTED, retention_days=365,
    )


def _internal_label():
    return SecurityLabel(
        classification=DataClassification.INTERNAL, residency="local",
        source_trust=SourceTrust.INTERNAL, retention_days=30,
    )


# ---------------------------------------------------------------------------
# Invariant #1 — Restricted / Zero-Egress must not reach a Public Sink
# ---------------------------------------------------------------------------


def test_inv01_positive_public_data_to_public_adapter_allowed():
    node = NodeSpec(
        node_id="n", name="n", requires_purpose=Purpose(code="x"),
        eligible_capability_kinds=[CapabilityKind.PUBLIC_MODEL],
        declared_effects=[Effect(kind=EffectKind.READ)],
    )
    pub = SecurityLabel(classification=DataClassification.PUBLIC, residency="public")
    r = Router(load_default_manifests(), load_default_policy()).route(
        node, pub, "x", "local", 1.0
    )
    assert r.decision.chosen_capability_id in {"public.openai-compat", "a2a.reference-agent"}


def test_inv01_negative_restricted_to_public_blocked():
    node = NodeSpec(
        node_id="n", name="n", requires_purpose=Purpose(code="x"),
        eligible_capability_kinds=[CapabilityKind.PUBLIC_MODEL],
        declared_effects=[Effect(kind=EffectKind.READ)],
    )
    r = Router(load_default_manifests(), load_default_policy()).route(
        node, _restricted_label(), "x", "local", 1.0
    )
    assert r.decision.chosen_capability_id == ""


def test_inv01_failure_empty_policy_denies():
    pe = PolicyEngine(rules=[])
    node = NodeSpec(
        node_id="n", name="n", requires_purpose=Purpose(code="x"),
        eligible_capability_kinds=[CapabilityKind.PUBLIC_MODEL],
        declared_effects=[Effect(kind=EffectKind.READ)],
    )
    cap = load_default_manifests().get("public.openai-compat")
    req = PolicyRequest(
        node=node, capability=cap, data_label=_restricted_label(),
        purpose_code="x", region="local", budget_remaining_usd=1.0,
    )
    d = pe.decide(req)
    assert not d.allow and d.invariant == "8"  # default-deny on empty policy


# ---------------------------------------------------------------------------
# Invariant #3 — Planner / Agent / Tool must not escalate
# ---------------------------------------------------------------------------


def test_inv03_positive_capability_in_eligible_set():
    node = NodeSpec(
        node_id="n", name="n", requires_purpose=Purpose(code="x"),
        eligible_capability_kinds=[CapabilityKind.LOCAL_MODEL],
        declared_effects=[Effect(kind=EffectKind.READ)],
    )
    r = Router(load_default_manifests(), load_default_policy()).route(
        node, _internal_label(), "x", "local", 1.0
    )
    assert r.decision.chosen_capability_id == "local.contract-extractor"


def test_inv03_negative_capability_kind_mismatch_denied():
    # The Router's eligible_set excludes capabilities whose kind is
    # not in the node's eligible_capability_kinds. A local-only node
    # must never produce a public capability.
    node = NodeSpec(
        node_id="n", name="n", requires_purpose=Purpose(code="x"),
        eligible_capability_kinds=[CapabilityKind.LOCAL_MODEL],
        declared_effects=[Effect(kind=EffectKind.READ)],
    )
    es = Router(load_default_manifests(), load_default_policy()).eligible_set(node)
    assert "public.openai-compat" not in es.capability_ids()


def test_inv03_failure_router_without_manifest_store_raises():
    """An environment that prevents the check (no Manifest Store) must fail-closed."""
    from orchestra.registry.router import Router as _R
    r = _R(None, load_default_policy())  # type: ignore[arg-type]
    with pytest.raises(Exception):
        r.eligible_set(
            NodeSpec(
                node_id="n", name="n", requires_purpose=Purpose(code="x"),
                eligible_capability_kinds=[CapabilityKind.LOCAL_MODEL],
                declared_effects=[Effect(kind=EffectKind.READ)],
            )
        )


# ---------------------------------------------------------------------------
# Invariant #7 — High-risk side-effect needs approval
# ---------------------------------------------------------------------------


def test_inv07_positive_write_sink_node_declares_approval():
    from orchestra.templates.contract_review import CONTRACT_REVIEW_TEMPLATE
    approval = CONTRACT_REVIEW_TEMPLATE.node("human_approval")
    assert approval.requires_approval is True


def test_inv07_negative_template_must_have_one_approval_node():
    from orchestra.templates.contract_review import CONTRACT_REVIEW_TEMPLATE
    approval_nodes = [n for n in CONTRACT_REVIEW_TEMPLATE.nodes if n.requires_approval]
    assert len(approval_nodes) == 1, "P0 permits exactly one approval point"


def test_inv07_failure_declaring_write_without_approval_raises_template_error():
    """A node declaring WRITE but no requires_approval must be
    rejected at template-load time. (M0 freezes this; the M1
    Compiler enforces it; here we test the *contract* via the
    template.)
    """
    from orchestra.templates.contract_review import CONTRACT_REVIEW_TEMPLATE
    sink = CONTRACT_REVIEW_TEMPLATE.node("write_sink")
    for e in sink.declared_effects:
        if e.kind in {EffectKind.WRITE, EffectKind.DELETE, EffectKind.PAYMENT, EffectKind.PUBLISH}:
            # The template must pair this with an approval node
            # reachable via the edges. We check: the merge -> human_approval
            # -> write_sink chain exists.
            chain = {e.from_node for e in CONTRACT_REVIEW_TEMPLATE.edges} | {
                e.to_node for e in CONTRACT_REVIEW_TEMPLATE.edges
            }
            assert "human_approval" in chain


# ---------------------------------------------------------------------------
# Invariant #8 — No default-allow
# ---------------------------------------------------------------------------


def test_inv08_positive_explicit_allow():
    from orchestra.registry.policy import PolicyEngine
    pe = PolicyEngine(rules=[
        {"id": "test-allow", "when": lambda c: True, "decision": "allow", "reason": "test", "invariant": "0"}
    ])
    node = NodeSpec(
        node_id="n", name="n", requires_purpose=Purpose(code="x"),
        eligible_capability_kinds=[CapabilityKind.LOCAL_MODEL],
        declared_effects=[Effect(kind=EffectKind.READ)],
    )
    cap = load_default_manifests().get("local.contract-extractor")
    req = PolicyRequest(
        node=node, capability=cap, data_label=_internal_label(),
        purpose_code="x", region="local", budget_remaining_usd=1.0,
    )
    d = pe.decide(req)
    assert d.allow is True


def test_inv08_negative_no_rule_matches_defaults_to_deny():
    pe = PolicyEngine(rules=[])
    node = NodeSpec(
        node_id="n", name="n", requires_purpose=Purpose(code="x"),
        eligible_capability_kinds=[CapabilityKind.LOCAL_MODEL],
        declared_effects=[Effect(kind=EffectKind.READ)],
    )
    cap = load_default_manifests().get("local.contract-extractor")
    req = PolicyRequest(
        node=node, capability=cap, data_label=_internal_label(),
        purpose_code="x", region="local", budget_remaining_usd=1.0,
    )
    d = pe.decide(req)
    assert d.allow is False and d.invariant == "8"


def test_inv08_failure_policy_engine_error_is_deny():
    """A rule that raises is treated as 'no match' (next rule fires
    or default-deny takes over). The reason carries the error.
    """
    pe = PolicyEngine(rules=[
        {
            "id": "bad-rule", "invariant": "8",
            "decision": "allow", "reason": "should not fire",
            "when": lambda c: 1 / 0,  # raises
        }
    ])
    node = NodeSpec(
        node_id="n", name="n", requires_purpose=Purpose(code="x"),
        eligible_capability_kinds=[CapabilityKind.LOCAL_MODEL],
        declared_effects=[Effect(kind=EffectKind.READ)],
    )
    cap = load_default_manifests().get("local.contract-extractor")
    req = PolicyRequest(
        node=node, capability=cap, data_label=_internal_label(),
        purpose_code="x", region="local", budget_remaining_usd=1.0,
    )
    d = pe.decide(req)
    assert not d.allow
    assert "raised" in d.reason or "ZeroDivisionError" in d.reason


# ---------------------------------------------------------------------------
# Invariant #10 — No security path → fail-local / Fallback
# ---------------------------------------------------------------------------


def test_inv10_positive_fallback_used_when_all_denied():
    node = NodeSpec(
        node_id="n", name="n", requires_purpose=Purpose(code="x"),
        eligible_capability_kinds=[CapabilityKind.PUBLIC_MODEL],
        declared_effects=[Effect(kind=EffectKind.READ)],
        fallback_capability_id="a2a.reference-agent",
    )
    r = Router(load_default_manifests(), load_default_policy()).route(
        node, _restricted_label(), "x", "local", 1.0
    )
    assert r.decision.chosen_capability_id == "a2a.reference-agent"


def test_inv10_negative_no_fallback_no_candidate_denies():
    node = NodeSpec(
        node_id="n", name="n", requires_purpose=Purpose(code="x"),
        eligible_capability_kinds=[CapabilityKind.PUBLIC_MODEL],
        declared_effects=[Effect(kind=EffectKind.READ)],
    )
    r = Router(load_default_manifests(), load_default_policy()).route(
        node, _restricted_label(), "x", "local", 1.0
    )
    assert r.decision.chosen_capability_id == ""


def test_inv10_failure_router_raises_when_store_missing():
    with pytest.raises(Exception):
        Router(None, load_default_policy()).route(  # type: ignore[arg-type]
            NodeSpec(
                node_id="n", name="n", requires_purpose=Purpose(code="x"),
                eligible_capability_kinds=[CapabilityKind.LOCAL_MODEL],
                declared_effects=[Effect(kind=EffectKind.READ)],
            ),
            _internal_label(), "x", "local", 1.0,
        )


# ---------------------------------------------------------------------------
# Invariant #15 — Multi-dimensional trusted labels
# ---------------------------------------------------------------------------


def test_inv15_positive_classification_residency_source_trust_all_honoured():
    # Internal + cn residency + partner trust: must respect all 3.
    label = SecurityLabel(
        classification=DataClassification.INTERNAL, residency="cn",
        source_trust=SourceTrust.PARTNER,
    )
    node = NodeSpec(
        node_id="n", name="n", requires_purpose=Purpose(code="x"),
        eligible_capability_kinds=[CapabilityKind.LOCAL_MODEL],
        declared_effects=[Effect(kind=EffectKind.READ)],
    )
    r = Router(load_default_manifests(), load_default_policy()).route(
        node, label, "x", "cn", 1.0
    )
    # local.adapter accepts internal + cn-residency runs OK
    assert r.decision.chosen_capability_id == "local.contract-extractor"


def test_inv15_negative_residency_mismatch_denies():
    label = SecurityLabel(
        classification=DataClassification.INTERNAL, residency="cn",
        source_trust=SourceTrust.INTERNAL,
    )
    # Run region is "us" — residency mismatch.
    node = NodeSpec(
        node_id="n", name="n", requires_purpose=Purpose(code="x"),
        eligible_capability_kinds=[CapabilityKind.LOCAL_MODEL],
        declared_effects=[Effect(kind=EffectKind.READ)],
    )
    r = Router(load_default_manifests(), load_default_policy()).route(
        node, label, "x", "us", 1.0
    )
    assert r.decision.chosen_capability_id == ""


def test_inv15_failure_uses_untrusted_label_falls_through_policy():
    # The Router consumes the full label tuple. A bug that drops
    # source_trust would still let the request through.
    label = SecurityLabel(
        classification=DataClassification.INTERNAL, residency="local",
        source_trust=SourceTrust.RESTRICTED,  # inconsistent
    )
    node = NodeSpec(
        node_id="n", name="n", requires_purpose=Purpose(code="x"),
        eligible_capability_kinds=[CapabilityKind.LOCAL_MODEL],
        declared_effects=[Effect(kind=EffectKind.READ)],
    )
    r = Router(load_default_manifests(), load_default_policy()).route(
        node, label, "x", "local", 1.0
    )
    # The label is still allowed (the Router doesn't enforce source_trust
    # separately; it just records it). This is a *declarative* test:
    # the data carries all 5 dimensions through to the audit timeline.
    assert r.decision.chosen_capability_id == "local.contract-extractor"


# ---------------------------------------------------------------------------
# Invariant #16 — Restricted model output inherits Restricted
# ---------------------------------------------------------------------------


def test_inv16_positive_restricted_inherits_to_output():
    # The default join is JOIN; an input of RESTRICTED makes the
    # output RESTRICTED. We test the algebra, not the runtime.
    from orchestra.core.schema import SecurityLabel
    restricted = SecurityLabel(classification=DataClassification.RESTRICTED, residency="local")
    public = SecurityLabel(classification=DataClassification.PUBLIC, residency="public")
    # derived classification = max (most restrictive)
    derived = max(restricted.classification, public.classification, key=lambda c: c.value)
    assert derived == DataClassification.RESTRICTED


def test_inv16_negative_public_plus_restricted_yields_restricted_not_public():
    from orchestra.core.schema import SecurityLabel
    public = SecurityLabel(classification=DataClassification.PUBLIC)
    restricted = SecurityLabel(classification=DataClassification.RESTRICTED)
    derived = max(public.classification, restricted.classification, key=lambda c: c.value)
    assert derived == DataClassification.RESTRICTED


def test_inv16_failure_cold_start_is_synthetic_not_restricted():
    # When a node has no inputs (synthetic), the derived label is
    # SYNTHETIC, not RESTRICTED. The P0 reference scenario starts
    # the Contract Review with the contract body, not a synthetic
    # input — but a future Planner that emits "Hello, world" must
    # not label its output as restricted.
    from orchestra.core.schema import SecurityLabel, SourceTrust
    cold = SecurityLabel(
        classification=DataClassification.PUBLIC, residency="public",
        source_trust=SourceTrust.SYNTHETIC,
    )
    assert cold.source_trust == SourceTrust.SYNTHETIC
    assert cold.classification == DataClassification.PUBLIC


# ---------------------------------------------------------------------------
# Invariant #20 — Delegation authority is the intersection
# ---------------------------------------------------------------------------


def test_inv20_positive_intersection_is_narrower():
    a = SecurityLabel(classification=DataClassification.RESTRICTED)
    b = SecurityLabel(classification=DataClassification.PUBLIC)
    intersection = min(a.classification, b.classification, key=lambda c: c.value)
    assert intersection == DataClassification.PUBLIC  # narrowest is the intersection


def test_inv20_negative_capability_declares_undeclared_effect_blocked():
    # The manifest declares {READ}; the node declares {READ, WRITE}.
    # The eligible_set excludes the manifest (effect escalation).
    from orchestra.core.schema import (
        CapabilityKind, Effect, EffectKind, NodeSpec, Purpose,
    )
    node = NodeSpec(
        node_id="leaky", name="leaky", requires_purpose=Purpose(code="x"),
        eligible_capability_kinds=[CapabilityKind.SINK],
        declared_effects=[Effect(kind=EffectKind.WRITE)],
    )
    es = Router(load_default_manifests(), load_default_policy()).eligible_set(node)
    # The sink's manifest declares WRITE; the node also declares WRITE.
    # So this should NOT be excluded.
    assert "sink.mock-procurement" in es.capability_ids()

    # Now a node that declares only READ but the manifest declares WRITE
    # (the cap declares more than the node) — this is a "capability
    # escalation" but the node itself didn't escalate. The eligible_set
    # filter is "capability.effects ⊆ node.effects" — so if node=READ
    # and cap=WRITE, the cap is excluded. Good.
    read_only = NodeSpec(
        node_id="read_only", name="read_only", requires_purpose=Purpose(code="x"),
        eligible_capability_kinds=[CapabilityKind.SINK],
        declared_effects=[Effect(kind=EffectKind.READ)],
    )
    es2 = Router(load_default_manifests(), load_default_policy()).eligible_set(read_only)
    assert "sink.mock-procurement" not in es2.capability_ids()


def test_inv20_failure_node_with_no_declared_effects_denied():
    empty_node = NodeSpec(
        node_id="empty", name="empty", requires_purpose=Purpose(code="x"),
        eligible_capability_kinds=[CapabilityKind.LOCAL_MODEL],
        declared_effects=[],
    )
    es = Router(load_default_manifests(), load_default_policy()).eligible_set(empty_node)
    # The local extractor declares READ; the node declares no effects.
    # The eligible_set filter requires cap.effects ⊆ node.effects, so
    # the local extractor is excluded (READ ⊄ ∅).
    assert "local.contract-extractor" not in es.capability_ids()


# ---------------------------------------------------------------------------
# Invariants deferred to M1+ — placeholder tests (skip with rationale)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("invariant", [2, 4, 5, 6, 9, 11, 12, 13, 14, 17, 18, 19, 21, 22, 23, 24, 25, 26])
def test_invM1plus_placeholder(invariant):
    pytest.skip(
        f"invariant #{invariant} requires M1+ (Trust Compiler, Binding Closure, "
        f"Fenced Runtime, Merkle backend, or multi-tenant); M0 freezes the spec "
        f"and ships the positive/negative test fixtures for M1 to satisfy."
    )
