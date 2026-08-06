"""M13 — Prometheus metrics registry.

A minimal, dependency-free Prometheus text-format exporter.
The dev path is fast (one allocation per metric) and good
enough for a single-process SRE. The production swap plugs in
``prometheus_client`` or OpenTelemetry without changing the
metric *names* the SLO calculator and Grafana dashboards expect.

Format reference: https://prometheus.io/docs/instrumenting/exposition_formats/#text-based-format
"""

from __future__ import annotations

import math
from collections import defaultdict
from threading import Lock
from typing import Any


class Counter:
    """A monotonically-increasing counter. ``inc(n=1)`` adds ``n``."""

    def __init__(self, name: str, help: str, labels: tuple[str, ...] = ()) -> None:
        self._name = name
        self._help = help
        self._labels = labels
        self._values: dict[tuple[str, ...], float] = defaultdict(float)
        self._lock = Lock()

    def inc(self, n: float = 1.0, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] += n

    def _key(self, labels: dict[str, str]) -> tuple[str, ...]:
        return tuple(labels.get(label, "") for label in self._labels)

    def render(self) -> str:
        out = [f"# HELP {self._name} {self._help}", f"# TYPE {self._name} counter"]
        for key, value in self._values.items():
            labels = self._format_labels(key)
            out.append(f"{self._name}{labels} {_fmt(value)}")
        return "\n".join(out) + "\n"

    def _format_labels(self, key: tuple[str, ...]) -> str:
        if not self._labels:
            return ""
        parts = [f'{name}="{_escape(value)}"' for name, value in zip(self._labels, key) if value]
        return "{" + ",".join(parts) + "}"


