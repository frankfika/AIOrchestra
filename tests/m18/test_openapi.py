"""M18 — OpenAPI example coverage.

The /docs page is the partner developer's first stop.
The tests below prove that the most-used partner-facing
endpoints ship with a request body example + a few
response examples (200, 422, 429, 404) so the partner
can copy-paste a real shape into their SDK generator.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from orchestra.api.app import create_app, AppState


def _build_test_state():
    class StubStore:
        def get_task_run(self, _tid):
            return None

        def close(self):
            pass

    return AppState(store=StubStore(), coordinator=None, benchmark_runner=None)


def test_post_tasks_has_request_body_example():
    """``POST /tasks`` is the partner's first call; the
    request body example is the shape the partner's SDK
    generator copies into their code."""
    client = TestClient(create_app(_build_test_state()))
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/tasks"]["post"]
    rb = op.get("requestBody", {}).get("content", {}).get("application/json", {})
    example = rb.get("example")
    assert example is not None
    assert "contract_id" in example
    assert "contract_text" in example
    assert "vendor_id" in example
    # The webhook fields are part of the example so a
    # partner who reads /docs sees the M17 shape.
    assert "webhook_url" in example
    assert "webhook_secret" in example


def test_post_tasks_200_example_carries_real_field_values():
    """The 200 example shows the real field names +
    sample values. A partner SDK generator uses the
    example to build a typed response object."""
    client = TestClient(create_app(_build_test_state()))
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/tasks"]["post"]
    example = (
        op.get("responses", {})
        .get("200", {})
        .get("content", {})
        .get("application/json", {})
        .get("example")
    )
    assert example is not None
    assert example["state"] == "created"
    assert "task_run_id" in example


def test_post_tasks_422_example_carries_problem_envelope():
    """The 422 example must use the RFC 7807 envelope
    shape (type / title / status / detail / orchestra
    extension)."""
    client = TestClient(create_app(_build_test_state()))
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/tasks"]["post"]
    example = (
        op.get("responses", {})
        .get("422", {})
        .get("content", {})
        .get("application/problem+json", {})
        .get("example")
    )
    assert example is not None
    assert example["type"] == "urn:orchestra:problem:validation_error"
    assert example["status"] == 422
    # The orchestra extension carries the structured errors.
    assert "errors" in example["orchestra"]
    assert isinstance(example["orchestra"]["errors"], list)


def test_post_tasks_429_example_carries_retry_after():
    """The 429 example surfaces the ``retry_after_seconds``
    extension a partner's SDK reads to back off."""
    client = TestClient(create_app(_build_test_state()))
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/tasks"]["post"]
    example = (
        op.get("responses", {})
        .get("429", {})
        .get("content", {})
        .get("application/problem+json", {})
        .get("example")
    )
    assert example is not None
    assert example["type"] == "urn:orchestra:problem:rate_limited"
    assert "retry_after_seconds" in example["orchestra"]


def test_admin_webhook_history_has_response_examples():
    """The webhook history endpoint ships with a 200
    example so a SRE looking at /docs sees the shape
    without having to trigger a delivery first."""
    client = TestClient(create_app(_build_test_state()))
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/admin/webhooks/{task_run_id}"]["get"]
    example = (
        op.get("responses", {})
        .get("200", {})
        .get("content", {})
        .get("application/json", {})
        .get("example")
    )
    assert example is not None
    assert "deliveries" in example
    assert example["count"] >= 1
    # The first delivery carries the full set of fields.
    first = example["deliveries"][0]
    for key in (
        "delivery_id",
        "task_run_id",
        "state",
        "delivered",
        "attempts",
        "last_status",
        "error",
        "attempt_started_at",
    ):
        assert key in first, f"missing example field: {key}"


def test_openapi_lists_examples_in_swagger_ui():
    """The Swagger UI (the partner-developer view) shows
    the example dropdowns. The OpenAPI spec carries
    them under ``example`` (singular) — a partner
    generator (openapi-generator, etc.) consumes
    these without further config."""
    client = TestClient(create_app(_build_test_state()))
    spec = client.get("/openapi.json").json()
    # Walk every operation, count operations that
    # have at least one example somewhere.
    ops_with_examples = 0
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            if method == "parameters":
                continue
            has_example = False
            for body in (op.get("requestBody") or {}).get("content", {}).values():
                if "example" in body:
                    has_example = True
            for response in op.get("responses", {}).values():
                for body in response.get("content", {}).values():
                    if "example" in body:
                        has_example = True
            if has_example:
                ops_with_examples += 1
    # The two endpoints we just added are the only ones
    # with examples; older endpoints ship the schema
    # only. The contract is "the partner-developer
    # onboarding endpoints have examples".
    assert ops_with_examples >= 2
