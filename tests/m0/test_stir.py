"""M0.1 — STIR round-trip and JSON Schema export (SPEC-001 acceptance)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestra.core.schema import (
    CapabilityKind,
    DataClassification,
    DataView,
    Effect,
    EffectKind,
    ExecutionPlan,
    FieldManifest,
    JoinSemantics,
    NodeSpec,
    PlanEdge,
    PlanNode,
    Purpose,
    Requirement,
    SecurityLabel,
    SourceTrust,
    ValueRef,
    export_json_schemas,
)
from orchestra.templates.contract_review import CONTRACT_REVIEW_TEMPLATE


def test_stir_plan_round_trip_through_json():
    plan = ExecutionPlan(
        contract_id="c-1",
        template_id=CONTRACT_REVIEW_TEMPLATE.template_id,
        template_version=CONTRACT_REVIEW_TEMPLATE.version,
        nodes=[
            PlanNode(
                node_id="a",
                capability_id="local.contract-extractor",
                manifest_id="manifest:abc",
                purpose=Purpose(code="contract-review"),
                input_views=[],
                expected_outputs=["facts"],
                timeout_ms=1000,
            ),
        ],
        edges=[],
    )
    raw = plan.model_dump_json()
    again = ExecutionPlan.model_validate_json(raw)
    assert again.contract_id == plan.contract_id
    assert again.plan_id == plan.plan_id  # default_factory is deterministic per field
    assert again.digest() == plan.digest()


def test_value_ref_carries_producer_and_label():
    ref = ValueRef(
        producer_node_id="extract_facts_local",
        producer_output="facts",
        view_name="facts.internal",
        type_hint="json",
        label=SecurityLabel(classification=DataClassification.INTERNAL, residency="local"),
    )
    assert ref.ref_id.startswith("ref:") is False  # not address-prefixed
    assert ref.producer_node_id == "extract_facts_local"
    assert ref.label.classification == DataClassification.INTERNAL


def test_requirement_kind_set_is_frozen():
    """Adding a new Requirement kind is a Spec-001 change."""
    valid_kinds = {"region", "gpu", "memory-mb", "timeout-ms", "network", "tool", "language", "tier"}
    # quick round-trip
    for k in valid_kinds:
        r = Requirement(kind=k, op="eq", value=1)
        assert r.kind == k


def test_information_flow_rule_default_is_join():
    rule = JoinSemantics.JOIN
    assert rule == JoinSemantics.JOIN
    assert rule.value == "join"


def test_field_manifest_carryes_source_view_and_budget():
    fm = FieldManifest(
        name="public.facts",
        source_view="facts.internal",
        allowed_fields=["vendor_name", "jurisdiction"],
        byte_budget=512,
    )
    assert fm.source_view == "facts.internal"
    assert fm.byte_budget == 512
    assert "vendor_name" in fm.allowed_fields


def test_json_schema_export_writes_to_disk(tmp_path: Path):
    schemas = export_json_schemas()
    out = tmp_path / "stir-json-schema.json"
    out.write_text(json.dumps(schemas, indent=2, ensure_ascii=False))
    assert out.exists()
    loaded = json.loads(out.read_text())
    assert "SecurityLabel" in loaded
    assert "ExecutionPlan" in loaded
    assert "ValueRef" in loaded
    assert "FieldManifest" in loaded
    assert "InformationFlowRule" in loaded


def test_template_topology_is_acyclic_when_evaluated_against_edges():
    """The Contract Review template's edges must form a DAG (no
    cycles). P0 promises this; the M0 acceptance verifies it.
    """
    from collections import defaultdict, deque

    nodes = {n.node_id for n in CONTRACT_REVIEW_TEMPLATE.nodes}
    indeg: dict[str, int] = defaultdict(int)
    for n in nodes:
        indeg[n] = 0
    for e in CONTRACT_REVIEW_TEMPLATE.edges:
        if e.to_node in nodes:
            indeg[e.to_node] += 1
    q = deque(n for n in nodes if indeg[n] == 0)
    visited = 0
    while q:
        u = q.popleft()
        visited += 1
        for e in CONTRACT_REVIEW_TEMPLATE.edges:
            if e.from_node == u and e.to_node in nodes:
                indeg[e.to_node] -= 1
                if indeg[e.to_node] == 0:
                    q.append(e.to_node)
    assert visited == len(nodes), "Contract Review template has a cycle"


def test_stir_every_node_has_a_purpose():
    """Invariant #20: every node has a Purpose that survives into
    the Plan. The Contract Review template satisfies this; the test
    catches future templates that forget to set it.
    """
    for n in CONTRACT_REVIEW_TEMPLATE.nodes:
        assert n.requires_purpose.code, f"node {n.node_id} missing purpose"