class Gauge:
    """A value that can go up or down (e.g. queue depth, SLO attainment)."""

    def __init__(self, name: str, help: str, labels: tuple[str, ...] = ()) -> None:
        self._name = name
        self._help = help
        self._labels = labels
        self._values: dict[tuple[str, ...], float] = {}
        self._lock = Lock()

    def set(self, value: float, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = value

    def _key(self, labels: dict[str, str]) -> tuple[str, ...]:
        return tuple(labels.get(label, "") for label in self._labels)

    def render(self) -> str:
        out = [f"# HELP {self._name} {self._help}", f"# TYPE {self._name} gauge"]
        for key, value in self._values.items():
            labels = self._format_labels(key)
            out.append(f"{self._name}{labels} {_fmt(value)}")
        return "\n".join(out) + "\n"

    def _format_labels(self, key: tuple[str, ...]) -> str:
        if not self._labels:
            return ""
        parts = [f'{name}="{_escape(value)}"' for name, value in zip(self._labels, key) if value]
        return "{" + ",".join(parts) + "}"


class Histogram:
    """A simple histogram with a fixed bucket set.

    The dev impl uses the Prometheus default buckets
    (5ms, 10ms, 25ms, 50ms, 100ms, 250ms, 500ms, 1s, 2.5s, 5s, 10s)
    plus a ``+Inf`` bucket. The M8 perf test exercises this
    histogram so the buckets are tuned for Orchestra's hot paths.
    """

    DEFAULT_BUCKETS: tuple[float, ...] = (
        0.005,
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        1.0,
        2.5,
        5.0,
        10.0,
    )

    def __init__(
        self,
        name: str,
        help: str,
        labels: tuple[str, ...] = (),
        buckets: tuple[float, ...] = DEFAULT_BUCKETS,
    ) -> None:
        self._name = name
        self._help = help
        self._labels = labels
        self._buckets = buckets
        self._counts: dict[tuple[str, ...], dict[float, int]] = defaultdict(
            lambda: {b: 0 for b in buckets}
        )
        self._sums: dict[tuple[str, ...], float] = defaultdict(float)
        self._totals: dict[tuple[str, ...], int] = defaultdict(int)
        self._lock = Lock()

    def observe(self, value: float, **labels: str) -> None:
        """Record one observation.

        Only the **first** bucket the value fits in gets incremented;
        the cumulative sum is computed at :meth:`render` time so a
        bucket's count is "how many observations fell into this
        bucket" rather than "how many observations also fell into
        every larger bucket". This matches the Prometheus client
        library's behavior.
        """
        key = self._key(labels)
        with self._lock:
            for b in self._buckets:
                if value <= b:
                    self._counts[key][b] += 1
                    break
            self._sums[key] += value
            self._totals[key] += 1

    def _key(self, labels: dict[str, str]) -> tuple[str, ...]:
        return tuple(labels.get(label, "") for label in self._labels)

    def render(self) -> str:
        out = [f"# HELP {self._name} {self._help}", f"# TYPE {self._name} histogram"]
        # Iterate over every key that has been observed, not just
        # those that hit a bucket. A value larger than the largest
        # bucket still needs a ``_count`` + ``_sum`` line so a
        # scraper can compute the average.
        for key, total in self._totals.items():
            counts = self._counts.get(key, {})
            cumulative = 0
            for b in self._buckets:
                cumulative += counts.get(b, 0)
                labels = self._format_labels(key, le=b)
                out.append(f"{self._name}_bucket{labels} {cumulative}")
            labels = self._format_labels(key, le="+Inf")
            out.append(f"{self._name}_bucket{labels} {total}")
            labels = self._format_labels(key, le="")
            out.append(f"{self._name}_sum{labels} {_fmt(self._sums[key])}")
            out.append(f"{self._name}_count{labels} {total}")
        return "\n".join(out) + "\n"

    def _format_labels(self, key: tuple[str, ...], le: Any) -> str:
        parts = []
        for name, value in zip(self._labels, key):
            if value:
                parts.append(f'{name}="{_escape(value)}"')
        if le == "":
            pass
        elif le == "+Inf":
            parts.append('le="+Inf"')
        else:
            parts.append(f'le="{_fmt(le)}"')
        return "{" + ",".join(parts) + "}" if parts else ""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class Metrics:
    """The in-memory metrics registry.

    A new instance is created per process; the FastAPI app holds
    it on the AppState so the ``/metrics`` route can render it.
    """

    def __init__(self) -> None:
        self._counters: dict[str, Counter] = {}
        self._gauges: dict[str, Gauge] = {}
        self._histograms: dict[str, Histogram] = {}

    def counter(self, name: str, help: str, labels: tuple[str, ...] = ()) -> Counter:
        if name not in self._counters:
            self._counters[name] = Counter(name, help, labels)
        return self._counters[name]

    def gauge(self, name: str, help: str, labels: tuple[str, ...] = ()) -> Gauge:
        if name not in self._gauges:
            self._gauges[name] = Gauge(name, help, labels)
        return self._gauges[name]

    def histogram(
        self,
        name: str,
        help: str,
        labels: tuple[str, ...] = (),
        buckets: tuple[float, ...] = Histogram.DEFAULT_BUCKETS,
    ) -> Histogram:
        if name not in self._histograms:
            self._histograms[name] = Histogram(name, help, labels, buckets)
        return self._histograms[name]


def render_prometheus(m: Metrics) -> str:
    """Render the registry as a Prometheus text payload.

    The output is the standard ``text/plain; version=0.0.4``
    exposition format. A SRE scraper consumes the body
    verbatim; the optional ``# EOF`` trailer is not required
    and we omit it.
    """
    parts: list[str] = []
    for c in m._counters.values():  # noqa: SLF001
        parts.append(c.render())
    for g in m._gauges.values():  # noqa: SLF001
        parts.append(g.render())
    for h in m._histograms.values():  # noqa: SLF001
        parts.append(h.render())
    return "".join(parts)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt(value: float) -> str:
    """Format a float the Prometheus way. NaN and Inf are allowed."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:g}"


def _escape(value: str) -> str:
    """Escape a label value the Prometheus way."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


# ---------------------------------------------------------------------------
# Built-in metrics (M13)
# ---------------------------------------------------------------------------


def builtin_metrics() -> Metrics:
    """The M13-builtin metric set.

    These are the metrics a Grafana dashboard or SRE runbook
    would expect to see. Callers may register more on the
    same instance.
    """
    m = Metrics()
    # Process-level
    m.gauge("orchestra_up", "1 if the process is alive and serving requests.")
    # HTTP request totals
    m.counter(
        "orchestra_http_requests_total", "Total HTTP requests.", labels=("method", "path", "status")
    )
    m.histogram(
        "orchestra_http_request_duration_seconds",
        "HTTP request duration in seconds.",
        labels=("method", "path"),
    )
    # M3 Egress PEP
    m.counter(
        "orchestra_egress_pep_projection_total",
        "Total EgressPEP projections.",
        labels=("capability", "view"),
    )
    m.counter(
        "orchestra_egress_pep_denied_total",
        "Total EgressPEP denials.",
        labels=("capability", "view"),
    )
    m.histogram(
        "orchestra_egress_pep_projection_bytes", "Projected payload bytes.", labels=("capability",)
    )
    # M5 Publishing
    m.counter("orchestra_publish_published_total", "Total Agent Cards published.")
    m.counter("orchestra_publish_revoked_total", "Total Agent Cards revoked.")
    m.counter("orchestra_ingress_admit_total", "Total Ingress.admit calls.", labels=("outcome",))
    m.counter(
        "orchestra_release_gate_denied_total", "Total ReleaseGate denials.", labels=("reason",)
    )
    # M6 Multi-tenant
    m.gauge("orchestra_tenants_total", "Total tenants in the multi-tenant store.")
    m.gauge("orchestra_published_cards_total", "Total published Agent Cards.")
    m.gauge("orchestra_capabilities_total", "Total registered capabilities.")
    return m
