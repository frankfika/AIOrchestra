"""Schema tests — verify the frozen P0 vocabulary is well-formed."""
from __future__ import annotations

import json

import pytest

from orchestra.core.hashing import hmac_keygen, hmac_sign, hmac_verify
from orchestra.core.ids import content_addressed_id, digest_json, new_id
from orchestra.core.schema import (
    CapabilityKind,
    DataClassification,
    DataView,
    Effect,
    EffectKind,
    ExecutionPlan,
    NodeGrant,
    PlanEdge,
    PlanNode,
    Purpose,
    RoutingDecision,
    SecurityLabel,
    SourceTrust,
    TaskContract,
)
from orchestra.templates.contract_review import CONTRACT_REVIEW_TEMPLATE


def test_security_label_can_flow_to_lower():
    r = SecurityLabel(classification=DataClassification.RESTRICTED, residency="local")
    i = SecurityLabel(classification=DataClassification.INTERNAL, residency="local")
    p = SecurityLabel(classification=DataClassification.PUBLIC, residency="local")
    # public can flow to any context
    assert p.can_flow_to(r)
    assert p.can_flow_to(i)
    # internal can flow to internal or restricted, not to public
    assert i.can_flow_to(r)
    assert i.can_flow_to(i)
    assert not i.can_flow_to(p)
    # restricted can only flow to restricted
    assert r.can_flow_to(r)
    assert not r.can_flow_to(i)
    assert not r.can_flow_to(p)


def test_security_label_residency_check():
    a = SecurityLabel(classification=DataClassification.INTERNAL, residency="cn")
    b = SecurityLabel(classification=DataClassification.INTERNAL, residency="us")
    # 'cn' is a specific residency and cannot flow to a 'us' context
    assert not a.can_flow_to(b)
    # a 'local' context accepts any specific residency
    assert a.can_flow_to(SecurityLabel(classification=DataClassification.INTERNAL, residency="local"))


def test_data_view_must_have_fields_for_fields_shape():
    v = DataView(name="x", shape="fields", fields=[])
    assert v.fields == []
    # shape=reference requires no fields
    DataView(name="y", shape="reference")


def test_template_serializes_to_canonical_json():
    payload = CONTRACT_REVIEW_TEMPLATE.model_dump(mode="json")
    # round-trip
    again = CONTRACT_REVIEW_TEMPLATE.__class__.model_validate_json(json.dumps(payload))
    assert again.template_id == CONTRACT_REVIEW_TEMPLATE.template_id


def test_plan_digest_is_stable():
    nodes = [
        PlanNode(
            node_id="a",
            capability_id="c1",
            manifest_id="m:c1",
            purpose=Purpose(code="x"),
            input_views=[],
            expected_outputs=[],
            timeout_ms=1000,
        ),
        PlanNode(
            node_id="b",
            capability_id="c2",
            manifest_id="m:c2",
            purpose=Purpose(code="x"),
            input_views=[],
            expected_outputs=[],
            timeout_ms=1000,
        ),
    ]
    edges = [PlanEdge(from_node="a", to_node="b")]
    p = ExecutionPlan(contract_id="c1", template_id="t", template_version="1", nodes=nodes, edges=edges)
    d1 = p.digest()
    d2 = p.digest()
    assert d1 == d2
    # adding a routing decision must change the digest
    p.routing.append(RoutingDecision(node_id="a", chosen_capability_id="c1", chosen_manifest_id="m:c1", eligible_set=["c1"], rationale="t"))
    assert p.digest() != d1


def test_node_grant_keeps_expiry_parseable():
    from datetime import datetime, timezone, timedelta

    g = NodeGrant(
        task_run_id="t",
        node_run_id="n",
        task_id="t",
        node_id="n",
        capability_id="c",
        manifest_id="m",
        data_view=DataView(name="x", shape="reference"),
        purpose=Purpose(code="x"),
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    )
    assert g.grant_id != ""


def test_id_helpers():
    a = new_id()
    b = new_id()
    assert a != b
    cid = content_addressed_id("plan", {"x": 1})
    assert cid.startswith("plan:")
    assert len(cid.split(":")[1]) == 12
    # same payload → same id
    assert content_addressed_id("plan", {"x": 1}) == cid
    # different payload → different id
    assert content_addressed_id("plan", {"x": 2}) != cid


def test_hmac_roundtrip():
    k = hmac_keygen()
    payload = {"a": 1, "b": [1, 2, 3]}
    sig = hmac_sign(k, payload)
    assert hmac_verify(k, payload, sig)
    assert not hmac_verify(k, {"a": 1, "b": [1, 2, 4]}, sig)
    assert not hmac_verify(hmac_keygen(), payload, sig)
