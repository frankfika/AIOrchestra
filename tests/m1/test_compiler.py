"""M1 CMP-001/002/003 — Trust Compiler test suite."""
from __future__ import annotations

import pytest

from orchestra.compiler import (
    BindingClosureChecker,
    CompileErrorKind,
    DelegationChecker,
    EffectChecker,
    InformationFlowChecker,
    Normalizer,
    Parser,
    PlanSigner,
    Resolver,
    TrustCompiler,
    render_counter_example,
)
from orchestra.compiler.errors import CompileError
from orchestra.compiler.parser import CandidateGraph
from orchestra.core.schema import (
    CapabilityKind,
    DataClassification,
    DataView,
    EdgeSpec,
    Effect,
    EffectKind,
    ExecutionPlan,
    NodeSpec,
    PlanEdge,
    PlanNode,
    Purpose,
    SecurityLabel,
    SourceTrust,
    TaskContract,
    TaskTemplate,
)
from orchestra.registry.bootstrap import load_default_manifests


def _label(c=DataClassification.INTERNAL, r="local"):
    return SecurityLabel(
        classification=c, residency=r, source_trust=SourceTrust.INTERNAL,
        retention_days=30,
    )


def _contract_review_template() -> TaskTemplate:
    return TaskTemplate(
        template_id="contract-review",
        name="Contract Review",
        description="P0 reference scenario",
        version="0.1.0",
        nodes=[
            NodeSpec(
                node_id="ingest",
                name="ingest", requires_purpose=Purpose(code="contract-review"),
                eligible_capability_kinds=[CapabilityKind.LOCAL_MODEL],
                declared_effects=[Effect(kind=EffectKind.READ)],
                timeout_ms=1000,
            ),
            NodeSpec(
                node_id="extract",
                name="extract", requires_purpose=Purpose(code="contract-review"),
                eligible_capability_kinds=[CapabilityKind.LOCAL_MODEL],
                declared_effects=[Effect(kind=EffectKind.READ)],
                fallback_capability_id="local.contract-extractor",
                timeout_ms=1000,
            ),
            NodeSpec(
                node_id="research",
                name="research", requires_purpose=Purpose(code="contract-review"),
                eligible_capability_kinds=[CapabilityKind.PUBLIC_MODEL, CapabilityKind.A2A_AGENT],
                declared_effects=[Effect(kind=EffectKind.READ)],
                fallback_capability_id="a2a.reference-agent",
                timeout_ms=1000,
            ),
            NodeSpec(
                node_id="write_sink",
                name="write_sink", requires_purpose=Purpose(code="contract-review"),
                eligible_capability_kinds=[CapabilityKind.SINK],
                declared_effects=[Effect(kind=EffectKind.WRITE, target="mock-procurement")],
                requires_approval=True,
                fallback_capability_id="sink.mock-procurement",
                timeout_ms=1000,
            ),
        ],
        edges=[
            EdgeSpec(from_node="ingest", to_node="extract"),
            EdgeSpec(from_node="extract", to_node="research"),
            EdgeSpec(from_node="research", to_node="write_sink"),
        ],
        required_purposes=[Purpose(code="contract-review")],
    )


def _edges():
    return [
        EdgeSpec(from_node="ingest", to_node="extract"),
        EdgeSpec(from_node="extract", to_node="research"),
        EdgeSpec(from_node="research", to_node="write_sink"),
    ]


def _contract():
    return TaskContract(
        template_id="contract-review",
        submitted_by="frank",
        purpose=Purpose(code="contract-review"),
        inputs=[DataView(name="contract.full", shape="fields", fields=["vendor_name"])],
    )


# ---------------------------------------------------------------------------
# CMP-001: Parser + Normalizer + TypeChecker
# ---------------------------------------------------------------------------


def test_parser_produces_candidate_graph():
    g = Parser().parse(_contract_review_template(), _contract())
    assert isinstance(g, CandidateGraph)
    assert set(g.nodes.keys()) == {"ingest", "extract", "research", "write_sink"}
    assert len(g.edges) == 3


def test_parser_rejects_template_with_unknown_edge_endpoint():
    bad = TaskTemplate(
        template_id="bad", name="bad", description="", version="0.1.0",
        nodes=[
            NodeSpec(
                node_id="a", name="a", requires_purpose=Purpose(code="x"),
                eligible_capability_kinds=[CapabilityKind.LOCAL_MODEL],
                declared_effects=[Effect(kind=EffectKind.READ)],
            ),
        ],
        edges=[EdgeSpec(from_node="a", to_node="does_not_exist")],
        required_purposes=[Purpose(code="x")],
    )
    with pytest.raises(Exception):
        Parser().parse(bad, _contract())


