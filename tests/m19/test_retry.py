"""M19 — Webhook manual retry tests.

A SRE who notices a partner's endpoint is back online
uses ``POST /admin/webhooks/{task_id}/retry`` to re-fire
the latest failed delivery without re-submitting the
task. The retry uses the original partner URL + secret
(stored on the record) and a fresh ``delivery_id`` so
the partner's dedup logic sees it as a new attempt.

The tests below prove:

  * the endpoint returns 404 when there is no failed
    delivery to retry (either no history at all, or
    every prior delivery already succeeded),
  * the endpoint re-fires the original payload with a
    new ``delivery_id`` and appends the new attempt to
    the history,
  * the partner URL + secret are reused without the
    operator having to re-supply them.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request, Response

from orchestra.webhooks import (
    DeliveryHistory,
    WebhookConfig,
    WebhookDeliveryRecord,
    WebhookDispatcher,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _start_partner_receiver(received: list, status_code: int = 200) -> tuple:
    """Run a tiny FastAPI server that records every POST."""
    app = FastAPI()

    @app.post("/webhook")
    async def receive(request: Request) -> Response:
        body = await request.body()
        received.append(
            {
                "headers": dict(request.headers),
                "body": body,
            }
        )
        return Response(status_code=status_code)

    sock = socket.socket()
    sock.bind(("", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline and not server.started:
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}", server, thread


def _stop(server, thread) -> None:
    server.should_exit = True
    thread.join(timeout=5)


# ---------------------------------------------------------------------------
# DeliveryHistory.last_failed
# ---------------------------------------------------------------------------


def test_last_failed_returns_most_recent_failure():
    """The retry endpoint re-fires the latest failure,
    not the earliest one — operators usually want the
    most recent payload."""
    h = DeliveryHistory()
    # Earliest failure.
    h.record(
        WebhookDeliveryRecord(
            delivery_id="d1",
            task_run_id="tA",
            state="succeeded",
            delivered=False,
            attempts=3,
            last_status=503,
            webhook_url="http://old.example/wh",
            webhook_secret="old",
        )
    )
    # Success in the middle.
    h.record(
        WebhookDeliveryRecord(
            delivery_id="d2",
            task_run_id="tA",
            state="succeeded",
            delivered=True,
            attempts=1,
            last_status=200,
            webhook_url="http://old.example/wh",
            webhook_secret="old",
        )
    )
    # Latest failure.
    h.record(
        WebhookDeliveryRecord(
            delivery_id="d3",
            task_run_id="tA",
            state="succeeded",
            delivered=False,
            attempts=3,
            last_status=502,
            webhook_url="http://new.example/wh",
            webhook_secret="new",
        )
    )
    last = h.last_failed("tA")
    assert last is not None
    assert last.delivery_id == "d3"
    # The new URL + secret are surfaced for the retry.
    assert last.webhook_url == "http://new.example/wh"
    assert last.webhook_secret == "new"


def test_last_failed_returns_none_for_unknown_task():
    h = DeliveryHistory()
    assert h.last_failed("never-seen") is None


def test_last_failed_returns_none_when_all_succeeded():
    h = DeliveryHistory()
    h.record(
        WebhookDeliveryRecord(
            delivery_id="d1",
            task_run_id="tA",
            state="succeeded",
            delivered=True,
            attempts=1,
            last_status=200,
        )
    )
    assert h.last_failed("tA") is None


# ---------------------------------------------------------------------------
# API endpoint
# ---------------------------------------------------------------------------


def _build_test_state(
    history: DeliveryHistory | None = None, dispatcher: WebhookDispatcher | None = None
):
    from orchestra.api.app import AppState

    class StubStore:
        def get_task_run(self, _tid):
            return None

        def close(self):
            pass

    state = AppState(store=StubStore(), coordinator=None, benchmark_runner=None)
    if history is not None:
        state.webhook_history = history
    if dispatcher is not None:
        state.webhook_dispatcher = dispatcher
    return state


def test_retry_endpoint_returns_404_when_no_history():
    """A task that has never had a webhook can't be
    retried; the operator sees 404 with a clear
    problem body."""
    from fastapi.testclient import TestClient
    from orchestra.api.app import create_app

    state = _build_test_state(
        history=DeliveryHistory(),
        dispatcher=WebhookDispatcher(sleep=lambda _s: None),
    )
    client = TestClient(create_app(state))
    r = client.post("/admin/webhooks/never-seen/retry")
    assert r.status_code == 404
    body = r.json()
    assert body["type"] == "urn:orchestra:problem:not_found"


def test_retry_endpoint_returns_404_when_all_delivered():
    """A task whose every prior delivery succeeded has
    nothing to retry; 404 with a clear message."""
    from fastapi.testclient import TestClient
    from orchestra.api.app import create_app

    history = DeliveryHistory()
    history.record(
        WebhookDeliveryRecord(
            delivery_id="d1",
            task_run_id="tA",
            state="succeeded",
            delivered=True,
            attempts=1,
            last_status=200,
        )
    )
    state = _build_test_state(
        history=history,
        dispatcher=WebhookDispatcher(sleep=lambda _s: None),
    )
    client = TestClient(create_app(state))
    r = client.post("/admin/webhooks/tA/retry")
    assert r.status_code == 404


def test_retry_endpoint_refires_original_payload_with_new_id():
    """The retry path re-fires the original payload using
    the stored URL + secret; a fresh ``delivery_id``
    marks it as a new attempt."""
    from fastapi.testclient import TestClient
    from orchestra.api.app import create_app

    received: list[dict[str, Any]] = []
    base, server, thread = _start_partner_receiver(received, status_code=200)
    try:
        history = DeliveryHistory()
        # Record a previous failure that points at the
        # partner we're about to bring online.
        history.record(
            WebhookDeliveryRecord(
                delivery_id="original-delivery-1",
                task_run_id="tA",
                state="succeeded",
                delivered=False,
                attempts=3,
                last_status=502,
                error="HTTP 502",
                webhook_url=f"{base}/webhook",
                webhook_secret="partner-secret",
                plan_id="plan-xyz",
                node_results={"vendor": "acme", "score": 0.91},
                payload_error=None,
            )
        )
        state = _build_test_state(
            history=history,
            dispatcher=WebhookDispatcher(sleep=lambda _s: None),
        )
        client = TestClient(create_app(state))
        r = client.post("/admin/webhooks/tA/retry")
        assert r.status_code == 200
        body = r.json()
        assert body["task_run_id"] == "tA"
        assert body["retried"] is True
        assert body["delivered"] is True
        assert body["attempts"] == 1
        assert body["last_status"] == 200
        # The new delivery_id is fresh and different
        # from the original.
        assert body["new_delivery_id"] != "original-delivery-1"
        # The partner received the new POST.
        assert len(received) == 1
        # The body carries the original payload (the
        # plan_id + node_results we recorded).
        import json

        parsed = json.loads(received[0]["body"].decode("utf-8"))
        assert parsed["plan_id"] == "plan-xyz"
        assert parsed["node_results"]["vendor"] == "acme"
        # The signature header is present and verifies
        # against the original secret.
        import hmac, hashlib

        norm = {k.lower(): v for k, v in received[0]["headers"].items()}
        sig = norm["x-orchestra-signature"]
        body_bytes = received[0]["body"]
        expected = "sha256=" + hmac.new(b"partner-secret", body_bytes, hashlib.sha256).hexdigest()
        assert sig == expected
    finally:
        _stop(server, thread)


def test_retry_endpoint_appends_new_record_to_history():
    """The retry appends a new record to the history so
    a follow-up ``GET /admin/webhooks/{id}`` shows both
    the original failure and the retry outcome."""
    from fastapi.testclient import TestClient
    from orchestra.api.app import create_app

    received: list[dict[str, Any]] = []
    base, server, thread = _start_partner_receiver(received, status_code=200)
    try:
        history = DeliveryHistory()
        history.record(
            WebhookDeliveryRecord(
                delivery_id="d-original",
                task_run_id="tA",
                state="succeeded",
                delivered=False,
                attempts=3,
                last_status=502,
                webhook_url=f"{base}/webhook",
                webhook_secret="s",
            )
        )
        state = _build_test_state(
            history=history,
            dispatcher=WebhookDispatcher(sleep=lambda _s: None),
        )
        client = TestClient(create_app(state))
        client.post("/admin/webhooks/tA/retry")
        # The history now has 2 records: the original
        # failure + the new retry.
        records = history.for_task("tA")
        assert len(records) == 2
        assert records[0].delivery_id == "d-original"
        assert records[0].delivered is False
        assert records[1].delivery_id != "d-original"
        assert records[1].delivered is True
    finally:
        _stop(server, thread)


def test_retry_endpoint_uses_stored_url_and_secret():
    """The operator doesn't re-supply the URL + secret;
    the dispatcher reads them off the record. This
    test proves a partner who set up the webhook once
    doesn't need to give the SRE the secret over chat."""
    from fastapi.testclient import TestClient
    from orchestra.api.app import create_app

    received: list[dict[str, Any]] = []
    base, server, thread = _start_partner_receiver(received, status_code=200)
    try:
        history = DeliveryHistory()
        history.record(
            WebhookDeliveryRecord(
                delivery_id="d-1",
                task_run_id="tA",
                state="succeeded",
                delivered=False,
                attempts=3,
                last_status=502,
                webhook_url=f"{base}/webhook",
                webhook_secret="opaque-secret-42",
            )
        )
        state = _build_test_state(
            history=history,
            dispatcher=WebhookDispatcher(sleep=lambda _s: None),
        )
        client = TestClient(create_app(state))
        r = client.post("/admin/webhooks/tA/retry")
        assert r.status_code == 200
        # The partner received a body signed with the
        # stored secret. A SRE who lost the secret can
        # still retry because the dev path keeps it on
        # the record.
        import hmac, hashlib

        body_bytes = received[0]["body"]
        norm = {k.lower(): v for k, v in received[0]["headers"].items()}
        sig = norm["x-orchestra-signature"]
        expected = "sha256=" + hmac.new(b"opaque-secret-42", body_bytes, hashlib.sha256).hexdigest()
        assert sig == expected
    finally:
        _stop(server, thread)


# ---------------------------------------------------------------------------
# OpenAPI example
# ---------------------------------------------------------------------------


def test_retry_endpoint_has_openapi_examples():
    """The partner-developer view at /docs shows real
    request/response shapes for the retry endpoint."""
    from fastapi.testclient import TestClient
    from orchestra.api.app import create_app

    state = _build_test_state(
        history=DeliveryHistory(),
        dispatcher=WebhookDispatcher(sleep=lambda _s: None),
    )
    client = TestClient(create_app(state))
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/admin/webhooks/{task_run_id}/retry"]["post"]
    # 200 example carries the new_delivery_id + outcome fields.
    example_200 = (
        op.get("responses", {})
        .get("200", {})
        .get("content", {})
        .get("application/json", {})
        .get("example")
    )
    assert example_200 is not None
    assert "new_delivery_id" in example_200
    assert "delivered" in example_200
    # 404 example is the problem envelope.
    example_404 = (
        op.get("responses", {})
        .get("404", {})
        .get("content", {})
        .get("application/problem+json", {})
        .get("example")
    )
    assert example_404 is not None
    assert example_404["type"] == "urn:orchestra:problem:not_found"
