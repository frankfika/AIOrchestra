"""Contract Review Task Template (LIT-001).

The fixed scenario per the P0 Gate:

1. ``ingest_contract``: the upper layer hands Orchestra the raw contract
   text. The input view is the full document, classified ``restricted``.
2. ``extract_facts_local``: a Local Model Adapter extracts a structured
   Fact Set. Output is ``internal`` only.
3. ``public_research``: a public OpenAI-compatible Adapter produces a
   public research summary **using only the projected Fact Set**, never
   the raw contract. An A2A Reference Agent supplies an industry-context
   lookup in parallel (the P0 "limited fan-out"). The node's eligible
   kinds include both ``public-model`` and ``a2a-agent``; the Router
   picks deterministically.
4. ``merge``: a deterministic local merge produces the review summary.
   The merge is in-process — it's not a real Adapter call, it's a
   Coordinator function. (Still surfaced as a PlanNode so the audit
   timeline shows it.)
5. ``human_approval``: the only approval point. The Coordinator pauses
   and waits for a human decision.
6. ``write_sink``: on ``approve``, write to the Mock Procurement Sink.
   On ``reject``, no write — the task ends with state ``rejected``.
"""
from __future__ import annotations

from typing import Any

from orchestra.core.schema import (
    CapabilityKind,
    DataView,
    EdgeSpec,
    Effect,
    EffectKind,
    ExecutionPlan,
    NodeSpec,
    PlanEdge,
    PlanNode,
    Purpose,
    TaskTemplate,
)

# ---------------------------------------------------------------------------
# Shared purpose
# ---------------------------------------------------------------------------


def get_default_purpose() -> Purpose:
    return Purpose(
        code="contract-review",
        description="Review a supplier contract and decide on the procurement write.",
    )


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


FULL_CONTRACT_VIEW = DataView(
    name="contract.full",
    shape="fields",
    fields=[
        "vendor_name",
        "buyer_name",
        "contract_amount",
        "payment_terms",
        "effective_date",
        "expiration_date",
        "termination_clause",
        "jurisdiction",
    ],
    source="tenant.contract-repo",
)

FACT_SET_VIEW = DataView(
    name="facts.internal",
    shape="fields",
    fields=[
        "vendor_name",
        "buyer_name",
        "contract_amount",
        "payment_terms",
        "effective_date",
        "expiration_date",
        "jurisdiction",
    ],
    source="local.contract-extractor",
)

PUBLIC_QUERY_VIEW = DataView(
    name="public.query",
    shape="fields",
    fields=["vendor_id", "vendor_name", "jurisdiction"],
    source="facts.internal",
)

INDUSTRY_VIEW = DataView(
    name="a2a.industry",
    shape="fields",
    fields=["query"],
    source="facts.internal",
)

REVIEW_VIEW = DataView(
    name="review.summary",
    shape="fields",
    fields=[
        "vendor_id",
        "vendor_name",
        "public_summary",
        "industry_context",
        "risk_flags",
    ],
    source="merged",
)


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def _nodes() -> list[NodeSpec]:
    return [
        NodeSpec(
            node_id="ingest_contract",
            name="Ingest contract (from caller)",
            requires_purpose=get_default_purpose(),
            requires_views=[FULL_CONTRACT_VIEW],
            eligible_capability_kinds=[CapabilityKind.TOOL],
            declared_effects=[Effect(kind=EffectKind.READ)],
            fallback_capability_id="local.contract-extractor",  # dev convenience
            timeout_ms=5_000,
        ),
        NodeSpec(
            node_id="extract_facts_local",
            name="Local contract fact extraction",
            requires_purpose=get_default_purpose(),
            requires_views=[FULL_CONTRACT_VIEW],
            eligible_capability_kinds=[CapabilityKind.LOCAL_MODEL],
            declared_effects=[Effect(kind=EffectKind.READ)],
            fallback_capability_id="local.contract-extractor",
            timeout_ms=5_000,
        ),
        NodeSpec(
            node_id="public_research",
            name="Public research on vendor",
            requires_purpose=get_default_purpose(),
            requires_views=[PUBLIC_QUERY_VIEW],
            eligible_capability_kinds=[
                CapabilityKind.PUBLIC_MODEL,
                CapabilityKind.A2A_AGENT,
            ],
            declared_effects=[Effect(kind=EffectKind.READ)],
            fallback_capability_id="a2a.reference-agent",  # one pre-approved Fallback
            timeout_ms=10_000,
        ),
        NodeSpec(
            node_id="merge",
            name="Local deterministic merge",
            requires_purpose=get_default_purpose(),
            requires_views=[REVIEW_VIEW],
            eligible_capability_kinds=[CapabilityKind.LOCAL_MODEL],
            declared_effects=[Effect(kind=EffectKind.READ)],
            fallback_capability_id="local.contract-extractor",
            timeout_ms=5_000,
        ),
        NodeSpec(
            node_id="human_approval",
            name="Human approval (one approval point)",
            requires_purpose=get_default_purpose(),
            requires_views=[REVIEW_VIEW],
            eligible_capability_kinds=[CapabilityKind.HUMAN],
            declared_effects=[Effect(kind=EffectKind.READ)],
            requires_approval=True,
            fallback_capability_id="human",
            timeout_ms=86_400_000,  # up to 24h for human
        ),
        NodeSpec(
            node_id="write_sink",
            name="Write to Mock Procurement Sink",
            requires_purpose=get_default_purpose(),
            requires_views=[REVIEW_VIEW],
            eligible_capability_kinds=[CapabilityKind.SINK],
            declared_effects=[Effect(kind=EffectKind.WRITE, target="mock-procurement")],
            fallback_capability_id="sink.mock-procurement",
            timeout_ms=5_000,
        ),
    ]


