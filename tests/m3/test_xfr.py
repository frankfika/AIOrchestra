"""M3 XFR-001 — Schema Projection + Egress PEP tests.

The XFR-001 invariant: anything leaving the tenant goes through
:func:`FieldProjector.project` with a :class:`FieldManifest`, and the
audit timeline records only the projected digest, never the raw payload.
"""
from __future__ import annotations

import json

import pytest

from orchestra.core.ids import digest_json
from orchestra.core.schema import EventKind, FieldManifest
from orchestra.registry.bootstrap import (
    load_default_field_manifests,
    make_egress_manifest_lookup,
)
from orchestra.xfr.egress_pep import EgressDenied, EgressPEP
from orchestra.xfr.projector import EgressBudgetExceeded, FieldProjector


# ---------------------------------------------------------------------------
# FieldProjector
# ---------------------------------------------------------------------------


def _payload() -> dict:
    return {
        "facts": {"vendor_name": "Acme Cloud Logistics Co., Ltd.", "jurisdiction": "HK", "amount_bucket": "8.6M", "topic": "Termination notice"},
        "query": "industry classification",
        "secret_token": "super-secret-do-not-leak",
        "internal_id": "INC-9000",
    }


def test_field_projector_is_deterministic():
    """Same input + same manifest -> byte-identical output, twice."""
    manifest = load_default_field_manifests()[("public.openai-compat", "public-research")]
    p = FieldProjector()
    a = p.project(manifest, _payload())
    b = p.project(manifest, _payload())
    assert a.projected == b.projected
    assert a.dropped_fields == b.dropped_fields
    assert a.digest == b.digest
    assert a.projected_bytes == b.projected_bytes
    # Two runs of the digest_json helper should match too.
    assert a.digest == digest_json(a.projected)


def test_field_projector_drops_unlisted_fields():
    manifest = load_default_field_manifests()[("public.openai-compat", "public-research")]
    p = FieldProjector()
    result = p.project(manifest, _payload())
    # secret_token and internal_id are NOT in allowed_fields, must be dropped.
    assert "secret_token" not in result.projected
    assert "internal_id" not in result.projected
    assert "secret_token" in result.dropped_fields
    assert "internal_id" in result.dropped_fields
    # Allowed fields must be present.
    assert "facts" in result.projected
    assert "query" in result.projected
    # Dropped list is sorted for determinism.
    assert result.dropped_fields == sorted(result.dropped_fields)


def test_field_projector_redaction_partial_and_tokenize():
    """partial-N truncates with ellipsis; tokenize replaces with a tag."""
    manifest = FieldManifest(
        name="redaction-test",
        source_view="view:test",
        allowed_fields=["vendor_name", "amount_bucket"],
        redaction_rules=[
            {"field": "vendor_name", "op": "partial-6"},
            {"field": "amount_bucket", "op": "tokenize"},
        ],
    )
    result = FieldProjector().project(manifest, {"vendor_name": "Acme Cloud Logistics", "amount_bucket": "8.6M"})
    # partial-6 -> first 6 chars + "…"
    assert result.projected["vendor_name"] == "Acme C…"
    # tokenize -> <token:XXXXXXXX> 8-char digest prefix
    assert result.projected["amount_bucket"].startswith("<token:")
    assert result.projected["amount_bucket"].endswith(">")


def test_field_projector_redaction_hash_is_digest():
    manifest = FieldManifest(
        name="hash-test",
        source_view="view:test",
        allowed_fields=["vendor_name"],
        redaction_rules=[{"field": "vendor_name", "op": "hash"}],
    )
    payload = {"vendor_name": "Acme"}
    result = FieldProjector().project(manifest, payload)
    assert result.projected["vendor_name"] == digest_json("Acme")


def test_field_projector_byte_budget_raises():
    """A payload larger than the manifest's byte_budget is refused."""
    manifest = FieldManifest(
        name="budget-test",
        source_view="view:test",
        allowed_fields=["a", "b"],
        byte_budget=10,
    )
    p = FieldProjector()
    with pytest.raises(EgressBudgetExceeded) as ei:
        p.project(manifest, {"a": "x" * 100, "b": "y" * 100})
    assert "byte_budget" in str(ei.value)


def test_field_projector_no_byte_budget_allows_anything():
    manifest = FieldManifest(
        name="no-budget",
        source_view="view:test",
        allowed_fields=["a"],
    )
    result = FieldProjector().project(manifest, {"a": "x" * 10_000})
    assert result.projected_bytes > 10_000


def test_field_projector_rejects_non_dict_payload():
    manifest = FieldManifest(
        name="non-dict",
        source_view="view:test",
        allowed_fields=["a"],
    )
    with pytest.raises(Exception):
        FieldProjector().project(manifest, "not a dict")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# EgressPEP
