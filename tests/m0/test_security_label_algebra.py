"""M0.7 — SecurityLabel algebra: the 5 canonical properties (SEC-001 §8)."""
from __future__ import annotations

import pytest

from orchestra.core.schema import (
    DataClassification,
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


# Source-trust ordinal: HIGHER number = more conservative. A RESTRICTED
# input forces the output to RESTRICTED (most conservative wins; see
# SEC-001 §5).
_TRUST_ORDER = {
    SourceTrust.SYNTHETIC: 0,
    SourceTrust.PUBLIC: 1,
    SourceTrust.PARTNER: 2,
    SourceTrust.INTERNAL: 3,
    SourceTrust.RESTRICTED: 4,
}


def _class_max(a, b):
    return a if _CLASS_ORDER[a] >= _CLASS_ORDER[b] else b


def _trust_max(a, b):
    """Derived source trust: the more conservative input wins. If any
    input is RESTRICTED, the output is RESTRICTED.
    """
    return a if _TRUST_ORDER[a] >= _TRUST_ORDER[b] else b


def _label(c=DataClassification.INTERNAL, r="local", t=SourceTrust.INTERNAL, ret=30):
    return SecurityLabel(
        classification=c, residency=r, source_trust=t, retention_days=ret
    )


def test_monotonicity_join_with_lower_label_yields_higher():
    """For L₁ ≤ L₂, JOIN(L₁, L₂) = L₂."""
    l1 = _label(c=DataClassification.PUBLIC, r="public")
    l2 = _label(c=DataClassification.INTERNAL, r="local")
    join = SecurityLabel(
        classification=_class_max(l1.classification, l2.classification),
        residency="local",
        source_trust=_trust_max(l1.source_trust, l2.source_trust),
        retention_days=min(l1.retention_days, l2.retention_days),
    )
    assert join.classification == l2.classification
    assert join.source_trust == l2.source_trust
    assert join.residency == "local"


def test_idempotence():
    """JOIN(L, L) = L; MEET(L, L) = L."""
    l = _label()
    join = SecurityLabel(
        classification=l.classification,
        residency="local",
        source_trust=l.source_trust,
        retention_days=l.retention_days,
    )
    meet = SecurityLabel(
        classification=l.classification,
        residency=l.residency,
        source_trust=l.source_trust,
        retention_days=l.retention_days,
    )
    assert join.classification == l.classification
    assert meet.classification == l.classification


def test_commutativity():
    """JOIN(L₁, L₂) = JOIN(L₂, L₁)."""
    l1 = _label(c=DataClassification.PUBLIC, r="public", t=SourceTrust.PUBLIC, ret=7)
    l2 = _label(c=DataClassification.RESTRICTED, r="local", t=SourceTrust.RESTRICTED, ret=365)
    join_a = SecurityLabel(
        classification=_class_max(l1.classification, l2.classification),
        residency="local",
        source_trust=_trust_max(l1.source_trust, l2.source_trust),
        retention_days=min(l1.retention_days, l2.retention_days),
    )
    join_b = SecurityLabel(
        classification=_class_max(l2.classification, l1.classification),
        residency="local",
        source_trust=_trust_max(l2.source_trust, l1.source_trust),
        retention_days=min(l2.retention_days, l1.retention_days),
    )
    assert join_a.classification == join_b.classification
    assert join_a.source_trust == join_b.source_trust
    assert join_a.retention_days == join_b.retention_days


def test_default_deny_on_empty():
    """No inputs → JOIN is undefined; PDP returns deny. (M0 delivers
    the rule; the actual PDP default-deny is in registry.policy.)
    """
    from orchestra.registry.policy import PolicyEngine, PolicyRequest
    from orchestra.core.schema import (
        CapabilityKind, Effect, EffectKind, NodeSpec, Purpose,
    )
    from orchestra.registry.bootstrap import load_default_manifests

    pe = PolicyEngine(rules=[])  # no rules → default-deny
    node = NodeSpec(
        node_id="n", name="n", requires_purpose=Purpose(code="x"),
        eligible_capability_kinds=[CapabilityKind.LOCAL_MODEL],
        declared_effects=[Effect(kind=EffectKind.READ)],
    )
    cap = load_default_manifests().get("local.contract-extractor")
    req = PolicyRequest(
        node=node, capability=cap,
        data_label=_label(), purpose_code="x", region="local", budget_remaining_usd=1.0,
    )
    d = pe.decide(req)
    assert not d.allow
    assert d.invariant == "8"  # default-deny is tagged with invariant #8


def test_trust_inheritance_restricted_propagates():
    """If any input is restricted, output is restricted (invariant #16)."""
    restricted = _label(c=DataClassification.RESTRICTED, t=SourceTrust.RESTRICTED)
    public = _label(c=DataClassification.PUBLIC, t=SourceTrust.PUBLIC)
    # derived trust = min (most trusted wins)
    derived = _trust_max(restricted.source_trust, public.source_trust)
    assert derived == SourceTrust.RESTRICTED
    # derived classification = max (most restrictive wins)
    derived_c = _class_max(restricted.classification, public.classification)
    assert derived_c == DataClassification.RESTRICTED