def test_parser_rejects_cycle():
    cyclic = TaskTemplate(
        template_id="cyc", name="cyc", description="", version="0.1.0",
        nodes=[
            NodeSpec(
                node_id="a", name="a", requires_purpose=Purpose(code="x"),
                eligible_capability_kinds=[CapabilityKind.LOCAL_MODEL],
                declared_effects=[Effect(kind=EffectKind.READ)],
            ),
            NodeSpec(
                node_id="b", name="b", requires_purpose=Purpose(code="x"),
                eligible_capability_kinds=[CapabilityKind.LOCAL_MODEL],
                declared_effects=[Effect(kind=EffectKind.READ)],
            ),
        ],
        edges=[EdgeSpec(from_node="a", to_node="b"), EdgeSpec(from_node="b", to_node="a")],
        required_purposes=[Purpose(code="x")],
    )
    with pytest.raises(Exception):
        Parser().parse(cyclic, _contract())


def test_normalizer_assigns_value_refs_per_edge():
    g = Parser().parse(_contract_review_template(), _contract())
    n = Normalizer().normalize(g)
    # extract's input_refs list contains a ValueRef for ingest.
    extract_refs = n.nodes["extract"].input_refs
    assert any(r.producer_node_id == "ingest" for r in extract_refs)


def test_type_checker_rejects_high_risk_without_approval():
    bad_node = NodeSpec(
        node_id="unsafe_write", name="unsafe", requires_purpose=Purpose(code="x"),
        eligible_capability_kinds=[CapabilityKind.SINK],
        declared_effects=[Effect(kind=EffectKind.WRITE, target="erp")],
        # no requires_approval
    )
    template = TaskTemplate(
        template_id="t", name="t", description="", version="0.1.0",
        nodes=[bad_node],
        edges=[],
        required_purposes=[Purpose(code="x")],
    )
    g = Parser().parse(template, _contract())
    from orchestra.compiler import TypeChecker
    n = Normalizer().normalize(g)
    errs = TypeChecker().check(n)
    assert any(e.kind == CompileErrorKind.HIGH_RISK_EFFECT_WITHOUT_APPROVAL for e in errs)


# ---------------------------------------------------------------------------
# CMP-002: Information Flow + Effect + Delegation checkers
# ---------------------------------------------------------------------------


def test_info_flow_blocks_restricted_to_public_outside_public_research():
    template = _contract_review_template()
    g = Parser().parse(template, _contract())
    n = Normalizer().normalize(g)
    # Bind the research node to public.openai-compat (a public-model).
    # The input data is RESTRICTED. The Info Flow checker must deny.
    manifests = {m.capability_id: m for m in load_default_manifests().all()}
    bindings = {
        "ingest": "local.contract-extractor",
        "extract": "local.contract-extractor",
        "research": "public.openai-compat",
        "write_sink": "sink.mock-procurement",
    }
    restricted = SecurityLabel(
        classification=DataClassification.RESTRICTED, residency="local",
        source_trust=SourceTrust.INTERNAL, retention_days=365,
    )
    errs = InformationFlowChecker(manifests).check(n, restricted, bindings)
    assert any(e.kind == CompileErrorKind.RESTRICTED_TO_PUBLIC for e in errs)


def test_info_flow_allows_public_research_node_to_receive_restricted():
    """The dedicated public_research node in the P0 reference
    template can receive internal data (per the P0 PDP rule).
    """
    from orchestra.compiler.parser import CandidateNode
    template = _contract_review_template()
    g = Parser().parse(template, _contract())
    # Build a renamed graph directly.
    new_nodes = {}
    for old_id, n in g.nodes.items():
        new_id = "public_research" if n.node_id == "research" else n.node_id
        new_nodes[new_id] = CandidateNode(
            node_id=new_id,
            name=n.name,
            purpose_code=n.purpose_code,
            requires_purpose=n.requires_purpose,
            requires_views=n.requires_views,
            eligible_capability_kinds=n.eligible_capability_kinds,
            declared_effect_kinds=n.declared_effect_kinds,
            requires_approval=n.requires_approval,
            fallback_capability_id=n.fallback_capability_id,
            timeout_ms=n.timeout_ms,
        )
    from orchestra.compiler.parser import CandidateEdge, CandidateGraph
    new_edges = [
        CandidateEdge(
            from_node="public_research" if e.from_node == "research" else e.from_node,
            to_node="public_research" if e.to_node == "research" else e.to_node,
            when=e.when,
        )
        for e in g.edges
    ]
    g2 = CandidateGraph(
        template_id=g.template_id,
        template_version=g.template_version,
        nodes=new_nodes,
        edges=new_edges,
        inputs=g.inputs,
        purpose=g.purpose,
    )
    n = Normalizer().normalize(g2)
    manifests = {m.capability_id: m for m in load_default_manifests().all()}
    bindings = {
        "ingest": "local.contract-extractor",
        "extract": "local.contract-extractor",
        "public_research": "public.openai-compat",
        "write_sink": "sink.mock-procurement",
    }
    restricted = SecurityLabel(
        classification=DataClassification.RESTRICTED, residency="local",
        source_trust=SourceTrust.INTERNAL, retention_days=365,
    )
    errs = InformationFlowChecker(manifests).check(n, restricted, bindings)
    assert not any(e.kind == CompileErrorKind.RESTRICTED_TO_PUBLIC for e in errs)


