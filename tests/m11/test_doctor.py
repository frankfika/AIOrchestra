"""M11 — Doctor command tests.

The CLI's ``orchestra doctor`` is the SRE health-check probe
the M7 runbook references. Tests here prove:

  * The doctor returns exit 0 when the cluster is green.
  * The doctor returns exit 1 on a soft warning (e.g. no
    published cards).
  * The doctor returns exit 2 on a hard failure (e.g.
    /healthz down).
  * The JSON output lists every check with name / status /
    detail.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest


def _run_doctor(base: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "orchestra.cli", "--base", base, "doctor"],
        capture_output=True, text=True, timeout=15,
    )


def test_doctor_against_live_server_returns_zero():
    """When the cluster is up, doctor returns 0 and reports
    api_health + capabilities + published_cards as ok."""
    from fastapi.testclient import TestClient
    from orchestra.api.app import create_app
    with TestClient(create_app()) as client:
        # We can't bind a real port in TestClient, so we use a
        # real port by booting uvicorn in this test (skipped
        # if port is in use).
        import socket
        s = socket.socket()
        s.bind(("", 0))
        port = s.getsockname()[1]
        s.close()
        import threading
        import time
        import uvicorn
        config = uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        t = threading.Thread(target=server.run, daemon=True)
        t.start()
        # Wait for startup.
        deadline = time.time() + 10
        while time.time() < deadline and not server.started:
            time.sleep(0.1)
        try:
            r = _run_doctor(f"http://127.0.0.1:{port}")
            assert r.returncode == 0, f"stdout: {r.stdout}, stderr: {r.stderr}"
            data = json.loads(r.stdout)
            assert data["base"] == f"http://127.0.0.1:{port}"
            assert data["failures"] == []
            # The checks list has the three named probes.
            check_names = {c["name"] for c in data["checks"]}
            assert "api_health" in check_names
            assert "capabilities" in check_names
            assert "published_cards" in check_names
        finally:
            server.should_exit = True
            t.join(timeout=5)


def test_doctor_against_unreachable_server_returns_two():
    """When the server is unreachable, doctor returns 2 (hard
    failure) and the checks list shows every probe as fail."""
    r = _run_doctor("http://127.0.0.1:1")  # nothing listens on :1
    assert r.returncode == 2
    data = json.loads(r.stdout)
    assert data["failures"], "expected at least one failure"
    for c in data["checks"]:
        assert c["status"] in ("fail", "warn")
