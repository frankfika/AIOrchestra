"""M15 — CORS middleware tests.

CORS is the partner-UI unlock. The dev path defaults to
"no CORS" so a browser hitting the API without an explicit
allow-list still gets blocked. The tests below prove the
three modes:

  * ``ORCHESTRA_CORS_ORIGINS=""`` — CORS disabled; a browser
    preflight is rejected (no ``Access-Control-Allow-Origin``
    response header).
  * ``ORCHESTRA_CORS_ORIGINS="*"`` — wildcard allow; the
    response carries ``Access-Control-Allow-Origin: *`` and
    credentials are NOT permitted.
  * ``ORCHESTRA_CORS_ORIGINS="https://a.com,https://b.com"``
    — explicit allow-list; only the listed origins get
    the allow header back.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestra.api.openapi import apply_cors, cors_origins_from_env


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/probe")
    def probe() -> dict:
        return {"probe": "ok"}

    return app


def test_cors_disabled_by_default(monkeypatch):
    """With no env var, CORS is off; preflight gets no
    Access-Control-Allow-Origin header."""
    monkeypatch.delenv("ORCHESTRA_CORS_ORIGINS", raising=False)
    app = _build_app()
    apply_cors(app, origins=cors_origins_from_env())
    client = TestClient(app)
    # The preflight OPTIONS request should NOT have the CORS
    # header because CORS isn't enabled.
    r = client.options(
        "/probe",
        headers={
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers.keys()}


def test_cors_wildcard_allows_any_origin(monkeypatch):
    monkeypatch.setenv("ORCHESTRA_CORS_ORIGINS", "*")
    app = _build_app()
    apply_cors(app, origins=cors_origins_from_env())
    client = TestClient(app)
    r = client.get("/probe", headers={"Origin": "https://anything.example"})
    assert r.headers.get("access-control-allow-origin") == "*"
    # credentials flag is disabled with wildcard (CORS spec).
    assert "access-control-allow-credentials" not in {k.lower() for k in r.headers.keys()}


def test_cors_explicit_allowlist(monkeypatch):
    monkeypatch.setenv(
        "ORCHESTRA_CORS_ORIGINS",
        "https://partner-a.com,https://partner-b.com",
    )
    app = _build_app()
    apply_cors(app, origins=cors_origins_from_env())
    client = TestClient(app)
    # An allowed origin gets the header.
    r = client.get("/probe", headers={"Origin": "https://partner-a.com"})
    assert r.headers.get("access-control-allow-origin") == "https://partner-a.com"
    # A non-allowed origin does NOT get the header.
    r = client.get("/probe", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers.keys()}


def test_cors_preflight_includes_partner_headers(monkeypatch):
    """The preflight response advertises the partner-UI
    headers the SDK needs (X-Tenant-Id, X-Request-Id,
    Content-Type, Authorization)."""
    monkeypatch.setenv("ORCHESTRA_CORS_ORIGINS", "https://partner.com")
    app = _build_app()
    apply_cors(app, origins=cors_origins_from_env())
    client = TestClient(app)
    r = client.options(
        "/probe",
        headers={
            "Origin": "https://partner.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-tenant-id",
        },
    )
    allow_headers = r.headers.get("access-control-allow-headers", "").lower()
    # The exact allow list is set by apply_cors; the test
    # only asserts the partner-UI headers are present in
    # the response (CORS may split the list across multiple
    # headers — we look at any header with that name).
    assert "content-type" in allow_headers
    assert "x-tenant-id" in allow_headers
