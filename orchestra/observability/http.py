"""M13 — ASGI middleware for HTTP request metrics.

The middleware records:

  * ``orchestra_http_requests_total{method, path, status}`` — a counter
    that ticks once per response (success or error).
  * ``orchestra_http_request_duration_seconds{method, path}`` — a
    histogram of wall-clock seconds between request and response.

``path`` prefers the matched route's template
(``/tasks/{task_run_id}``) so the label cardinality stays bounded
even when callers hit thousands of distinct task_run_ids. When the
ASGI scope has no matched route (404, raw Starlette paths), it
falls back to ``request.url.path``.

The dev impl uses :mod:`time.perf_counter` (monotonic, nanosecond
precision). Production swaps to the prometheus_client middleware or
an OTel HTTP server span without changing the metric *names*.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from orchestra.observability.metrics import Metrics


class HTTPMetricsMiddleware:
    """ASGI middleware that records per-request metrics.

    Wire it onto the FastAPI app via ``app.add_middleware(...)`` or
    wrap the ASGI app in production deployments. The middleware is
    transparent: the response body, status, and headers pass through
    unchanged.
    """

    def __init__(self, app, metrics: Metrics) -> None:
        self.app = app
        self._metrics = metrics
        # Cache the Counter / Histogram handles — the registry
        # returns the same instance for the same name, so this is
        # a single dict lookup per request.
        self._m_count = metrics.counter(
            "orchestra_http_requests_total",
            "Total HTTP requests.",
            labels=("method", "path", "status"),
        )
        self._m_duration = metrics.histogram(
            "orchestra_http_request_duration_seconds",
            "HTTP request duration in seconds.",
            labels=("method", "path"),
        )

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            # Lifespan, websocket, etc. — pass through.
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        start = time.perf_counter()
        # The status code is only known when the application starts
        # the response. We capture it from the first
        # ``http.response.start`` message and let the rest flow.
        captured_status: dict[str, int] = {"status": 500}

        async def send_wrapper(message):
            if message.get("type") == "http.response.start":
                captured_status["status"] = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration = time.perf_counter() - start
            path = self._resolve_path(scope)
            status = str(captured_status["status"])
            self._m_count.inc(method=method, path=path, status=status)
            self._m_duration.observe(duration, method=method, path=path)

    @staticmethod
    def _resolve_path(scope) -> str:
        """Best-effort: prefer the matched route's template.

        Returns ``"/"`` for paths that fall outside the route table
        (404s, raw Starlette paths) so a flood of bad URLs can't
        blow up Prometheus label cardinality.
        """
        route = scope.get("route")
        if route is not None:
            template = getattr(route, "path", None)
            if template:
                return template
        path = scope.get("path", "/")
        return path or "/"
