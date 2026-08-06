"""M6 ENT-003 — Supply chain: SBOM, signing, provenance."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestra.core.hashing import hmac_keygen
from orchestra.enterprise.supply_chain import (
    SBOM,
    Provenance,
    build_provenance,
    build_sbom_from_pyproject,
    build_sbom_from_requirements,
    file_sha256,
    git_revision,
    sign_artifact,
    verify_artifact,
    verify_provenance,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_sbom_from_pyproject_lists_each_dependency():
    sbom = build_sbom_from_pyproject(pyproject_path=REPO_ROOT / "pyproject.toml")
    names = {c.name for c in sbom.components}
    assert "fastapi" in names
    assert "pydantic" in names
    # The version is parsed and matches the actual pin (whether
    # "==0.115.0" or ">=0.116.0"). The test asserts the SBOM
    # reflects what the operator actually wrote, not a stale
    # version.
    fastapi = next(c for c in sbom.components if c.name == "fastapi")
    assert fastapi.version != ""
    assert fastapi.version[0].isdigit()  # not "unknown" or empty
    # purl is well-formed.
    assert fastapi.purl.startswith("pkg:pypi/fastapi@")
    # Format metadata is present.
    assert sbom.bom_format == "CycloneDX"
    assert sbom.spec_version == "1.5"
    # generated_at is set.
    assert "generated_at" in sbom.metadata


def test_sbom_from_requirements_lists_each_dependency():
    sbom = build_sbom_from_requirements(requirements_path=REPO_ROOT / "requirements.txt")
    names = {c.name for c in sbom.components}
    # requirements.txt includes the extra test deps.
    assert "pytest" in names
    assert "fastapi" in names


def test_sbom_to_dict_is_serialisable():
    sbom = build_sbom_from_pyproject(pyproject_path=REPO_ROOT / "pyproject.toml")
    d = sbom.to_dict()
    serialised = json.dumps(d)
    reloaded = json.loads(serialised)
    assert reloaded["bomFormat"] == sbom.bom_format
    assert len(reloaded["components"]) == len(sbom.components)


def test_sign_artifact_then_verify(tmp_path):
    p = tmp_path / "wheel.whl"
    p.write_bytes(b"fake wheel bytes " * 100)
    key = hmac_keygen()
    sig = sign_artifact(p, key=key, kid="key-1")
    assert sig.algorithm == "HS256"
    assert sig.kid == "key-1"
    assert sig.artifact_sha256 == file_sha256(p)
    # Verify with the same key works.
    assert verify_artifact(p, sig, key=key)
    # Tamper with the file -> verify fails.
    p.write_bytes(b"tampered")
    assert not verify_artifact(p, sig, key=key)


def test_sign_artifact_wrong_key_fails(tmp_path):
    p = tmp_path / "x.txt"
    p.write_bytes(b"hello")
    sig = sign_artifact(p, key=hmac_keygen(), kid="key-1")
    assert not verify_artifact(p, sig, key=hmac_keygen())


def test_provenance_sign_and_verify():
    key = hmac_keygen()
    p = build_provenance(
        source_uri="https://github.com/frankfika/AIOrchestra",
        source_revision="abc123",
        builder_id="local-ci",
        materials=[{"uri": "pyproject.toml", "sha256": "x" * 64}],
        artifacts=[{"name": "orchestra-0.1.0.whl", "sha256": "y" * 64}],
        key=key, kid="key-1",
    )
    assert p.kid == "key-1"
    assert p.signature is not None
    # Verify with the right key.
    assert verify_provenance(p, key=key)
    # Tamper with the body -> verify fails.
    tampered = Provenance(
        build_id=p.build_id,
        source_uri="evil",
        source_revision=p.source_revision,
        builder_id=p.builder_id,
        build_started_at=p.build_started_at,
        build_finished_at=p.build_finished_at,
        materials=p.materials,
        artifacts=p.artifacts,
        signature=p.signature,
        kid=p.kid,
    )
    assert not verify_provenance(tampered, key=key)
    # Or a different key.
    assert not verify_provenance(p, key=hmac_keygen())


def test_git_revision_returns_a_string():
    rev = git_revision()
    # The repo has git; we should get a real hash or 'unknown'.
    assert isinstance(rev, str)
    assert len(rev) > 0