# ---------------------------------------------------------------------------


def test_egress_pep_denies_when_no_manifest():
    """No manifest for the (capability, view) -> EgressDenied."""
    pep = EgressPEP(manifest_lookup=make_egress_manifest_lookup())
    with pytest.raises(EgressDenied) as ei:
        pep.project(capability_id="public.openai-compat", view_name="does-not-exist", payload={})
    assert "no FieldManifest" in str(ei.value)


def test_egress_pep_projects_via_manifest():
    pep = EgressPEP(manifest_lookup=make_egress_manifest_lookup())
    projected, manifest_dict = pep.project(
        capability_id="public.openai-compat",
        view_name="public-research",
        payload=_payload(),
    )
    # Secret fields must not appear.
    assert "secret_token" not in projected
    assert "internal_id" not in projected
    # Allowed fields appear.
    assert {"facts", "query"} <= projected.keys()
    # Manifest dict roundtrips the original config.
    assert manifest_dict["allowed_fields"] == ["facts", "query"]
    assert manifest_dict["byte_budget"] == 8 * 1024


def test_egress_pep_byte_budget_overrun_denies():
    """Byte budget overrun surfaces as EgressDenied (not EgressBudgetExceeded)
    so the Coordinator can attribute the failure to the PEP, not the
    Projector layer."""
    lookup = make_egress_manifest_lookup(overrides={
        ("public.openai-compat", "tight"): FieldManifest(
            name="tight", source_view="view:t", allowed_fields=["a"], byte_budget=5,
        ),
    })
    pep = EgressPEP(manifest_lookup=lookup)
    with pytest.raises(EgressDenied) as ei:
        pep.project(capability_id="public.openai-compat", view_name="tight", payload={"a": "x" * 100})
    assert "byte_budget" in str(ei.value).lower() or "5" in str(ei.value)


def test_egress_pep_audit_event_carries_digest_not_payload():
    """The io.sent event must record projected_digest + dropped_fields,
    but NEVER the raw payload values."""
    pep = EgressPEP(manifest_lookup=make_egress_manifest_lookup())
    payload = _payload()
    projected, manifest_dict = pep.project(
        capability_id="public.openai-compat",
        view_name="public-research",
        payload=payload,
    )
    event = pep.audit_event(
        task_run_id="trun-test",
        node_run_id="nrun-test",
        capability_id="public.openai-compat",
        view_name="public-research",
        projected=projected,
        manifest=manifest_dict,
        dropped=["secret_token", "internal_id"],
        projected_bytes=120,
    )
    assert event.kind == EventKind.IO_SENT
    payload_dump = event.payload
    # Mandatory fields
    assert payload_dump["capability_id"] == "public.openai-compat"
    assert payload_dump["view_name"] == "public-research"
    assert payload_dump["manifest_id"] == manifest_dict["manifest_id"]
    assert payload_dump["projected_bytes"] == 120
    # The digest matches the projector digest (NOT the raw payload digest).
    assert payload_dump["projected_digest"] == digest_json(projected)
    assert payload_dump["projected_digest"] != digest_json(payload)
    # Dropped fields are listed (so the audit explains what was filtered).
    assert payload_dump["dropped_fields"] == ["secret_token", "internal_id"]
    # Negative assertion: the secret value must not be in the event payload.
    serialised = json.dumps(event.payload, sort_keys=True)
    assert "super-secret-do-not-leak" not in serialised
    assert "INC-9000" not in serialised
    # Only declared field names from the manifest may appear in the
    # event payload as data, never their values.
    declared_event_fields = {
        "node_id", "capability_id", "view_name", "manifest_id",
        "projected_digest", "projected_bytes", "dropped_fields",
    }
    assert set(event.payload.keys()) <= declared_event_fields


def test_egress_pep_overrides_layer_on_top_of_default():
    """make_egress_manifest_lookup(overrides=...) lets tests pin a
    specific manifest while keeping the rest of the bootstrap."""
    custom = FieldManifest(
        name="custom-view",
        source_view="view:test",
        allowed_fields=["x"],
        byte_budget=64,
    )
    lookup = make_egress_manifest_lookup(overrides={
        ("public.openai-compat", "custom"): custom,
    })
    pep = EgressPEP(manifest_lookup=lookup)
    # The override works.
    projected, m = pep.project(
        capability_id="public.openai-compat", view_name="custom", payload={"x": "abcd"}
    )
    # Projector sorts keys before serialising, so {"x":"abcd"} -> 12 bytes.
    assert projected == {"x": "abcd"}
    assert m["name"] == "custom-view"
    assert m["byte_budget"] == 64
    # The default table is still served.
    assert lookup("public.openai-compat", "public-research") is not None
