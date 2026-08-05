"""M6 ENT-003 — Supply chain: SBOM, signing, provenance.

The M6 supply chain surface is intentionally minimal:

  * :class:`SBOM` — a CycloneDX-like JSON document listing every
    dependency declared in ``pyproject.toml`` / ``requirements.txt``.
  * :func:`sign_artifact` — produce a detached signature for any
    file (wheel, SBOM, OCI image) using the same HMAC envelope
    the rest of Orchestra uses.
  * :class:`Provenance` — an in-toto-style SLSA provenance
    attestation: who built the artifact, when, from what source,
    with what dependencies.

M6 ships a *dev* implementation that uses local files + HMAC. The
production swap plugs in a real signing backend (Sigstore / cosign /
HSM) without changing the interface.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from orchestra.core.hashing import hmac_keygen, hmac_sign, hmac_verify
from orchestra.core.ids import new_id
from orchestra.core.time import utc_now_iso


# ---------------------------------------------------------------------------
# SBOM
# ---------------------------------------------------------------------------


@dataclass
class SBOMComponent:
    name: str
    version: str
    purl: str  # package URL (a la purl-spec)


@dataclass
class SBOM:
    """CycloneDX-like Software Bill of Materials.

    M6 ships a minimal JSON shape; the production pipeline will
    emit a full CycloneDX 1.5 / SPDX 2.3 document. The :meth:`to_dict`
    shape is intentionally stable so the signing pipeline can
    reference it.
    """

    bom_format: str = "CycloneDX"
    spec_version: str = "1.5"
    serial_number: str = field(default_factory=lambda: f"urn:uuid:{new_id()}")
    version: int = 1
    components: list[SBOMComponent] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bomFormat": self.bom_format,
            "specVersion": self.spec_version,
            "serialNumber": self.serial_number,
            "version": self.version,
            "components": [
                {"name": c.name, "version": c.version, "purl": c.purl} for c in self.components
            ],
            "metadata": self.metadata,
        }


def build_sbom_from_requirements(*, requirements_path: Path) -> SBOM:
    """Build a minimal SBOM from a pip-style requirements file.

    The dev implementation parses the file as ``name==version``
    lines. The production version will resolve transitives and
    emit a CycloneDX 1.5 / SPDX 2.3 document.
    """
    components: list[SBOMComponent] = []
    for raw in requirements_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Strip the version spec after the name.
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*([=~<>!]+.*)?$", line)
        if not m:
            continue
        name, spec = m.group(1), m.group(2) or ""
        version = ""
        if spec:
            vm = re.search(r"([0-9][0-9.\-a-zA-Z]*)", spec)
            if vm:
                version = vm.group(1)
        purl = f"pkg:pypi/{name}@{version}" if version else f"pkg:pypi/{name}"
        components.append(SBOMComponent(name=name, version=version, purl=purl))
    return SBOM(components=components, metadata={"generated_at": utc_now_iso()})


def build_sbom_from_pyproject(*, pyproject_path: Path) -> SBOM:
    """Build an SBOM from a PEP-621 pyproject.toml dependencies list.

    M6 dev impl uses a tiny TOML parser (the only TOML element
    the project uses is ``project.dependencies``); the production
    version will call into a full SPDX / CycloneDX generator.
    """
    deps = _parse_pyproject_dependencies(pyproject_path)
    components: list[SBOMComponent] = []
    for line in deps:
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*([=~<>!]+.*)?$", line.strip())
        if not m:
            continue
        name, spec = m.group(1), m.group(2) or ""
        version = ""
        if spec:
            vm = re.search(r"([0-9][0-9.\-a-zA-Z]*)", spec)
            if vm:
                version = vm.group(1)
        purl = f"pkg:pypi/{name}@{version}" if version else f"pkg:pypi/{name}"
        components.append(SBOMComponent(name=name, version=version, purl=purl))
    return SBOM(components=components, metadata={"generated_at": utc_now_iso()})


def _parse_pyproject_dependencies(path: Path) -> list[str]:
    """Tiny parser for the ``project.dependencies = [...]`` block.

    Avoids pulling in a TOML dependency for the dev build. We
    scan line by line: when we see ``dependencies = [`` we
    accumulate quoted strings until the matching ``]`` on its own
    line.
    """
    out: list[str] = []
    in_block = False
    depth = 0
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not in_block:
            if line.startswith("dependencies") and "[" in line:
                in_block = True
                depth = line.count("[") - line.count("]")
                # Capture any quoted strings on the same line.
                for m in re.findall(r'"([^"]+)"|\'([^\']+)\'', line):
                    if m[0]:
                        out.append(m[0])
                    elif m[1]:
                        out.append(m[1])
                if depth <= 0:
                    break
            continue
        # In-block: track depth and capture strings until close.
        for m in re.findall(r'"([^"]+)"|\'([^\']+)\'', line):
            if m[0]:
                out.append(m[0])
            elif m[1]:
                out.append(m[1])
        depth += line.count("[") - line.count("]")
        if depth <= 0:
            break
    return out


# ---------------------------------------------------------------------------
# Artifact signing
# ---------------------------------------------------------------------------


@dataclass
class ArtifactSignature:
    """A detached signature for an artifact (SBOM, wheel, OCI image)."""

    artifact_path: str
    artifact_sha256: str
    algorithm: str
    signature: str
    kid: str
    signed_at: str = field(default_factory=utc_now_iso)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sign_artifact(path: Path, *, key: bytes, kid: str) -> ArtifactSignature:
    """Sign a file with an HMAC key. The signature covers the
    file's SHA-256 so a verifier does not need the entire file in
    memory."""
    digest = file_sha256(path)
    sig = hmac_sign(key, {"sha256": digest, "path": str(path)})
    return ArtifactSignature(
        artifact_path=str(path),
        artifact_sha256=digest,
        algorithm="HS256",
        signature=sig,
        kid=kid,
    )


def verify_artifact(path: Path, signature: ArtifactSignature, *, key: bytes) -> bool:
    """Verify a detached signature. Re-computes the SHA-256 and
    checks the HMAC. Returns False on any mismatch."""
    if file_sha256(path) != signature.artifact_sha256:
        return False
    return hmac_verify(key, {"sha256": signature.artifact_sha256, "path": str(path)}, signature.signature)


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


@dataclass
class Provenance:
    """An in-toto-style SLSA provenance attestation.

    The dev attestation is a JSON document with the source
    revision, builder, materials (deps), and built artifacts. The
    production pipeline will sign this with a real key + cosign.
    """

    build_id: str
    source_uri: str
    source_revision: str
    builder_id: str
    build_started_at: str
    build_finished_at: str
    materials: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    signature: Optional[str] = None
    kid: Optional[str] = None

    def to_signable(self) -> dict[str, Any]:
        """The body that the signature covers. Excludes the
        signature / kid fields so the signer and verifier can both
        re-derive the same body."""
        return {
            "build_id": self.build_id,
            "source": {"uri": self.source_uri, "revision": self.source_revision},
            "builder": {"id": self.builder_id},
            "started_at": self.build_started_at,
            "finished_at": self.build_finished_at,
            "materials": self.materials,
            "artifacts": self.artifacts,
        }

    def to_dict(self) -> dict[str, Any]:
        d = self.to_signable()
        d["signature"] = self.signature
        d["kid"] = self.kid
        return d


def build_provenance(
    *,
    source_uri: str,
    source_revision: str,
    builder_id: str,
    materials: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    key: bytes,
    kid: str,
) -> Provenance:
    """Build + sign a Provenance attestation.

    The signature is HMAC over the to_dict body so a verifier does
    not need to re-derive the build environment.
    """
    import time as _time
    p = Provenance(
        build_id=f"build:{new_id()[:12]}",
        source_uri=source_uri,
        source_revision=source_revision,
        builder_id=builder_id,
        build_started_at=utc_now_iso(),
        build_finished_at=utc_now_iso(),
        materials=materials,
        artifacts=artifacts,
    )
    body = p.to_signable()
    sig = hmac_sign(key, body)
    p.signature = sig
    p.kid = kid
    return p


def verify_provenance(p: Provenance, *, key: bytes) -> bool:
    """Verify a Provenance attestation's signature."""
    if not p.signature or not p.kid:
        return False
    body = p.to_signable()
    return hmac_verify(key, body, p.signature)


def git_revision() -> str:
    """Best-effort: return the current HEAD short hash, or 'unknown'."""
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"
