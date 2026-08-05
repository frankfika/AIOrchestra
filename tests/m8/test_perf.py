"""M8 — Performance: EgressPEP + Ingress overhead in a hot loop.

The dev path is not production. But we can prove the per-call
overhead is sub-millisecond for the projection + token-verify
path, which is the M3/M5 hot loop. The test prints the
throughput and p99 latency; it does NOT enforce a hard
threshold (a real production swap may use a slower KMS / OIDC
verifier and the budget changes).

Run with ``pytest tests/m8/test_perf.py -v -s`` to see the
printout; the test passes as long as the loop completes and
the per-call cost is below a generous sanity bound (1 ms
for projection, 1 ms for token verify).
"""
from __future__ import annotations

import statistics
import time

import pytest

from orchestra.core.hashing import hmac_keygen
from orchestra.core.schema import FieldManifest
from orchestra.publishing.card import AgentCard
from orchestra.publishing.ingress import Ingress
from orchestra.publishing.registry import PublishedRegistry
from orchestra.xfr.egress_pep import EgressPEP
from orchestra.xfr.projector import FieldProjector
from orchestra.registry.bootstrap import load_default_field_manifests, make_egress_manifest_lookup


def _hot_path_payload():
    return {
        "facts": {
            "vendor_name": "Acme Cloud Logistics Co., Ltd.",
            "jurisdiction": "HK",
            "amount_bucket": "8.6M",
            "topic": "Termination notice",
            "extra": "x" * 200,
        },
        "query": "industry classification; public registry lookup; " + ("y" * 100),
    }


def test_egress_pep_projection_throughput():
    """Project 5,000 payloads through the M3 EgressPEP. Print
    the per-call cost so a future regression is visible."""
    manifest = load_default_field_manifests()[("public.openai-compat", "public-research")]
    pep = EgressPEP(manifest_lookup=make_egress_manifest_lookup())
    payload = _hot_path_payload()
    # Warm up the projector's import path.
    pep.project(capability_id="public.openai-compat", view_name="public-research", payload=payload)
    n = 5000
    t0 = time.perf_counter()
    for _ in range(n):
        pep.project(capability_id="public.openai-compat", view_name="public-research", payload=payload)
    elapsed = time.perf_counter() - t0
    per_call_us = (elapsed / n) * 1_000_000
    print(f"\n[EgressPEP] {n} calls in {elapsed*1000:.2f}ms -> {per_call_us:.2f} µs/call")
    # Sanity bound: a full PEP round-trip on a modern CPU should
    # be sub-millisecond. We allow 5 ms (a real production swap
    # may add KMS / OIDC verify overhead).
    assert per_call_us < 5_000, f"EgressPEP too slow: {per_call_us:.2f} µs/call"


def test_ingress_token_verify_throughput():
    """Mint a token once, then verify it 1,000 times. Print
    the per-call cost."""
    key = hmac_keygen()
    registry = PublishedRegistry(default_key=key, default_kid="key-perf")
    card = AgentCard(
        capability_id="perf.test", name="Perf", version="0.1.0",
        partner_id="partner-perf", partner_contract_id="contract-perf",
        audiences=["partner-perf-api"],
        contract_snapshot={"audiences": [{"audience_id": "partner-perf-api", "required_scopes": []}]},
    )
    registry.publish(card, key=key, kid="key-perf")
    ingress = Ingress(registry, token_key=key)
    token = ingress.issue_token(
        issuer="partner-perf-idp", subject="user-1",
        audience="partner-perf-api", scopes=[],
    )
    # Warm up.
    ingress.verify_token(token)
    n = 1000
    t0 = time.perf_counter()
    for _ in range(n):
        ingress.verify_token(token)
    elapsed = time.perf_counter() - t0
    per_call_us = (elapsed / n) * 1_000_000
    print(f"\n[Ingress.verify_token] {n} calls in {elapsed*1000:.2f}ms -> {per_call_us:.2f} µs/call")
    # Sanity bound: HMAC verify is dominated by the base64 +
    # JSON decode; should be well under 1 ms.
    assert per_call_us < 1_000, f"verify_token too slow: {per_call_us:.2f} µs/call"


def test_field_projector_pure_projection_throughput():
    """Project a payload 10,000 times through the bare
    FieldProjector (no PEP, no EgressPEP). This is the lower
    bound on the M3 hot path."""
    manifest = FieldManifest(
        name="perf",
        source_view="view:perf",
        allowed_fields=["facts", "query"],
        byte_budget=8 * 1024,
    )
    p = FieldProjector()
    payload = _hot_path_payload()
    n = 10_000
    t0 = time.perf_counter()
    for _ in range(n):
        p.project(manifest, payload)
    elapsed = time.perf_counter() - t0
    per_call_us = (elapsed / n) * 1_000_000
    print(f"\n[FieldProjector] {n} calls in {elapsed*1000:.2f}ms -> {per_call_us:.2f} µs/call")
    assert per_call_us < 1_000, f"FieldProjector too slow: {per_call_us:.2f} µs/call"


def test_release_gate_throughput():
    """Run the Release Gate over a CitationManifest 5,000 times.
    The gate is the M5 hot path on the partner response."""
    from orchestra.core.schema import Citation, CitationManifest, CitationSourceRef
    from orchestra.core.schema import DataClassification, SecurityLabel, SourceTrust
    from orchestra.publishing.card import AgentCard
    from orchestra.publishing.release_gate import ReleaseGate

    card = AgentCard(
        capability_id="perf", name="Perf", version="0.1.0",
        partner_id="p", partner_contract_id="c",
        audiences=["p", "partner"],
    )
    gate = ReleaseGate(card=card)
    manifest = CitationManifest(
        task_run_id="trun-perf",
        citations=[
            Citation(
                claim="x", sources=[CitationSourceRef(
                    kind="synthetic", ref="s",
                    label=SecurityLabel(
                        classification=DataClassification.PUBLIC,
                        residency="public", source_trust=SourceTrust.PUBLIC,
                    ),
                )],
                audience="partner", release_class="attested",
            ),
        ],
    )
    result = {"claims": ["x"]}
    n = 5000
    t0 = time.perf_counter()
    for _ in range(n):
        gate.release(result, manifest)
    elapsed = time.perf_counter() - t0
    per_call_us = (elapsed / n) * 1_000_000
    print(f"\n[ReleaseGate] {n} calls in {elapsed*1000:.2f}ms -> {per_call_us:.2f} µs/call")
    assert per_call_us < 1_000, f"ReleaseGate too slow: {per_call_us:.2f} µs/call"
