"""Default P0 manifests and policy bundle.

These are the *exact* artefacts the Contract Review demo runs against. They
are kept in Python (instead of YAML) so the demo cannot accidentally drift
between an on-disk file and what tests expect.
"""
from __future__ import annotations

from collections.abc import Callable

from orchestra.core.schema import (
    CapabilityKind,
    CapabilityManifest,
    DataClassification,
    Effect,
    EffectKind,
    FieldManifest,
    IntegrationLevel,
    SecurityLabel,
    SourceTrust,
)
from orchestra.registry.manifest_store import ManifestStore
from orchestra.registry.policy import PolicyEngine, default_p0_rules


def _default_security_labels() -> tuple[SecurityLabel, SecurityLabel, SecurityLabel]:
    return (
        SecurityLabel(
            classification=DataClassification.PUBLIC,
            residency="public",
            source_trust=SourceTrust.PUBLIC,
            retention_days=7,
            owner="tenant:demo",
        ),
        SecurityLabel(
            classification=DataClassification.INTERNAL,
            residency="local",
            source_trust=SourceTrust.INTERNAL,
            retention_days=30,
            owner="tenant:demo",
        ),
        SecurityLabel(
            classification=DataClassification.RESTRICTED,
            residency="local",
            source_trust=SourceTrust.INTERNAL,
            retention_days=365,
            owner="tenant:demo",
        ),
    )


def load_default_manifests(endpoints: dict[str, str] | None = None) -> ManifestStore:
    """The three reference adapters + a Mock Procurement Sink.

    ``endpoints`` is an optional override map keyed by capability_id, used
    by the demo to pin the manifest endpoints to the ports the
    ``servers.start_all_servers`` helper chose.
    """
    endpoints = endpoints or {}
    public_label, internal_label, restricted_label = _default_security_labels()

    def _ep(cap_id: str, default: str) -> str:
        return endpoints.get(cap_id, default)

    manifests = [
        CapabilityManifest(
            capability_id="local.contract-extractor",
            name="Local Contract Fact Extractor",
            kind=CapabilityKind.LOCAL_MODEL,
            endpoint=_ep("local.contract-extractor", "http://127.0.0.1:8101/v1/extract"),
            integration_level=IntegrationLevel.ENFORCE,
            accepts_labels=[restricted_label, internal_label],
            produces_labels=[internal_label],
            declared_effects=[Effect(kind=EffectKind.READ)],
            cost_estimate_usd=0.0,
            p50_latency_ms=200,
            p95_latency_ms=600,
            tags={"model": "deterministic-extractor-v1", "in_repo": "true"},
        ),
        CapabilityManifest(
            capability_id="public.openai-compat",
            name="Public OpenAI-Compatible Research",
            kind=CapabilityKind.PUBLIC_MODEL,
            endpoint=_ep(
                "public.openai-compat", "http://127.0.0.1:8102/v1/chat/completions"
            ),
            integration_level=IntegrationLevel.ENFORCE,
            accepts_labels=[public_label],
            produces_labels=[public_label],
            declared_effects=[Effect(kind=EffectKind.READ)],
            cost_estimate_usd=0.002,
            p50_latency_ms=1500,
            p95_latency_ms=3500,
            tags={"model": "demo-openai-compat", "max_tokens": "256"},
            egress_view_name="public-research",
        ),
        CapabilityManifest(
            capability_id="a2a.reference-agent",
            name="In-Repo A2A Reference Agent",
            kind=CapabilityKind.A2A_AGENT,
            endpoint=_ep("a2a.reference-agent", "http://127.0.0.1:8103/a2a/v1"),
            integration_level=IntegrationLevel.ENFORCE,
            accepts_labels=[public_label],
            produces_labels=[public_label],
            declared_effects=[Effect(kind=EffectKind.READ)],
            cost_estimate_usd=0.001,
            p50_latency_ms=900,
            p95_latency_ms=2500,
            tags={"agent_card": "/.well-known/agent.json"},
            egress_view_name="a2a-reference",
        ),
        CapabilityManifest(
            capability_id="sink.mock-procurement",
            name="Mock Procurement Sink",
            kind=CapabilityKind.SINK,
            endpoint=_ep("sink.mock-procurement", "http://127.0.0.1:8104/sink"),
            integration_level=IntegrationLevel.ENFORCE,
            accepts_labels=[internal_label, public_label],
            produces_labels=[public_label],
            declared_effects=[Effect(kind=EffectKind.WRITE, target="mock-procurement")],
            cost_estimate_usd=0.0,
            p50_latency_ms=50,
            p95_latency_ms=150,
            tags={"synthetic_only": "true"},
        ),
        CapabilityManifest(
            capability_id="tool.contract-passthrough",
            name="Contract passthrough tool (in-process)",
            kind=CapabilityKind.TOOL,
            endpoint="inproc:contract-passthrough",
            integration_level=IntegrationLevel.ENFORCE,
            accepts_labels=[restricted_label, internal_label, public_label],
            produces_labels=[internal_label],
            declared_effects=[Effect(kind=EffectKind.READ)],
            cost_estimate_usd=0.0,
            p50_latency_ms=5,
            p95_latency_ms=20,
            tags={"purpose": "lift the contract text from initial_inputs into the node_results bus"},
        ),
    ]
    return ManifestStore(manifests)


def load_default_policy() -> PolicyEngine:
    return PolicyEngine(default_p0_rules())


# ---------------------------------------------------------------------------
# M3 XFR-001 — default FieldManifests
# ---------------------------------------------------------------------------


def load_default_field_manifests() -> dict[tuple[str, str], FieldManifest]:
    """Return a lookup table of ``(capability_id, view_name) -> FieldManifest``.

    The M3 Egress PEP calls this lookup at run time. A capability that
    does not appear here cannot be reached — the PEP raises
    :class:`EgressDenied`, which the Coordinator surfaces as
    "no manifest published for this egress view".

    The :class:`FieldManifest` operates on the **top-level input keys**
    the Coordinator hands to the Adapter. For ``public.openai-compat``
    the Adapter expects ``{"facts": {...}, "query": "..."}`` — the
    manifest declares these as the only allowed top-level fields. The
    inner ``facts`` dict is already a deterministic, restricted
    extraction produced by the local extractor, so the manifest does
    not need to re-redact its contents.
    """
    public_research_view = FieldManifest(
        name="public-research",
        source_view="view:public-research",
        allowed_fields=["facts", "query"],
        byte_budget=8 * 1024,
    )
    public_research_raw = FieldManifest(
        name="public-research-raw",
        source_view="view:public-research",
        allowed_fields=["topic", "jurisdiction"],
        byte_budget=128,
    )
    a2a_reference_view = FieldManifest(
        name="a2a-reference",
        source_view="view:a2a-reference",
        allowed_fields=["facts", "query"],
        byte_budget=8 * 1024,
    )
    return {
        ("public.openai-compat", "public-research"): public_research_view,
        ("public.openai-compat", "public-research-raw"): public_research_raw,
        ("a2a.reference-agent", "a2a-reference"): a2a_reference_view,
    }


def make_egress_manifest_lookup(
    overrides: dict[tuple[str, str], FieldManifest] | None = None,
) -> Callable[[str, str], FieldManifest | None]:
    """Build a ``(capability_id, view_name) -> FieldManifest`` lookup.

    The base table is :func:`load_default_field_manifests`; ``overrides``
    are layered on top and are intended for tests.
    """
    base = load_default_field_manifests()
    if overrides:
        base.update(overrides)

    def _lookup(capability_id: str, view_name: str) -> FieldManifest | None:
        return base.get((capability_id, view_name))

    return _lookup