def _edges() -> list[EdgeSpec]:
    return [
        EdgeSpec(from_node="ingest_contract", to_node="extract_facts_local"),
        EdgeSpec(from_node="extract_facts_local", to_node="public_research"),
        EdgeSpec(from_node="extract_facts_local", to_node="merge"),
        EdgeSpec(from_node="public_research", to_node="merge"),
        EdgeSpec(from_node="merge", to_node="human_approval"),
        EdgeSpec(from_node="human_approval", to_node="write_sink", when="approved"),
    ]


CONTRACT_REVIEW_TEMPLATE = TaskTemplate(
    template_id="contract-review",
    name="Contract Review (P0 fixed scenario)",
    description=(
        "Review a supplier contract using a Local Model, a public OpenAI-compatible "
        "model, an in-repo A2A Reference Agent, a human approval, and a Mock "
        "Procurement Sink. Topology is sequential with one fan-out and one "
        "approval point, per the P0 Gate."
    ),
    version="0.1.0",
    nodes=_nodes(),
    edges=_edges(),
    required_purposes=[get_default_purpose()],
    max_runtime_ms=120_000,
)


# ---------------------------------------------------------------------------
# Plan construction
# ---------------------------------------------------------------------------


def _build_plan_node(
    template: TaskTemplate, node_id: str, *, capability_id: str, manifest_id: str
) -> PlanNode:
    spec = template.node(node_id)
    return PlanNode(
        node_id=node_id,
        capability_id=capability_id,
        manifest_id=manifest_id,
        purpose=spec.requires_purpose,
        input_views=list(spec.requires_views),
        expected_outputs=[v.name for v in spec.requires_views],
        timeout_ms=spec.timeout_ms,
        fallback_capability_id=spec.fallback_capability_id,
        requires_approval=spec.requires_approval,
    )


def build_contract_review_plan(
    *,
    contract_id: str,
    routing: list[Any],
    capability_bindings: dict[str, str],
    manifest_bindings: dict[str, str],
) -> ExecutionPlan:
    """Construct the ExecutionPlan for a contract review.

    ``capability_bindings`` maps ``node_id`` → ``capability_id`` (chosen
    by the Router). ``manifest_bindings`` is the matching ``manifest_id``
    snapshot.
    """
    nodes = [
        _build_plan_node(
            CONTRACT_REVIEW_TEMPLATE,
            node_id,
            capability_id=capability_bindings[node_id],
            manifest_id=manifest_bindings[node_id],
        )
        for node_id in [
            "ingest_contract",
            "extract_facts_local",
            "public_research",
            "merge",
            "human_approval",
            "write_sink",
        ]
    ]
    edges = [
        PlanEdge(from_node=e.from_node, to_node=e.to_node, when=e.when)
        for e in _edges()
    ]
    return ExecutionPlan(
        contract_id=contract_id,
        template_id=CONTRACT_REVIEW_TEMPLATE.template_id,
        template_version=CONTRACT_REVIEW_TEMPLATE.version,
        nodes=nodes,
        edges=edges,
        routing=routing,
    )
