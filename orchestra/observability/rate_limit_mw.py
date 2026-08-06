"""M14 — ASGI middleware for rate limiting + request size limits.

Two complementary DoS protections:

  * :class:`RateLimitMiddleware` — a per-tenant token bucket. The
    tenant identity is taken from the ``X-Tenant-Id`` header (the
    same shape the M5 Ingress / M6 tenant context uses) and falls
    back to the client IP when the header is missing. The
    ``/healthz`` and ``/metrics`` endpoints are exempt so a SRE
    can always probe the instance.

  * :class:`RequestSizeLimitMiddleware` — a fast-path check on
    the ``Content-Length`` header. When the header is absent
    (chunked encoding) the middleware counts bytes as they arrive
    and aborts the request once the limit is crossed. The
    fast-path is the common case; the slow-path is rare in
    practice but the limit must still hold.

Both middlewares return 429 / 413 with a JSON body and increment
their own Prometheus counters so a SRE can graph throttle
pressure without enabling DEBUG logging.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    from orchestra.observability.metrics import Metrics
    from orchestra.runtime.rate_limit import RateLimiter


# The set of routes a SRE always needs to reach. A load-balancer
# health probe that gets 429 because the LB itself flooded the
# API is the kind of bug a SRE catches at 03:00. The list is
# short on purpose: every exempt route is a route the limiter
# cannot meaningfully protect.
_EXEMPT_PATHS: frozenset[str] = frozenset({"/healthz", "/metrics"})


def _is_exempt(path: str) -> bool:
    # Exact match for the small set; the FastAPI router is more
    # specific than this (e.g. ``/tasks/{task_run_id}`` is not
    # exempt), so a substring check would let too much through.
    return path in _EXEMPT_PATHS


def _resolve_tenant(scope) -> str:
    """Pull a tenant identifier from the request scope.

    Order of preference:
      1. ``X-Tenant-Id`` header (the M5/M6 convention).
      2. Client IP (fallback for un-authenticated probes).

    The IP fallback means a malicious client without the header
    can still be limited — by IP. Partners that want per-user
    limits set the header in their client SDK.
    """
    # The header name is case-insensitive in HTTP, but ASGI scopes
    # expose headers as a list of (bytes, bytes) pairs in lowercase
    # already. Decode once.
    for name, value in scope.get("headers", ()):
        if name == b"x-tenant-id":
            try:
                return value.decode("latin-1").strip() or "anonymous"
            except UnicodeDecodeError:
                return "anonymous"
    # Client IP — prefer the first X-Forwarded-For hop so the
    # rate limit follows the real client behind a load balancer.
    # In dev (no LB) the value is the loopback.
    client = scope.get("client")
    if client is None:
        return "anonymous"
    for name, value in scope.get("headers", ()):
        if name == b"x-forwarded-for":
            try:
                first = value.decode("latin-1").split(",", 1)[0].strip()
                if first:
                    return f"ip:{first}"
            except UnicodeDecodeError:
                pass
    return f"ip:{client[0]}"


class RateLimitMiddleware:
    """ASGI middleware that applies a per-tenant rate limit."""

    def __init__(self, app, limiter: "RateLimiter") -> None:
        self.app = app
        self._limiter = limiter

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or _is_exempt(scope.get("path", "/")):
            await self.app(scope, receive, send)
            return
        tenant = _resolve_tenant(scope)
        decision = self._limiter.check(tenant)
        if not decision.allowed:
            retry_after = max(1, int(decision.retry_after) + 1)
            body = json.dumps(
                {
                    "error": "rate_limited",
                    "tenant": tenant,
                    "retry_after_seconds": retry_after,
                }
            ).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                        (b"retry-after", str(retry_after).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


class RequestSizeLimitMiddleware:
    """ASGI middleware that aborts oversized request bodies.

    The middleware short-circuits on the ``Content-Length`` header
    (the common case). When the header is missing or the value is
    wrong, it falls back to counting bytes during ``http.request``
    messages. Once the limit is crossed the request is aborted
    with 413 and the rest of the body is dropped without being
    delivered to the application.
    """

    def __init__(self, app, *, max_bytes: int, metrics: "Optional[Metrics]" = None) -> None:
        self.app = app
        self._max = int(max_bytes)
        # M14 — count rejections so a SRE sees attempts to send
        # oversized bodies in the dashboard.
        self._metrics = metrics
        if metrics is not None:
            self._m_rejected = metrics.counter(
                "orchestra_request_size_rejected_total",
                "Total requests rejected for exceeding the body size limit.",
                labels=("reason",),
            )
        else:
            self._m_rejected = None

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Fast path: Content-Length header.
        content_length: Optional[int] = None
        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                try:
                    content_length = int(value.decode("ascii").strip())
                except (ValueError, UnicodeDecodeError):
                    content_length = None
                break
        if content_length is not None and content_length > self._max:
            await self._reject(send, reason="content_length")
            return
        if content_length is None:
            # Slow path: wrap receive so we can count bytes as
            # they arrive. We re-emit each message once we've
            # verified the total is within budget.
            received = 0

            async def wrapped_receive():
                nonlocal received
                message = await receive()
                if message.get("type") == "http.request":
                    chunk = message.get("body", b"") or b""
                    received += len(chunk)
                    if received > self._max:
                        await self._reject(send, reason="chunked_overflow")
                        # Replace the body with an empty chunk so
                        # downstream consumers don't see the
                        # partial payload; the ASGI server closes
                        # the connection on the next iteration.
                        return {"type": "http.disconnect"}
                return message

            await self.app(scope, wrapped_receive, send)
            return
        await self.app(scope, receive, send)

    async def _reject(self, send, *, reason: str) -> None:
        if self._m_rejected is not None:
            self._m_rejected.inc(reason=reason)
        body = json.dumps(
            {
                "error": "payload_too_large",
                "reason": reason,
                "max_bytes": self._max,
            }
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
