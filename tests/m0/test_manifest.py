"""M0.2 — Capability Manifest content-addressed snapshot + round-trip (SPEC-002)."""
from __future__ import annotations

import json

from orchestra.core.schema import (
    CapabilityKind,
    CapabilityManifest,
    DataClassification,
    Effect,
    EffectKind,
    IntegrationLevel,
    SecurityLabel,
    SourceTrust,
)


def _m(name: str = "m1", version: str = "0.1.0") -> CapabilityManifest:
    return CapabilityManifest(
        capability_id=name,
        name=name,
        kind=CapabilityKind.LOCAL_MODEL,
        endpoint="http://127.0.0.1:8101/v1/extract",
        integration_level=IntegrationLevel.ENFORCE,
        version=version,
        declared_effects=[Effect(kind=EffectKind.READ)],
    )


def test_manifest_id_is_content_addressed_and_stable():
    m1 = _m()
    m1_id = m1.manifest_id()
    # regenerate from the same body
    m2 = CapabilityManifest.model_validate_json(m1.model_dump_json())
    assert m2.manifest_id() == m1_id
    # a different body yields a different id
    m3 = _m(name="m2")
    assert m3.manifest_id() != m1_id


def test_manifest_id_changes_on_version_bump():
    a = _m(version="0.1.0").manifest_id()
    b = _m(version="0.1.1").manifest_id()
    assert a != b


def test_manifest_serialises_to_canonical_json():
    m = _m()
    raw = m.model_dump_json()
    # round-trip
    again = CapabilityManifest.model_validate_json(raw)
    assert again.capability_id == m.capability_id
    assert again.endpoint == m.endpoint


def test_manifest_id_format_is_prefix_12hex():
    mid = _m().manifest_id()
    assert mid.startswith("manifest:")
    assert len(mid.split(":", 1)[1]) == 12
    int(mid.split(":", 1)[1], 16)  # is hex
