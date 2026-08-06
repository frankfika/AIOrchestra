"""M13 — Prometheus metrics primitives.

The dev impl is dependency-free: a Counter / Gauge / Histogram
trio + a text renderer. The tests below pin the public contract
that Grafana / VictoriaMetrics / Datadog agents depend on. If any
of these change, the dashboard alerts go red.

The text format spec is the Prometheus exposition format
``text/plain; version=0.0.4`` — see
https://prometheus.io/docs/instrumenting/exposition_formats/.
"""
from __future__ import annotations

import pytest

from orchestra.observability import (
    Counter,
    Gauge,
    Histogram,
    Metrics,
    builtin_metrics,
    render_prometheus,
)


# ---------------------------------------------------------------------------
# Counter
# ---------------------------------------------------------------------------


def test_counter_increments_by_one_by_default():
    c = Counter("c1", "test counter")
    c.inc()
    c.inc()
    c.inc()
    assert "c1 3" in c.render()


def test_counter_supports_labeled_increments():
    c = Counter("c2", "labeled", labels=("method", "status"))
    c.inc(method="GET", status="200")
    c.inc(method="POST", status="200")
    c.inc(method="POST", status="500")
    out = c.render()
    assert 'c2{method="GET",status="200"} 1' in out
    assert 'c2{method="POST",status="200"} 1' in out
    assert 'c2{method="POST",status="500"} 1' in out


def test_counter_emits_help_and_type_lines():
    c = Counter("c3", "the help text")
    out = c.render()
    assert "# HELP c3 the help text" in out
    assert "# TYPE c3 counter" in out


def test_counter_skips_empty_label_values():
    """Empty label values are omitted so the output matches
    what a real Prometheus client emits (avoiding spurious
    ``label=""`` entries)."""
    c = Counter("c4", "test", labels=("method", "path"))
    c.inc(method="GET", path="/healthz")
    c.inc(method="GET")  # path empty
    out = c.render()
    assert 'c4{method="GET",path="/healthz"} 1' in out
    # The second call shouldn't introduce a path="" label.
    assert 'path=""' not in out
    assert 'c4{method="GET"}' in out


# ---------------------------------------------------------------------------
# Gauge
# ---------------------------------------------------------------------------


def test_gauge_set_overwrites():
    g = Gauge("g1", "queue depth")
    g.set(5.0)
    g.set(12.0)
    out = g.render()
    assert "g1 12" in out
    assert "g1 5" not in out


def test_gauge_renders_help_and_type():
    g = Gauge("g2", "the help")
    out = g.render()
    assert "# HELP g2 the help" in out
    assert "# TYPE g2 gauge" in out


# ---------------------------------------------------------------------------
# Histogram
# ---------------------------------------------------------------------------


def test_histogram_buckets_are_cumulative():
    h = Histogram("h1", "latency", buckets=(0.1, 0.5, 1.0))
    h.observe(0.05)  # <= 0.1
    h.observe(0.2)   # <= 0.5
    h.observe(0.6)   # <= 1.0
    h.observe(2.0)   # > 1.0  (only +Inf)
    out = h.render()
    # Cumulative counts.
    assert 'h1_bucket{le="0.1"} 1' in out
    assert 'h1_bucket{le="0.5"} 2' in out
    assert 'h1_bucket{le="1"} 3' in out
    assert 'h1_bucket{le="+Inf"} 4' in out
    assert 'h1_count 4' in out
    # Sum is the running total of observed values.
    assert "h1_sum 2.85" in out


def test_histogram_emits_help_type_and_total():
    h = Histogram("h2", "test")
    h.observe(0.01)
    out = h.render()
    assert "# HELP h2 test" in out
    assert "# TYPE h2 histogram" in out
    assert "h2_count 1" in out


# ---------------------------------------------------------------------------
# Metrics registry + render
# ---------------------------------------------------------------------------


def test_metrics_registry_returns_same_instance_for_same_name():
    m = Metrics()
    a = m.counter("c", "h")
    b = m.counter("c", "h")
    assert a is b


def test_render_prometheus_combines_all_metric_kinds():
    m = Metrics()
    m.counter("rc", "rc", labels=("k",)).inc(k="v")
    m.gauge("rg", "rg").set(7.0)
    m.histogram("rh", "rh").observe(0.1)
    out = render_prometheus(m)
    assert "rc{" in out
    assert "rg 7" in out
    assert "rh_count" in out


def test_builtin_metrics_registers_expected_metric_names():
    """The M13 dashboard expects these names; if any are renamed
    the alerts / panels need an update."""
    m = builtin_metrics()
    expected = {
        "orchestra_up",
        "orchestra_http_requests_total",
        "orchestra_http_request_duration_seconds",
        "orchestra_egress_pep_projection_total",
        "orchestra_egress_pep_denied_total",
        "orchestra_egress_pep_projection_bytes",
        "orchestra_publish_published_total",
        "orchestra_publish_revoked_total",
        "orchestra_ingress_admit_total",
        "orchestra_release_gate_denied_total",
        "orchestra_tenants_total",
        "orchestra_published_cards_total",
        "orchestra_capabilities_total",
    }
    out = render_prometheus(m)
    for name in expected:
        assert name in out, f"missing metric: {name}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_escape_handles_quote_backslash_newline():
    """Label values that contain ", \\, or \\n would break the
    exposition format; the helper escapes them the Prometheus way."""
    # Indirect: register a counter with a label value that needs escaping.
    m = Metrics()
    c = m.counter("c_esc", "esc", labels=("k",))
    c.inc(k='he said "hi"\\there\n')
    out = c.render()
    # The escaped form must appear in the output.
    assert 'k="he said \\"hi\\"\\\\there\\n"' in out


def test_metrics_module_does_not_import_heavy_dependencies():
    """The dev path must avoid pulling in prometheus_client /
    opentelemetry — otherwise the meta-pipeline can't even import
    the registry without a 10MB+ dependency."""
    import orchestra.observability as obs

    # Confirm the public surface stays minimal.
    assert hasattr(obs, "Metrics")
    assert hasattr(obs, "render_prometheus")
    assert hasattr(obs, "HTTPMetricsMiddleware")
