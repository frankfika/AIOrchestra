"""M9 — Structured logging.

The M7 runbook pins the SLO calculation to real telemetry. The
M9 dev path provides the JSON-logging shape that production
aggregators (ELK / Splunk / Loki) consume.

Two responsibilities:

  * :func:`setup_logging` — install a JSON formatter on the
    root logger. The format includes ``ts``, ``level``, ``logger``,
    ``msg``, and any structured ``extra`` fields the caller
    attaches (request id, task_run_id, etc.).
  * :class:`RequestIdMiddleware` — Starlette middleware that
    assigns a per-request UUID, propagates it as a header, and
    binds it to the logging context for the request's lifetime.

Usage::

    from orchestra.core.logging import setup_logging, RequestIdMiddleware
    setup_logging(level="INFO")
    app.add_middleware(RequestIdMiddleware)
"""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any

# The active request id is propagated through a contextvar so
# log records emitted from any layer (Coordinator, Egress PEP,
# Release Gate) carry the same id.
_request_id: ContextVar[str | None] = ContextVar("orchestra_request_id", default=None)


def current_request_id() -> str | None:
    return _request_id.get()


def set_request_id(rid: str | None) -> None:
    _request_id.set(rid)


class JsonFormatter(logging.Formatter):
    """JSON line formatter for production aggregators."""

    def __init__(self, *, include_extra: bool = True) -> None:
        super().__init__()
        self._include_extra = include_extra

    def format(self, record: logging.LogRecord) -> str:
        # Standard fields the production aggregator always wants.
        body: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # If we're inside a request, carry the request id.
        rid = _request_id.get()
        if rid is not None:
            body["request_id"] = rid
        # Exception info (stack trace) goes into ``exc``.
        if record.exc_info:
            body["exc"] = self.formatException(record.exc_info)
        if self._include_extra:
            # Anything the caller attached via ``logger.info(.., extra={...})``
            # lands here. Skip the standard LogRecord attributes to
            # avoid duplicate noise.
            standard = {
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "message",
                "asctime", "taskName",
            }
            for k, v in record.__dict__.items():
                if k in standard or k.startswith("_"):
                    continue
                body[k] = v
        return json.dumps(body, ensure_ascii=False, default=str)


def setup_logging(*, level: str = "INFO", json_output: bool = True, stream=None) -> None:
    """Install a JSON formatter on the root logger.

    The dev path uses JSON; a test path may pass ``json_output=False``
    to get the human-readable form for a debug session.
    """
    root = logging.getLogger()
    # Remove any existing handlers so re-calling this function
    # (e.g. in a test) does not duplicate output.
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(stream or sys.stderr)
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"
        ))
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # uvicorn's own loggers go through the same handler so the
    # JSON shape is consistent across request and access logs.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True


# ---------------------------------------------------------------------------
# Request id middleware
# ---------------------------------------------------------------------------


class RequestIdMiddleware:
    """ASGI middleware that assigns a per-request UUID.

    The id is read from the ``X-Request-Id`` header if the caller
    supplied one (so an upstream gateway can stitch the trace),
    or generated if not. The id is set in the contextvar, exposed
    on the response header, and logged with every record emitted
    during the request.
    """

    def __init__(self, app) -> None:  # noqa: ANN001
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        rid = headers.get(b"x-request-id", b"").decode("ascii") or f"req-{uuid.uuid4().hex[:12]}"
        token = _request_id.set(rid)
        try:
            # Wrap send to inject the header on the response.
            async def send_with_header(message):
                if message["type"] == "http.response.start":
                    message.setdefault("headers", [])
                    message["headers"] = list(message["headers"]) + [
                        (b"x-request-id", rid.encode("ascii")),
                    ]
                await send(message)
            await self.app(scope, receive, send_with_header)
        finally:
            _request_id.reset(token)
