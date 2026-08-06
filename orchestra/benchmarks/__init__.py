"""LIT-005: Benchmark Manifest + three baselines.

The Benchmark Manifest is **frozen** before any run (plan §P0 Gate). It
records the dataset, model/agent versions, prompts, sampling parameters,
price snapshots, repetition count, scorer, δ/α/Δ thresholds, and the
Budget. The manifest is content-addressed so a re-run of the same
manifest always produces the same numbers (modulo wall-clock latency).

The three baselines:

- ``all-local``     : the entire review is done by the Local Model +
                      deterministic merge. No public calls, no A2A. The
                      Approval point is preserved so we measure one
                      human-in-the-loop variant. Egress bytes = 0.
- ``all-public``    : the entire review is done by the Public Adapter
                      using only the public vendor_id (the contract
                      text is never sent — invariant #1). No contract
                      facts, only public registry lookups. Egress bytes
                      = 0 for the contract text; public traffic only.
- ``hybrid``        : the full Orchestra Contract Review path.

Metrics reported:

- ``structured_fact_accuracy``  : % of expected facts the baseline
                                  produced. For ``all-public`` this is
                                  bounded by what the public registry
                                  contains.
- ``public_research_completeness`` : % of expected public fields.
- ``estimated_cost_usd``
- ``p50_latency_ms``, ``p95_latency_ms``
- ``egress_bytes``                : bytes that left the tenant
- ``human_interventions``         : count

The ``paretok`` verdict is True if ``hybrid`` is *not* dominated by both
``all-local`` and ``all-public`` (Pareto front on the
{cost, latency, fact accuracy, research completeness, egress, human}
axes).
"""
from orchestra.benchmarks.manifest import (
    DEFAULT_P0_MANIFEST,
    BaselineSpec,
    BenchmarkManifest,
    MetricSpec,
    manifest_id,
)
from orchestra.benchmarks.runner import (
    GROUND_TRUTH,
    BaselineResult,
    BenchmarkResult,
    BenchmarkRunner,
    build_all_local_runner,
    build_all_public_runner,
    build_hybrid_runner,
)

__all__ = [
    "BenchmarkManifest",
    "BaselineSpec",
    "MetricSpec",
    "DEFAULT_P0_MANIFEST",
    "manifest_id",
    "BenchmarkRunner",
    "BenchmarkResult",
    "BaselineResult",
    "build_hybrid_runner",
    "build_all_local_runner",
    "build_all_public_runner",
    "GROUND_TRUTH",
]
