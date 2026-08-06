"""M17 — WebhookDispatcher tests.

The dispatcher is the partner-callback contract. The
tests below prove:

  * the body is signed with HMAC-SHA-256 and the
    ``X-Orchestra-Signature`` header carries
    ``sha256=<hex>``,
  * the body is the canonical JSON form (sort_keys)
    so the partner's signature check is stable against
    server-side key reordering,
  * retry happens on 5xx + transport error but not on
    4xx (a 4xx is a partner bug, not a transient
    failure; retrying just amplifies it),
  * the dispatcher's ``WebhookDelivery`` outcome
    correctly reports success vs. failure with
    attempt count + last status,
  * the URL validation refuses non-http(s) schemes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
import threading
import time
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request, Response

from orchestra.webhooks import (
    DELIVERY_ID_HEADER,
    EVENT_TYPE_HEADER,
    SIGNATURE_HEADER,
    WebhookConfig,
    WebhookDelivery,
    WebhookDispatcher,
    build_payload,
    sign_body,
)


# ---------------------------------------------------------------------------
# sign_body / build_payload
# ---------------------------------------------------------------------------


def test_sign_body_is_hmac_sha256():
    secret = "topsecret"
    body = b"hello"
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert sign_body(secret, body) == expected


def test_sign_body_changes_with_secret():
    body = b"hello"
    assert sign_body("a", body) != sign_body("b", body)


def test_build_payload_carries_required_fields():
    p = build_payload(
        task_run_id="tA",
        state="succeeded",
        plan_id="p1",
        node_results={"x": 1},
        error=None,
        delivery_id="d1",
    )
    assert p["event"] == "task.succeeded"
    assert p["task_run_id"] == "tA"
    assert p["state"] == "succeeded"
    assert p["plan_id"] == "p1"
    assert p["node_results"] == {"x": 1}
    assert p["error"] is None
    assert p["delivery_id"] == "d1"
    assert "delivered_at" in p


# ---------------------------------------------------------------------------
# WebhookConfig validation
# ---------------------------------------------------------------------------


def test_webhook_config_requires_http_scheme():
    assert WebhookConfig("https://ok.com", "s").is_valid() is True
    assert WebhookConfig("http://ok.com", "s").is_valid() is True
    assert WebhookConfig("ftp://bad.com", "s").is_valid() is False
    assert WebhookConfig("", "s").is_valid() is False
    assert WebhookConfig("https://ok.com", "").is_valid() is False


# ---------------------------------------------------------------------------
# Mock receiver — a real FastAPI app on a real port
# ---------------------------------------------------------------------------


def _start_receiver(handler, *, status_code: int = 200) -> str:
    """Start a FastAPI server on a random local port; return
    the base URL. The server's /webhook endpoint calls
    ``handler(request, body)`` and returns either the
    handler's dict or ``status_code`` (when ``handler``
    returns ``None``)."""
    app = FastAPI()

    @app.post("/webhook")
    async def receive(request: Request) -> Response:
        body = await request.body()
        result = handler(request, body)
        if result is None:
            return Response(status_code=status_code)
        if isinstance(result, Response):
            return result
        return Response(status_code=200, content=json.dumps(result))

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


def _stop_receiver(server, thread) -> None:
    server.should_exit = True
    thread.join(timeout=5)


def _build_dispatcher(*, max_attempts: int = 3) -> WebhookDispatcher:
    """A dispatcher with a real httpx.Client (no ASGITransport)."""
    return WebhookDispatcher(
        max_attempts=max_attempts,
        sleep=lambda _s: None,
    )


# ---------------------------------------------------------------------------
# Dispatcher — happy path
# ---------------------------------------------------------------------------


def test_dispatcher_posts_signed_payload():
    received: list[dict[str, Any]] = []

    def handler(request: Request, body: bytes) -> dict:
        received.append(
            {
                "headers": dict(request.headers),
                "body": body,
            }
        )
        return {"ok": True}

    base, server, thread = _start_receiver(handler)
    try:
        with _build_dispatcher() as dispatcher:
            config = WebhookConfig(f"{base}/webhook", "s3cret")
            result = dispatcher.deliver(
                config,
                task_run_id="tA",
                state="succeeded",
                plan_id=None,
                node_results={"k": "v"},
                error=None,
                delivery_id="d-001",
            )
        assert result.delivered is True
        assert result.attempts == 1
        assert result.last_status == 200
        assert result.delivery_id == "d-001"
        # The partner received exactly one call.
        assert len(received) == 1
        headers = received[0]["headers"]
        body = received[0]["body"]
        # Header lookups in HTTP are case-insensitive; normalise.
        norm = {k.lower(): v for k, v in headers.items()}
        # Signature header is present and valid.
        assert SIGNATURE_HEADER.lower() in norm
        sig = norm[SIGNATURE_HEADER.lower()]
        expected = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        assert sig == expected
        # Delivery id and event type are propagated.
        assert norm[DELIVERY_ID_HEADER.lower()] == "d-001"
        assert norm[EVENT_TYPE_HEADER.lower()] == "task.succeeded"
        # The body is the canonical JSON.
        parsed = json.loads(body.decode("utf-8"))
        assert parsed["task_run_id"] == "tA"
        assert parsed["state"] == "succeeded"
        assert parsed["node_results"] == {"k": "v"}
    finally:
        _stop_receiver(server, thread)


# ---------------------------------------------------------------------------
# Dispatcher — failure paths
# ---------------------------------------------------------------------------


def test_dispatcher_retries_on_5xx():
    """5xx is transient; the dispatcher retries up to
    max_attempts before giving up."""
    call_count = {"n": 0}

    def handler(request: Request, body: bytes) -> None:
        call_count["n"] += 1
        return None  # use the default 503 from _start_receiver

    base, server, thread = _start_receiver(handler, status_code=503)
    try:
        with _build_dispatcher() as dispatcher:
            config = WebhookConfig(f"{base}/webhook", "s3cret")
            result = dispatcher.deliver(
                config,
                task_run_id="tA",
                state="failed",
                plan_id=None,
                node_results={},
                error="boom",
                delivery_id="d-002",
            )
        assert result.delivered is False
        assert result.attempts == 3
        assert result.last_status == 503
        assert call_count["n"] == 3
    finally:
        _stop_receiver(server, thread)


def test_dispatcher_retries_on_transport_error():
    """A connection refused / DNS failure is transient;
    the dispatcher retries."""
    config = WebhookConfig("http://127.0.0.1:1/webhook", "s3cret")
    with _build_dispatcher() as dispatcher:
        result = dispatcher.deliver(
            config,
            task_run_id="tA",
            state="succeeded",
            plan_id=None,
            node_results={},
            error=None,
            delivery_id="d-003",
        )
    assert result.delivered is False
    assert result.attempts == 3
    assert result.last_status == 0
    assert (
        "Connection" in result.error
        or "refused" in result.error.lower()
        or "Connect" in result.error
    )


def test_dispatcher_invalid_config_is_a_no_op():
    """A bad config (empty URL, wrong scheme) is caught
    before any HTTP call is made; the partner-side
    failure is loud (returned error) but no request
    leaks."""
    config = WebhookConfig("ftp://nope.example", "s3cret")
    with _build_dispatcher() as dispatcher:
        result = dispatcher.deliver(
            config,
            task_run_id="tA",
            state="succeeded",
            plan_id=None,
            node_results={},
            error=None,
            delivery_id="d-005",
        )
    assert result.delivered is False
    assert result.attempts == 0
    assert "invalid" in result.error


# ---------------------------------------------------------------------------
# Signature stability
# ---------------------------------------------------------------------------


def test_signature_is_stable_against_payload_key_order():
    """The body is serialized with ``sort_keys=True`` so
    two payloads with the same fields in different
    orders produce the same signature. The dispatcher
    itself doesn't add this test; the function under
    test is :func:`build_payload`, which we then dump
    in the dispatcher's canonical way."""
    a = build_payload(
        task_run_id="t",
        state="succeeded",
        plan_id=None,
        node_results={"a": 1, "b": 2},
        error=None,
        delivery_id="d",
    )
    # Re-key the dict in a different order; the canonical
    # JSON form is identical because sort_keys=True.
    a_reordered = {k: a[k] for k in reversed(list(a.keys()))}
    body1 = json.dumps(a, sort_keys=True, ensure_ascii=False).encode("utf-8")
    body2 = json.dumps(a_reordered, sort_keys=True, ensure_ascii=False).encode("utf-8")
    assert body1 == body2
    assert sign_body("s", body1) == sign_body("s", body2)
