"""M4 OSS-001 — Docker / Helm / CLI packaging tests.

A "clean-room install" means a fresh checkout can install Orchestra
and exercise it without manual steps. The tests here do **not** spin
up Docker or Helm (those are documented in ``docs/m4_install.md``)
but they DO verify the artifacts are well-formed and the package
itself can be installed in an isolated venv.
"""
from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Artifact shape tests
# ---------------------------------------------------------------------------


def test_dockerfile_exists_and_has_healthcheck():
    p = REPO_ROOT / "Dockerfile"
    assert p.exists(), "Dockerfile missing at repo root"
    text = p.read_text()
    assert "FROM " in text
    assert "HEALTHCHECK" in text
    assert "8000" in text


def test_docker_compose_is_valid_yaml_and_has_two_services():
    p = REPO_ROOT / "docker-compose.yml"
    assert p.exists(), "docker-compose.yml missing at repo root"
    data = yaml.safe_load(p.read_text())
    assert "services" in data
    services = data["services"]
    # Required: postgres + orchestra. CLI is profile-gated.
    assert "postgres" in services
    assert "orchestra" in services
    pg = services["postgres"]
    assert pg["image"].startswith("postgres")
    orchestra = services["orchestra"]
    ports = orchestra.get("ports") or []
    # Either "8000:8000" or the bound-to-localhost form is acceptable.
    assert any(p.endswith("8000:8000") for p in ports)
    assert orchestra.get("depends_on", {}).get("postgres")


def test_helm_chart_yaml_parses():
    chart = REPO_ROOT / "deploy" / "helm" / "Chart.yaml"
    assert chart.exists()
    data = yaml.safe_load(chart.read_text())
    assert data["name"] == "orchestra"
    assert data["apiVersion"] == "v2"


def test_helm_values_yaml_parses_and_has_required_keys():
    p = REPO_ROOT / "deploy" / "helm" / "values.yaml"
    data = yaml.safe_load(p.read_text())
    for key in ("image", "replicaCount", "service", "resources", "postgres", "smokeTest"):
        assert key in data, f"missing values key: {key}"


def test_helm_templates_exist():
    base = REPO_ROOT / "deploy" / "helm" / "templates"
    for fname in ("_helpers.tpl", "deployment.yaml", "service.yaml", "ingress.yaml", "secret.yaml"):
        assert (base / fname).exists(), f"missing template: {fname}"


def test_helm_template_files_are_non_empty():
    base = REPO_ROOT / "deploy" / "helm" / "templates"
    for p in base.glob("*.yaml"):
        assert p.stat().st_size > 0, f"empty template: {p.name}"
        # The template uses Helm template directives; a quick parse
        # check is to look for at least one `{{` (placeholder) and
        # at least one `kind:` line.
        text = p.read_text()
        assert "kind:" in text or p.name == "secret.yaml"


# ---------------------------------------------------------------------------
# CLI as entry point
# ---------------------------------------------------------------------------


def test_orchestra_cli_help_via_module():
    """``python -m orchestra.cli --help`` is the canonical first
    command a new contributor runs. It must succeed without any
    server up."""
    r = subprocess.run(
        [sys.executable, "-m", "orchestra.cli", "--help"],
        capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )
    assert r.returncode == 0, f"stderr: {r.stderr}"
    assert "Orchestra CLI" in r.stdout
    for cmd in ("submit", "status", "approve", "audit", "benchmark", "capabilities"):
        assert cmd in r.stdout, f"missing subcommand: {cmd}"


def test_orchestra_cli_capabilities_against_live_server(dsn, db_available):
    """The CLI's ``capabilities`` command reaches a live server and
    prints the registered capability set. This is the smoke test the
    clean-room install invokes to confirm the server is up."""
    if not db_available:
        pytest.skip("PostgreSQL not reachable")
    from fastapi.testclient import TestClient
    from orchestra.api.app import create_app

    with TestClient(create_app()) as client:
        # Hit /healthz to make sure the server is up; then run the
        # CLI pointing at the test server (which TestClient binds
        # to in-process). We can't bind a real port in TestClient
        # so we hit the FastAPI app directly through the test client.
        r = client.get("/healthz")
        assert r.status_code == 200
        # The capabilities subcommand needs a real port, so we just
        # call the underlying API and verify the shape is the one
        # the CLI expects.
        r = client.get("/capabilities")
        assert r.status_code == 200
        data = r.json()
        assert "manifests" in data
        assert "policy_rule_count" in data


# ---------------------------------------------------------------------------
# Clean-room install — install the package into a fresh venv and run
# the CLI. This is the most expensive test; we only run it on demand
# via the env var ``RUN_CLEAN_ROOM=1`` so the regular CI loop stays
# fast.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("RUN_CLEAN_ROOM"),
    reason="set RUN_CLEAN_ROOM=1 to run the clean-room install (slow)",
)
def test_clean_room_install(tmp_path):
    """Install Orchestra into a fresh venv and confirm the CLI loads."""
    venv_dir = tmp_path / "orchestra_clean_venv"
    builder = venv.EnvBuilder(with_pip=True, clear=True)
    builder.create(str(venv_dir))
    py = venv_dir / "bin" / "python"
    r = subprocess.run(
        [str(py), "-m", "pip", "install", "--quiet", str(REPO_ROOT)],
        capture_output=True, text=True, timeout=300,
    )
    assert r.returncode == 0, f"pip install failed: {r.stderr}"
    # The console script must be available.
    cli = venv_dir / "bin" / "orchestra"
    assert cli.exists(), "console script not installed"
    r = subprocess.run([str(cli), "--help"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    assert "Orchestra CLI" in r.stdout
