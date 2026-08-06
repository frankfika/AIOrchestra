"""M16 — RFC 7807 Problem Details envelope tests.

Every 4xx and 5xx from the dev path carries the same JSON
shape. The tests below prove the wire format a partner
SDK will parse, plus the FastAPI exception handlers that
turn each kind of failure into the right problem body.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient

from orchestra.api.app import create_app, AppState


def _build_test_state():
    class StubStore:
        def get_task_run(self, _tid):
            return None

        def close(self):
            pass

    return AppState(store=StubStore(), coordinator=None, benchmark_runner=None)


def test_404_returns_problem_json():
    """A 404 (no matching task) returns ``application/problem+json``."""
    client = TestClient(create_app(_build_test_state()))
    r = client.get("/tasks/missing-id-xyz")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["type"] == "urn:orchestra:problem:not_found"
    assert body["title"] == "Resource not found."
    assert body["status"] == 404
    assert "task not found" in body["detail"]
    # Request id round-trips through the M9 middleware.
    assert "request_id" in body.get("orchestra", {})


def test_405_returns_problem_json():
    """Wrong HTTP method on a known path returns ProblemDetail too."""
    client = TestClient(create_app(_build_test_state()))
    r = client.post("/healthz")
    assert r.status_code == 405
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["type"] == "urn:orchestra:problem:method_not_allowed"


def test_422_returns_problem_json_with_errors_list():
    """A Pydantic validation error includes the full errors
    list in the ``orchestra`` extension so a partner SDK
    can show every field failure to the user, not just the
    first."""
    client = TestClient(create_app(_build_test_state()))
    r = client.post(
        "/tasks",
        json={"contract_id": "x"},  # missing contract_text, vendor_id
    )
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["type"] == "urn:orchestra:problem:validation_error"
    assert body["status"] == 422
    # The orchestra extension carries the structured errors list.
    errors = body["orchestra"]["errors"]
    assert isinstance(errors, list)
    assert len(errors) >= 1


def test_500_returns_problem_json_without_leaking_traceback():
    """An unhandled exception must not leak the raw traceback
    in the body; the partner gets a stable ``internal_error``
    problem and the operator greps by request id."""
    # Build the real app, then add a /boom route that raises
    # so the M16 exception handler runs. The default 500
    # handler in create_app emits a ProblemDetail body.
    from fastapi.testclient import TestClient

    state = _build_test_state()
    app = create_app(state)

    @app.get("/boom")
    def boom() -> dict:
        raise RuntimeError("secret internal state leaked here")

    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/boom")
    assert r.status_code == 500
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["type"] == "urn:orchestra:problem:internal_error"
    assert body["status"] == 500
    # The traceback must not leak.
    assert "secret internal state" not in r.text
    # The error_type extension is there for log correlation.
    assert body["orchestra"]["error_type"] == "RuntimeError"


def test_problem_type_uri_is_stable():
    """A partner's runbook can match the URN-shaped ``type``;
    the slug is the last component."""
    from orchestra.api.errors import problem_type_uri

    assert problem_type_uri("rate_limited") == "urn:orchestra:problem:rate_limited"
    assert problem_type_uri("not_found") == "urn:orchestra:problem:not_found"
