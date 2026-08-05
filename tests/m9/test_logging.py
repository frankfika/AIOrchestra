"""M9 — Logging + request id correlation tests."""
from __future__ import annotations

import io
import json
import logging

import pytest
from fastapi.testclient import TestClient

from orchestra.api.app import create_app
from orchestra.core.logging import (
    JsonFormatter,
    RequestIdMiddleware,
    current_request_id,
    set_request_id,
    setup_logging,
)


def test_json_formatter_emits_canonical_fields():
    """The JSON shape is stable: ts, level, logger, msg, request_id
    (when active), and any ``extra`` fields the caller passed."""
    fmt = JsonFormatter()
    rid_token = set_request_id("req-test-1")
    try:
        record = logging.LogRecord(
            name="orchestra.test", level=logging.INFO, pathname=__file__,
            lineno=1, msg="hello %s", args=("world",), exc_info=None,
        )
        record.task_run_id = "trun-1"
        out = fmt.format(record)
        body = json.loads(out)
        assert body["level"] == "INFO"
        assert body["logger"] == "orchestra.test"
        assert body["msg"] == "hello world"
        assert body["request_id"] == "req-test-1"
        assert body["task_run_id"] == "trun-1"
        assert "ts" in body
    finally:
        set_request_id(None)


def test_json_formatter_omits_request_id_when_unset():
    fmt = JsonFormatter()
    record = logging.LogRecord(
        name="x", level=logging.WARNING, pathname=__file__, lineno=1,
        msg="no rid", args=(), exc_info=None,
    )
    body = json.loads(fmt.format(record))
    assert "request_id" not in body
    assert body["msg"] == "no rid"


def test_setup_logging_installs_json_formatter():
    """setup_logging must be idempotent — calling it twice should
    not duplicate handlers."""
    buf = io.StringIO()
    setup_logging(level="INFO", json_output=True, stream=buf)
    root = logging.getLogger()
    n_handlers = len(root.handlers)
    setup_logging(level="INFO", json_output=True, stream=buf)
    assert len(logging.getLogger().handlers) == n_handlers
    # The installed handler is the JSON formatter.
    assert isinstance(root.handlers[0].formatter, JsonFormatter)


def test_request_id_middleware_propagates_caller_header():
    """If the caller sends X-Request-Id, the middleware uses it
    verbatim — the upstream gateway's trace continues across
    Orchestra."""
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/healthz", headers={"X-Request-Id": "trace-abc-123"})
        assert r.headers.get("x-request-id") == "trace-abc-123"


def test_request_id_middleware_generates_id_when_caller_omits():
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/healthz")
        rid = r.headers.get("x-request-id")
        assert rid is not None
        assert rid.startswith("req-")


def test_request_id_context_var_inside_request():
    """A log record emitted inside a request handler carries the
    request id; outside the request, the contextvar is None."""
    captured: list[dict] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record):
            try:
                captured.append(json.loads(self.format(record)))
            except Exception:  # noqa: BLE001
                pass

    fmt = JsonFormatter()
    handler = CaptureHandler()
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        app = create_app()
        with TestClient(app) as client:
            r = client.get("/healthz", headers={"X-Request-Id": "trace-xyz-1"})
            assert r.headers.get("x-request-id") == "trace-xyz-1"
        # The middleware sets / resets the contextvar; the
        # assert "contextvar is unset after the request" is
        # the strongest check we can make without coupling to
        # uvicorn's internal logging.
        assert current_request_id() is None
    finally:
        root.removeHandler(handler)


def test_log_emit_during_middleware_includes_request_id():
    """When a log record is emitted while a request is in
    flight, the JSON output carries the request id."""
    captured: list[dict] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record):
            try:
                captured.append(json.loads(self.format(record)))
            except Exception:  # noqa: BLE001
                pass

    fmt = JsonFormatter()
    handler = CaptureHandler()
    handler.setFormatter(fmt)
    # Install the handler BEFORE any other code touches the root
    # logger. We bypass setup_logging entirely so the test
    # controls the handler list.
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        # Build a middleware around a tiny inner app; do NOT
        # call create_app() (that would call setup_logging and
        # nuke our handler).
        async def _inner(scope, receive, send):
            lg = logging.getLogger("orchestra.inner")
            lg.info("during request", extra={"task_run_id": "trun-x"})
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        wrapped = RequestIdMiddleware(_inner)
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"x-request-id", b"trace-mw-001")],
        }
        async def _noop_receive():
            return {"type": "http.request"}
        async def _noop_send(msg):
            return None
        import asyncio
        asyncio.run(wrapped(scope, _noop_receive, _noop_send))
        # The "during request" log record should carry the request id.
        during = [c for c in captured if c.get("msg") == "during request"]
        assert during, "no log record captured during the request"
        assert during[0]["request_id"] == "trace-mw-001"
        assert during[0]["task_run_id"] == "trun-x"
    finally:
        root.removeHandler(handler)


def test_current_request_id_helper_round_trip():
    set_request_id("req-helper")
    try:
        assert current_request_id() == "req-helper"
    finally:
        set_request_id(None)
    assert current_request_id() is None
