"""M13 — Production observability.

The M7 SLO calculator consumes pilot telemetry. The Prometheus
metrics endpoint exports the same shape in the standard
``text/plain; version=0.0.4`` format so any production scraper
(Grafana / Datadog / VictoriaMetrics) consumes it without a
custom adapter.

Exports:

  * :class:`Metrics` — the in-memory counter / gauge / histogram
    registry. Production swap plugs in a real Prometheus client
    (or OpenTelemetry); the dev impl is a tiny text formatter
    that avoids a hard dependency.
  * :func:`render_prometheus` — render the registry as a
    Prometheus text payload.
  * :class:`HTTPMetricsMiddleware` — ASGI middleware that records
    ``orchestra_http_requests_total`` and
    ``orchestra_http_request_duration_seconds``.
"""

from orchestra.observability.http import HTTPMetricsMiddleware
from orchestra.observability.metrics import (
    Counter,
    Gauge,
    Histogram,
    Metrics,
    builtin_metrics,
    render_prometheus,
)

__all__ = [
    "Counter",
    "Gauge",
    "Histogram",
    "Metrics",
    "builtin_metrics",
    "render_prometheus",
    "HTTPMetricsMiddleware",
]
