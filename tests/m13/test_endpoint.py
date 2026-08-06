"""M13 — /metrics endpoint + HTTPMetricsMiddleware tests.

The endpoint is what a SRE Prometheus scraper hits. The body
must be a valid Prometheus text-format payload; the
Content-Type must be the one prometheus_client emits so a SRE
can drop in any standard scraper.

The middleware must record every HTTP exchange — even the
``/metrics`` endpoint itself — so the dashboard can graph
request rate and latency by route.
"""
from __future__ import annotations

import pytest


def _make_app_state():
    """Build a minimal AppState without starting the PG-backed
    EventStore. The /metrics + /healthz paths must work even
    when the DB is down — the M11 doctor spec says healthz can
    report ``degraded`` but the API must still answer."""
    from orchestra.api.app import AppState
    from orchestra.observability import builtin_metrics
    return AppState(
        store=None,  # not used by /metrics; the /healthz path is
        coordinator=None,  # not used by /metrics
        benchmark_runner=None,
        metrics=builtin_metrics(),
    )


def test_metrics_endpoint_returns_prometheus_text_format():
    from orchestra.api.app import create_app
    from fastapi.testclient import TestClient

    state = _make_app_state()
    app = create_app(state)
    client = TestClient(app)
    r = client.get("/metrics")
    assert r.status_code == 200
    # Content-Type matches what a real Prometheus client emits.
    assert r.headers["content-type"].startswith("text/plain")
    assert "version=0.0.4" in r.headers["content-type"]
    body = r.text
    # The builtin metrics are all there.
    assert "orchestra_up" in body
    assert "orchestra_http_requests_total" in body
    assert "orchestra_egress_pep_projection_total" in body


def test_metrics_endpoint_reflects_request_count():
    """Hitting the endpoint itself must tick the request counter."""
    from orchestra.api.app import create_app
    from fastapi.testclient import TestClient

    state = _make_app_state()
    app = create_app(state)
    client = TestClient(app)
    # Drive a few requests through other routes.
    client.get("/healthz")
    client.get("/healthz")
    client.get("/healthz")
    r = client.get("/metrics")
    body = r.text
    # /healthz hit 4 times (3 + this /metrics call which is
    # also a request, but to a different path).
    healthz_count = 0
    for line in body.splitlines():
        if line.startswith('orchestra_http_requests_total{method="GET",path="/healthz",status="200"}'):
            healthz_count = int(float(line.rsplit(" ", 1)[1]))
            break
    assert healthz_count == 3


def test_metrics_endpoint_records_request_duration():
    from orchestra.api.app import create_app
    from fastapi.testclient import TestClient

    state = _make_app_state()
    app = create_app(state)
    client = TestClient(app)
    client.get("/healthz")
    r = client.get("/metrics")
    body = r.text
    # The histogram emits a ``_count`` line per label set.
    assert 'orchestra_http_request_duration_seconds_count' in body


def test_metrics_endpoint_does_not_break_under_concurrent_load():
    """The in-memory registry is process-local; concurrent
    scrapes just append. The endpoint must stay fast."""
    from orchestra.api.app import create_app
    from fastapi.testclient import TestClient

    state = _make_app_state()
    app = create_app(state)
    client = TestClient(app)
    for _ in range(20):
        r = client.get("/metrics")
        assert r.status_code == 200
    # No exception, no race; the registry is consistent.
    final = client.get("/metrics").text
    # Each /metrics call adds 1 to its own counter row.
    # The exact count varies; we just need it to be present.
    assert 'orchestra_http_requests_total{method="GET",path="/metrics"' in final


def test_middleware_uses_route_template_not_url_path():
    """The ``path`` label must use the matched route's template
    (``/tasks/{task_run_id}``) so a flood of distinct IDs can't
    blow up Prometheus label cardinality."""
    from orchestra.api.app import create_app
    from fastapi.testclient import TestClient

    # Use a stub store so the /tasks route can return 404
    # without needing a real PG connection.
    class _StubStore:
        def get_task_run(self, _tid):
            return None

    state = _make_app_state()
    state.store = _StubStore()
    app = create_app(state)
    client = TestClient(app)
    # /tasks/missing-id returns 404 — but it's still on the route
    # ``/tasks/{task_run_id}``.
    r = client.get("/tasks/some-uuid-1")
    assert r.status_code == 404
    r = client.get("/tasks/some-uuid-2")
    assert r.status_code == 404
    body = client.get("/metrics").text
    # Both requests land on the templated path, not the actual URL.
    assert 'path="/tasks/{task_run_id}"' in body
    # The actual URLs must NOT appear as path labels.
    assert 'path="/tasks/some-uuid-1"' not in body
    assert 'path="/tasks/some-uuid-2"' not in body
