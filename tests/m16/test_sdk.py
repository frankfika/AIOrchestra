"""M16 — Partner SDK tests.

The SDK is a transport wrapper; the tests below exercise
the wrapper against a mock server (a FastAPI app the test
builds in-process) so the tests don't need a live dev
path. The mock server emits the same RFC 7807 problem
bodies the real dev path emits, so the error-handling
tests prove the SDK's parsing logic, not just the mock's
shape.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, HTTPException, Response

import orchestra_sdk
from orchestra_sdk import OrchestraClient
from orchestra_sdk.client import TERMINAL_STATES
from orchestra_sdk.errors import (
    NotFoundError,
    OrchestraError,
    PayloadTooLargeError,
    ProblemDetail,
    RateLimitError,
    TaskNotFoundError,
    ValidationError,
    exception_for_problem,
)


# ---------------------------------------------------------------------------
# Mock server
# ---------------------------------------------------------------------------


def _build_mock_server() -> FastAPI:
    """A minimal mock of the AgenticHub HTTP shape.

    The state lives in a module-level dict so the tests can
    poke the same task across multiple calls. The mock
    emits the same problem body shape as the real dev path
    so the SDK parsing logic is exercised against the
    actual contract.
    """
    state: dict[str, Any] = {
        "tasks": {},  # task_run_id -> dict
    }
    app = FastAPI()

    def _problem(status: int, slug: str, detail: str = "") -> dict:
        return {
            "type": f"urn:orchestra:problem:{slug}",
            "title": slug.replace("_", " ").title() + ".",
            "status": status,
            "detail": detail or slug,
            "instance": "req-test-0001",
            "orchestra": {"request_id": "req-test-0001"},
        }

    @app.post("/api/v1/orchestra/submit")
    def submit(body: dict) -> dict:
        from orchestra.core.ids import new_id as _new_id

        tid = _new_id()
        state["tasks"][tid] = {
            "task_run_id": tid,
            "state": "created",
            "plan_id": None,
            "node_results": {},
            "error": None,
        }
        return state["tasks"][tid]

    @app.get("/api/v1/orchestra/tasks/{task_run_id}")
    def get_task(task_run_id: str) -> dict:
        if task_run_id not in state["tasks"]:
            return Response(
                content=json.dumps(_problem(404, "not_found", "task not found")),
                status_code=404,
                media_type="application/problem+json",
            )
        return state["tasks"][task_run_id]

    @app.get("/api/v1/orchestra/tasks/{task_run_id}/events")
    def get_events(task_run_id: str) -> dict:
        return {"task_run_id": task_run_id, "events": []}

    @app.get("/api/v1/orchestra/tasks/{task_run_id}/receipts")
    def get_receipts(task_run_id: str) -> dict:
        return {"task_run_id": task_run_id, "receipts": []}

    @app.get("/api/v1/orchestra/tasks/{task_run_id}/grants")
    def get_grants(task_run_id: str) -> dict:
        return {"task_run_id": task_run_id, "grants": []}

    @app.get("/api/v1/orchestra/tasks/{task_run_id}/approvals")
    def get_approvals(task_run_id: str) -> dict:
        return {"task_run_id": task_run_id, "approvals": []}

    @app.post("/api/v1/orchestra/tasks/{task_run_id}/decide")
    def decide(task_run_id: str, body: dict) -> dict:
        return {"status": "approved", "task_run_id": task_run_id}

    @app.post("/tasks/{task_run_id}/reject")
    def reject(task_run_id: str, body: dict) -> dict:
        return {"status": "rejected", "task_run_id": task_run_id}

    @app.get("/capabilities")
    def capabilities() -> dict:
        return {"manifests": [], "policy_rule_count": 0}

    @app.post("/limited")
    def limited() -> dict:
        """A test-only endpoint that always returns 429."""
        return Response(
            content=json.dumps(
                {
                    **_problem(429, "rate_limited", "bucket empty"),
                    "orchestra": {
                        "request_id": "req-test-0001",
                        "retry_after_seconds": 7,
                    },
                }
            ),
            status_code=429,
            media_type="application/problem+json",
            headers={"retry-after": "7"},
        )

    @app.post("/toolarge")
    def toolarge() -> dict:
        return Response(
            content=json.dumps(_problem(413, "payload_too_large", "body too large")),
            status_code=413,
            media_type="application/problem+json",
        )

    # Wire a Starlette 404 handler so unknown routes return
    # the problem body the SDK parses, not FastAPI's default
    # ``{"detail": "Not Found"}``.
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(StarletteHTTPException)
    async def _starlette_handler(request, exc: StarletteHTTPException):  # noqa: ARG001
        return Response(
            content=json.dumps(_problem(exc.status_code, "not_found", str(exc.detail))),
            status_code=exc.status_code,
            media_type="application/problem+json",
        )

    return app, state


@pytest.fixture
def mock_server():
    """Run the mock server on a real local port; return
    ``(base_url, state)`` so the SDK uses real HTTP."""
    app, state = _build_mock_server()
    sock = socket.socket()
    sock.bind(("", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # Wait for startup.
    deadline = time.time() + 10
    while time.time() < deadline and not server.started:
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}", state
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture
def client(mock_server):
    """An OrchestraClient pointed at the mock server."""
    base_url, _ = mock_server
    return OrchestraClient(base_url=base_url, tenant_id="tA")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_submit_task_returns_initial_status(client):
    s = client.submit_task(contract_id="ctr-1", contract_text="hello", vendor_id="v-1")
    assert s.state == "created"
    assert s.task_run_id
    assert s.is_terminal is False


def test_get_task_returns_status(client, mock_server):
    _, state = mock_server
    s = client.submit_task(contract_id="ctr-1", contract_text="hello", vendor_id="v-1")
    state["tasks"][s.task_run_id]["state"] = "succeeded"
    final = client.get_task(s.task_run_id)
    assert final.state == "succeeded"
    assert final.is_terminal is True


def test_approve_drives_task_to_terminal_state(client, mock_server):
    """The wait_for_completion helper polls; in a unit test
    we simulate the terminal state by mutating the mock
    server's state, then call the helper with a tight
    timeout."""
    _, state = mock_server
    s = client.submit_task(contract_id="ctr-1", contract_text="hello", vendor_id="v-1")
    # Mark the task as succeeded so the next poll returns.
    state["tasks"][s.task_run_id]["state"] = "succeeded"
    final = client.wait_for_completion(s.task_run_id, timeout=2.0)
    assert final.state == "succeeded"


def test_wait_for_completion_times_out(client, mock_server):
    """A task that never reaches a terminal state must
    raise a built-in TimeoutError (not an OrchestraError)
    so a partner's ``except`` block can handle the two
    separately."""
    s = client.submit_task(contract_id="ctr-1", contract_text="hello", vendor_id="v-1")
    with pytest.raises(TimeoutError):
        client.wait_for_completion(s.task_run_id, timeout=0.5, poll_interval=0.1)


def test_get_events_receipts_grants_approvals_return_lists(client):
    s = client.submit_task(contract_id="ctr-1", contract_text="hello", vendor_id="v-1")
    assert isinstance(client.get_events(s.task_run_id), list)
    assert isinstance(client.get_receipts(s.task_run_id), list)
    assert isinstance(client.get_grants(s.task_run_id), list)
    assert isinstance(client.get_approvals(s.task_run_id), list)


def test_approve_calls_decide_endpoint(client):
    """Approve goes through the M4 AgenticHub /decide endpoint."""
    s = client.submit_task(contract_id="ctr-1", contract_text="hello", vendor_id="v-1")
    out = client.approve(s.task_run_id, decided_by="alice", rationale="ok")
    assert out["status"] == "approved"


def test_reject_uses_legacy_json_reject(client):
    """Reject goes through the legacy JSON /reject endpoint
    (the AgenticHub shape is one-decision-only)."""
    s = client.submit_task(contract_id="ctr-1", contract_text="hello", vendor_id="v-1")
    out = client.reject(s.task_run_id, decided_by="alice", rationale="no")
    assert out["status"] == "rejected"


def test_list_capabilities(client):
    out = client.list_capabilities()
    assert "manifests" in out
    assert "policy_rule_count" in out


def test_context_manager_closes_owned_client():
    """The ``with`` block closes the underlying httpx client
    when the block exits."""
    with OrchestraClient(base_url="http://127.0.0.1:1", tenant_id="tA") as c:
        # The httpx client is lazily created on first call.
        # Trigger a creation by closing the client ourselves
        # is too aggressive; instead just verify that the
        # owned-client flag is set.
        assert c._owns_client is True
    # After exit, the client closes the httpx client.
    assert c._http.is_closed is True


# ---------------------------------------------------------------------------
# Error handling — the SDK turns every problem body into a typed exception
# ---------------------------------------------------------------------------


def test_task_not_found_raises_task_not_found_error(client):
    with pytest.raises(TaskNotFoundError) as ei:
        client.get_task("no-such-task")
    assert ei.value.status == 404
    assert ei.value.type_uri == "urn:orchestra:problem:not_found"
    # The request id round-trips so a partner can grep server logs.
    assert ei.value.request_id == "req-test-0001"


def test_rate_limit_raises_with_retry_after(client):
    """A 429 raises RateLimitError with the retry-after
    seconds parsed from the problem body."""
    # The mock's /limited endpoint always returns 429.
    response = client._http.post(f"{client._base_url}/limited", headers=client._headers())
    from orchestra_sdk.client import _parse

    with pytest.raises(RateLimitError) as ei:
        _parse(response)
    assert ei.value.retry_after_seconds == 7
    assert ei.value.status == 429


def test_payload_too_large_raises_payload_too_large_error(client):
    response = client._http.post(f"{client._base_url}/toolarge", headers=client._headers())
    from orchestra_sdk.client import _parse

    with pytest.raises(PayloadTooLargeError) as ei:
        _parse(response)
    assert ei.value.status == 413


def test_unknown_error_falls_through_to_orchestra_error():
    """A 418 (I'm a teapot) or any unmapped status falls
    through to the catch-all :class:`OrchestraError`."""
    problem = ProblemDetail(
        type="urn:orchestra:problem:teapot",
        title="Teapot.",
        status=418,
        detail="short and stout",
    )
    cls = exception_for_problem(problem)
    assert cls is OrchestraError


def test_problem_detail_from_dict_handles_missing_fields():
    """A server that omits a standard field (older dev path
    or a misbehaving partner probe) must not crash the parser."""
    p = ProblemDetail.from_dict({"type": "x", "title": "y", "status": 500})
    assert p.status == 500
    assert p.detail == ""
    assert p.request_id() == ""


def test_orchestra_error_str_carries_status_and_detail():
    """The ``__str__`` shape is what a partner sees in a
    crash log; it must carry the HTTP status and the problem
    detail so a crash is debuggable from the message alone."""
    problem = ProblemDetail(
        type="urn:orchestra:problem:not_found",
        title="Not found.",
        status=404,
        detail="task abc",
    )
    cls = exception_for_problem(problem)
    # Build the message the SDK would build in production:
    # ``f"{response.status_code} {problem.detail}"``.
    err = cls(f"{problem.status} {problem.detail}", problem=problem)
    assert "404" in str(err)
    assert "task abc" in str(err)


def test_orchestra_sdk_exports_match_dunder_all():
    """The public surface documented in ``__all__`` matches
    what the import actually exposes. A partner who reads
    the docs doesn't get a ``from orchestra_sdk import X``
    surprise."""
    for name in orchestra_sdk.__all__:
        assert hasattr(orchestra_sdk, name), f"missing: {name}"


def test_404_on_non_task_path_raises_not_found_error(client):
    """A 404 on a path that isn't ``/tasks/{id}`` raises the
    generic :class:`NotFoundError`, not the task-specific one.
    The task-specific repackaging only happens on the
    get_task path."""
    response = client._http.get(f"{client._base_url}/no-such-resource", headers=client._headers())
    from orchestra_sdk.client import _parse

    with pytest.raises(NotFoundError):
        _parse(response)