def test_effect_checker_rejects_capability_with_undeclared_effect():
    template = _contract_review_template()
    g = Parser().parse(template, _contract())
    n = Normalizer().normalize(g)
    manifests = {m.capability_id: m for m in load_default_manifests().all()}
    # Bind the local extractor (which only declares READ) to the
    # write_sink node (which declares WRITE). The effect checker
    # flags this as escalation.
    bindings = {
        "ingest": "local.contract-extractor",
        "extract": "local.contract-extractor",
        "research": "a2a.reference-agent",
        "write_sink": "local.contract-extractor",  # wrong: only declares READ
    }
    errs = EffectChecker(manifests).check(n, bindings)
    assert any(e.kind == CompileErrorKind.EFFECT_ESCALATION for e in errs)


def test_delegation_checker_rejects_purpose_mismatch():
    bad_purpose_node = NodeSpec(
        node_id="rogue", name="rogue", requires_purpose=Purpose(code="rogue"),
        eligible_capability_kinds=[CapabilityKind.LOCAL_MODEL],
        declared_effects=[Effect(kind=EffectKind.READ)],
    )
    template = TaskTemplate(
        template_id="t", name="t", description="", version="0.1.0",
        nodes=[bad_purpose_node],
        edges=[],
        # The template allows the rogue purpose. We test the
        # DelegationChecker directly with a different task purpose.
        required_purposes=[Purpose(code="rogue"), Purpose(code="contract-review")],
    )
    g = Parser().parse(template, _contract())
    n = Normalizer().normalize(g)
    manifests = {m.capability_id: m for m in load_default_manifests().all()}
    bindings = {"rogue": "local.contract-extractor"}
    errs = DelegationChecker(manifests).check(
        n, "contract-review", bindings
    )
    assert any(e.kind == CompileErrorKind.PURPOSE_ESCALATION for e in errs)


# ---------------------------------------------------------------------------
# CMP-003: Counter-example rendering
# ---------------------------------------------------------------------------


def test_counter_example_renderer_is_one_line():
    err = CompileError(
        kind=CompileErrorKind.RESTRICTED_TO_PUBLIC,
        node_id="public_research",
        reason="data classified restricted would flow to public Adapter",
        data_path=["extract", "public_research", "public.openai-compat"],
    )
    s = render_counter_example(err)
    assert s.startswith("invariant 1 (")
    assert "extract -> public_research -> public.openai-compat" in s


# ---------------------------------------------------------------------------
# RSL-001: Resolver
# ---------------------------------------------------------------------------


def test_resolver_picks_capability_for_every_node():
    template = _contract_review_template()
    g = Parser().parse(template, _contract())
    n = Normalizer().normalize(g)
    r = Resolver().resolve(n, _label(), "local", 1.0, template=template)
    assert r.ok, f"errors: {[e.reason for e in r.errors]}"
    assert set(r.bindings.keys()) == {"ingest", "extract", "research", "write_sink"}


def test_resolver_amend_re_resolves_unavailable_capability():
    template = _contract_review_template()
    g = Parser().parse(template, _contract())
    n = Normalizer().normalize(g)
    r1 = Resolver().resolve(n, _label(), "local", 1.0, template=template)
    # Pretend the chosen public Adapter becomes unavailable.
    unavailable = r1.bindings.get("research")
    assert unavailable is not None
    r2 = Resolver().amend(r1, unavailable, n, _label(), "local", 1.0, template=template)
    # The amendment must not lose the write_sink binding.
    assert "write_sink" in r2.bindings
    # The new research binding may be the fallback (A2A).
    assert r2.bindings["research"] in {"public.openai-compat", "a2a.reference-agent"}


