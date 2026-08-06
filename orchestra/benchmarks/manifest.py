"""Benchmark Manifest (LIT-005).

P0 freezes one manifest — :data:`DEFAULT_P0_MANIFEST` — that records every
parameter of the experiment. A re-run of the same manifest is
reproducible: the data set, models, prompts, and metric definitions do
not change between runs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orchestra.core.ids import content_addressed_id


@dataclass(frozen=True)
class MetricSpec:
    name: str
    description: str
    higher_is_better: bool = True
    unit: str = ""


@dataclass(frozen=True)
class BaselineSpec:
    """A description of a baseline configuration."""

    name: str
    description: str
    kind: str  # "all-local" | "all-public" | "hybrid"
    fixed_seed: int = 0
    repetitions: int = 1


@dataclass(frozen=True)
class BenchmarkManifest:
    """Frozen benchmark specification.

    P0 stores one of these. The :func:`manifest_id` helper returns a
    content-addressed ID so the audit log can record which manifest
    produced which numbers.
    """

    manifest_name: str
    version: str
    dataset: tuple[str, ...]
    models: dict[str, str]
    prompts: dict[str, str]
    sampling: dict[str, Any]
    price_snapshot: dict[str, float]
    repetitions: int
    metrics: tuple[MetricSpec, ...]
    baselines: tuple[BaselineSpec, ...]
    alpha: float = 0.05
    delta_quality: float = 0.05
    delta_cost: float = 0.10
    budget_usd: float = 5.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_name": self.manifest_name,
            "version": self.version,
            "dataset": list(self.dataset),
            "models": self.models,
            "prompts": self.prompts,
            "sampling": self.sampling,
            "price_snapshot": self.price_snapshot,
            "repetitions": self.repetitions,
            "metrics": [{"name": m.name, "unit": m.unit, "higher_is_better": m.higher_is_better} for m in self.metrics],
            "baselines": [{"name": b.name, "kind": b.kind, "repetitions": b.repetitions} for b in self.baselines],
            "alpha": self.alpha,
            "delta_quality": self.delta_quality,
            "delta_cost": self.delta_cost,
            "budget_usd": self.budget_usd,
        }


def manifest_id(m: BenchmarkManifest) -> str:
    return content_addressed_id("bench", m.to_dict())


# The P0 manifest. Anything that varies between runs (e.g. wall-clock
# latency) is *not* in the manifest.
DEFAULT_P0_MANIFEST = BenchmarkManifest(
    manifest_name="contract-review-p0-v1",
    version="0.1.0",
    dataset=("ctr-001", "ctr-002", "ctr-003"),
    models={
        "local": "deterministic-extractor-v1",
        "public": "demo-openai-compat",
        "a2a": "in-repo-a2a-reference-v1",
    },
    prompts={
        "public-research": (
            "You are a public research assistant. You may only answer using the "
            "facts provided. Do not invent details. Respond in JSON matching the "
            "requested schema."
        ),
    },
    sampling={"temperature": 0.0, "max_tokens": 256, "top_p": 1.0},
    price_snapshot={
        "local": 0.0,
        "public": 0.002,
        "a2a": 0.001,
        "sink": 0.0,
    },
    repetitions=1,
    metrics=(
        MetricSpec("structured_fact_accuracy", "fraction of expected contract facts", True, "%"),
        MetricSpec("public_research_completeness", "fraction of expected public fields", True, "%"),
        MetricSpec("estimated_cost_usd", "cumulative cost in USD", False, "usd"),
        MetricSpec("p50_latency_ms", "median latency", False, "ms"),
        MetricSpec("p95_latency_ms", "p95 latency", False, "ms"),
        MetricSpec("egress_bytes", "bytes leaving the tenant", False, "bytes"),
        MetricSpec("human_interventions", "human-in-the-loop count", False, "count"),
    ),
    baselines=(
        BaselineSpec("all-local", "all processing on local model + merge", "all-local"),
        BaselineSpec("all-public", "all processing on public model (no contract text)", "all-public"),
        BaselineSpec("hybrid", "full Orchestra contract-review", "hybrid"),
    ),
)
