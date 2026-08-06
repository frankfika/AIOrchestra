"""M15 — OpenAPI metadata + tag coverage tests.

The /docs page is the partner developer's first stop. The
tests below prove the partner-developer experience:

  * every endpoint is grouped under a tag the partner
    recognises (Tasks, Admin, Health, Metrics, ...),
  * every endpoint has a one-line summary that fits in
    the /docs sidebar,
  * the /docs and /redoc pages render (the FastAPI defaults
    but the M15 commit pins the URLs so they're discoverable
    from the spec).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from orchestra.api.app import create_app, AppState
from orchestra.api.openapi import TAGS_METADATA


def _build_test_state():
    class StubStore:
        def get_task_run(self, _tid):
            return None

        def close(self):
            pass

    return AppState(store=StubStore(), coordinator=None, benchmark_runner=None)


def test_openapi_includes_canonical_tag_groups():
    """The TAGS_METADATA constant defines the canonical
    partner-facing groupings; they all show up in /openapi.json."""
    state = _build_test_state()
    client = TestClient(create_app(state))
    spec = client.get("/openapi.json").json()
    declared = {t["name"] for t in spec.get("tags", [])}
    expected = {t["name"] for t in TAGS_METADATA}
    assert expected <= declared, f"missing tag groups: {expected - declared}"


def test_every_endpoint_has_a_tag():
    """No endpoint is left in the default group; partners
    navigating /docs see every call under a named surface."""
    state = _build_test_state()
    client = TestClient(create_app(state))
    spec = client.get("/openapi.json").json()
    untagged = []
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            if method == "parameters":
                continue
            if not op.get("tags"):
                untagged.append(f"{method.upper()} {path}")
    assert untagged == [], f"untagged endpoints: {untagged}"


def test_every_endpoint_has_a_summary():
    """The /docs sidebar shows the summary; a missing one
    is a documentation gap that a partner developer will
    notice on first read."""
    state = _build_test_state()
    client = TestClient(create_app(state))
    spec = client.get("/openapi.json").json()
    no_summary = []
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            if method == "parameters":
                continue
            if not op.get("summary"):
                no_summary.append(f"{method.upper()} {path}")
    assert no_summary == [], f"endpoints without summary: {no_summary}"


def test_openapi_includes_partner_facing_tag_descriptions():
    """The tag descriptions are the partner developer's
    quick orientation; they must mention the partner-facing
    call surface so a first-time reader knows where to look."""
    state = _build_test_state()
    client = TestClient(create_app(state))
    spec = client.get("/openapi.json").json()
    by_name = {t["name"]: t for t in spec.get("tags", [])}
    # Tasks — the partner submits a task; the description
    # must mention the submission endpoint.
    assert "submit" in by_name["Tasks"]["description"].lower()
    # Admin — the CLI calls these; the description must
    # mention the CLI subcommands.
    assert "cli" in by_name["Admin"]["description"].lower()
    # Health — must mention SRE probes.
    assert "sre" in by_name["Health"]["description"].lower()


def test_openapi_version_reflects_current_milestone():
    """The version field is what partner SDK generators
    pin against. It must reflect the M15 reality (not
    the P0-era 0.1.0-p0)."""
    state = _build_test_state()
    client = TestClient(create_app(state))
    spec = client.get("/openapi.json").json()
    assert "0.1.0-m" in spec["info"]["version"]


def test_docs_and_redoc_routes_are_pinned():
    """``/docs`` (Swagger UI) and ``/redoc`` are the
    partner-developer onboarding entry points. The M15
    commit pins them in the FastAPI constructor so the
    routes are discoverable from the spec itself."""
    state = _build_test_state()
    client = TestClient(create_app(state))
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200


def test_openapi_spec_contains_known_partner_endpoints():
    """A partner developer looking for the submission
    endpoint should find /tasks in the spec — the
    canonical name, not a hidden alias."""
    state = _build_test_state()
    client = TestClient(create_app(state))
    spec = client.get("/openapi.json").json()
    assert "/tasks" in spec["paths"]
    assert "/tasks/{task_run_id}" in spec["paths"]
    assert "/api/v1/orchestra/submit" in spec["paths"]