# ---------------------------------------------------------------------------
# BND-001: Binding Closure + Plan Signer
# ---------------------------------------------------------------------------


def test_binding_closure_rejects_missing_binding():
    template = _contract_review_template()
    g = Parser().parse(template, _contract())
    n = Normalizer().normalize(g)
    # Drop the write_sink binding.
    manifests = {m.capability_id: m for m in load_default_manifests().all()}
    partial = {
        "ingest": "local.contract-extractor",
        "extract": "local.contract-extractor",
        "research": "a2a.reference-agent",
    }
    manifest_bindings = {
        "ingest": "manifest:local.contract-extractor",
        "extract": "manifest:local.contract-extractor",
        "research": "manifest:a2a.reference-agent",
    }
    result = BindingClosureChecker(manifests).check(n, partial, manifest_bindings)
    assert not result.ok
    assert any(e.kind == CompileErrorKind.INFEASIBLE_BINDING for e in result.errors)


def test_plan_signer_round_trip():
    signer = PlanSigner(b"k" * 32, kid="p1-test")
    plan = ExecutionPlan(
        contract_id="c1", template_id="t", template_version="0.1.0",
        nodes=[], edges=[],
    )
    signed = signer.sign(plan)
    assert signed.signature is not None
    assert signer.verify(signed)
    # Tamper with a field; verification must fail.
    bad = signed.model_copy(update={"template_version": "0.2.0"})
    assert not signer.verify(bad)


# ---------------------------------------------------------------------------
# End-to-end: TrustCompiler + Resolver + Plan Signer produce a signed Plan
# ---------------------------------------------------------------------------


def test_end_to_end_compile_sign_contract_review():
    from orchestra.templates.contract_review import CONTRACT_REVIEW_TEMPLATE, build_contract_review_plan

    template = CONTRACT_REVIEW_TEMPLATE
    g = Parser().parse(template, _contract())
    n = Normalizer().normalize(g)
    resolver = Resolver()
    rsl = resolver.resolve(n, _label(), "local", 1.0)
    assert rsl.ok, [e.reason for e in rsl.errors]
    manifests = {m.capability_id: m for m in load_default_manifests().all()}
    tc = TrustCompiler(manifests)
    cr = tc.compile(template, _contract(), _label(), rsl.bindings)
    assert cr.ok, [e.reason for e in cr.errors]
    closure = BindingClosureChecker(manifests).check(
        n, rsl.bindings, rsl.manifest_bindings
    )
    assert closure.ok
    plan = build_contract_review_plan(
        contract_id="c1", routing=[],  # not used at compile time
        capability_bindings=rsl.bindings,
        manifest_bindings=rsl.manifest_bindings,
    )
    signed = PlanSigner(b"k" * 32).sign(plan)
    assert signed.signature is not None


# ---------------------------------------------------------------------------
# OPA fail-closed
# ---------------------------------------------------------------------------


def test_opa_unavailable_raises():
    from orchestra.opa import OpaConfig, OpaHttpClient, OPAUnavailable
    from orchestra.registry.policy import PolicyRequest

    # OPA at a port nothing is listening on
    client = OpaHttpClient(OpaConfig(base_url="http://127.0.0.1:1", timeout_seconds=0.2))
    node = NodeSpec(
        node_id="n", name="n", requires_purpose=Purpose(code="x"),
        eligible_capability_kinds=[CapabilityKind.LOCAL_MODEL],
        declared_effects=[Effect(kind=EffectKind.READ)],
    )
    cap = load_default_manifests().get("local.contract-extractor")
    req = PolicyRequest(
        node=node, capability=cap, data_label=_label(),
        purpose_code="x", region="local", budget_remaining_usd=1.0,
    )
    with pytest.raises(OPAUnavailable):
        client.decide(req)


def test_in_process_pdp_matches_old_engine():
    from orchestra.opa import InProcessPDP
    from orchestra.registry.policy import PolicyRequest

    pdp = InProcessPDP()
    node = NodeSpec(
        node_id="n", name="n", requires_purpose=Purpose(code="x"),
        eligible_capability_kinds=[CapabilityKind.LOCAL_MODEL],
        declared_effects=[Effect(kind=EffectKind.READ)],
    )
    cap = load_default_manifests().get("local.contract-extractor")
    req = PolicyRequest(
        node=node, capability=cap, data_label=_label(),
        purpose_code="x", region="local", budget_remaining_usd=1.0,
    )
    d = pdp.decide(req)
    assert d.allow is True
