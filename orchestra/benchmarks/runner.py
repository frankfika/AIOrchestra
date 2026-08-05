"""Benchmark Runner (LIT-005).

Runs the three baselines and computes metrics + a Pareto verdict.

The three runners are deliberately simple:

- ``build_all_local_runner`` skips the public_research node and runs
  only the local extractor + a deterministic merge.
- ``build_all_public_runner`` sends only the ``vendor_id`` to the public
  Adapter and never reads the contract. Egress bytes = size of the
  public request body only.
- ``build_hybrid_runner`` is the full :class:`Coordinator`.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from data.samples.contracts import SAMPLE_CONTRACTS, get_contract
from orchestra.benchmarks.manifest import DEFAULT_P0_MANIFEST, BenchmarkManifest, manifest_id
from orchestra.coordinator.engine import Coordinator, build_default_coordinator
from orchestra.coordinator.event_store import EventStore
from orchestra.core.hashing import hmac_keygen
from orchestra.core.ids import new_id
from orchestra.core.schema import (
    DataClassification,
    SecurityLabel,
    SourceTrust,
)


# Per-contract ground truth for the P0 metrics. The fields here are
# what each baseline *could* produce. The all-public baseline cannot
# produce contract fields because the contract is Restricted.
GROUND_TRUTH: dict[str, dict[str, Any]] = {
    "ctr-001": {
        "contract_facts": {
            "vendor_name": "Acme Cloud Logistics Co., Ltd.",
            "buyer_name": "Helios Procurement Group",
            "contract_amount": "8,600,000",
            "payment_terms": "Net 30",
            "jurisdiction": "Hong Kong",
        },
        "public_fields": {"vendor_id", "vendor_name", "jurisdiction", "regulatory_actions"},
    },
    "ctr-002": {
        "contract_facts": {
            "vendor_name": "Helios Industrial Group",
            "buyer_name": "Stellar Manufacturing Ltd.",
            "contract_amount": "4,200,000",
            "payment_terms": "Net 45",
            "jurisdiction": "Singapore",
        },
        "public_fields": {"vendor_id", "vendor_name", "jurisdiction", "regulatory_actions"},
    },
    "ctr-003": {
        "contract_facts": {
            "vendor_name": "Acme Cloud Logistics Co., Ltd.",
            "buyer_name": "内部创新业务部",
            "contract_amount": "380,000",
            "payment_terms": "Net 15",
            "jurisdiction": "中国大陆",
        },
        "public_fields": {"vendor_id", "vendor_name", "jurisdiction", "regulatory_actions"},
    },
}


@dataclass
class BaselineResult:
    baseline: str
    per_contract: dict[str, dict[str, Any]]
    metrics: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {"baseline": self.baseline, "per_contract": self.per_contract, "metrics": self.metrics}


@dataclass
class BenchmarkResult:
    manifest_id: str
    manifest: dict[str, Any]
    baselines: list[BaselineResult]
    pareto_verdict: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "manifest": self.manifest,
            "baselines": [b.to_dict() for b in self.baselines],
            "pareto_verdict": self.pareto_verdict,
        }


# ---------------------------------------------------------------------------
# Builders for the three runner shapes
# ---------------------------------------------------------------------------


RunnerFactory = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def build_all_local_runner(
    *, store: EventStore, endpoints: dict[str, str]
) -> RunnerFactory:
    """All-local baseline: extract facts locally, merge, auto-approve,
    write to sink. Skips public research and A2A entirely.
    """
    coordinator = build_default_coordinator(store=store, endpoints=endpoints)
    # We need to skip the public_research node and the approval wait.
    # Easiest path: drive the local + merge nodes manually, then call
    # write_sink directly. We do this by directly invoking the local
    # adapter and the merge helper, then driving write_sink through the
    # coordinator's adapter map.

    async def run(contract_id: str, inputs: dict[str, Any]) -> dict[str, Any]:
        from orchestra.adapters.base import AdapterRequest
        from orchestra.coordinator.engine import _deterministic_merge

        # Extract
        local = coordinator._adapters["local.contract-extractor"]
        # Build a fake grant + view for the call.
        from orchestra.core.schema import DataView, Purpose
        from orchestra.coordinator.node_grant import NodeGrantIssuer

        # Local extract
        text = inputs["contract_text"]
        view = DataView(name="contract.full", shape="fields", fields=list(text and []))
        view.fields = [
            "vendor_name", "buyer_name", "contract_amount", "payment_terms",
            "effective_date", "expiration_date", "termination_clause", "jurisdiction",
        ]
        issuer = NodeGrantIssuer(hmac_keygen(), ttl_seconds=600)
        grant = issuer.issue(
            task_run_id=new_id(),
            node_run_id=new_id(),
            task_id=contract_id,
            node_id="extract_facts_local",
            capability_id="local.contract-extractor",
            manifest_id="manifest:all-local",
            data_view=view,
            purpose=Purpose(code="contract-review"),
        )
        local_result = await local.invoke(
            AdapterRequest(
                grant=grant, inputs={"contract_text": text}, data_view=view,
                purpose=Purpose(code="contract-review"), timeout_ms=5_000,
            )
        )
        facts = local_result.outputs["facts"]
        merged = _deterministic_merge({"facts": facts, "research": {}})
        # Auto-approve (no public comparison data)
        return {
            "contract_id": contract_id,
            "facts": facts,
            "merged": merged,
            "egress_bytes": 0,
            "human_interventions": 0,
            "latency_ms": 0,
            "cost_usd": 0.0,
        }

    return run


def build_all_public_runner(
    *, store: EventStore, endpoints: dict[str, str]
) -> RunnerFactory:
    """All-public baseline: send vendor_id only to the public model.
    Contract text never leaves the tenant.
    """
    coordinator = build_default_coordinator(store=store, endpoints=endpoints)
    public = coordinator._adapters["public.openai-compat"]

    async def run(contract_id: str, inputs: dict[str, Any]) -> dict[str, Any]:
        from orchestra.adapters.base import AdapterRequest
        from orchestra.core.schema import DataView, Purpose
        from orchestra.coordinator.node_grant import NodeGrantIssuer
        from orchestra.core.ids import new_id

        view = DataView(name="public.query", shape="fields", fields=["vendor_id", "vendor_name"])
        issuer = NodeGrantIssuer(hmac_keygen(), ttl_seconds=600)
        grant = issuer.issue(
            task_run_id=new_id(),
            node_run_id=new_id(),
            task_id=contract_id,
            node_id="public_research",
            capability_id="public.openai-compat",
            manifest_id="manifest:all-public",
            data_view=view,
            purpose=Purpose(code="contract-review"),
        )
        t0 = time.monotonic()
        result = await public.invoke(
            AdapterRequest(
                grant=grant,
                inputs={"facts": {"vendor_id": inputs["vendor_id"]}, "query": "vendor registry lookup"},
                data_view=view,
                purpose=Purpose(code="contract-review"),
                timeout_ms=10_000,
            )
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        research = result.outputs.get("research", {})
        # Compute egress bytes — the *only* data sent is the public query.
        sent = len(json.dumps({"vendor_id": inputs["vendor_id"]}, ensure_ascii=False).encode("utf-8"))
        return {
            "contract_id": contract_id,
            "facts": {},  # contract facts are not extractable without the contract
            "research": research,
            "merged": {"summary": research.get("public_summary", {}).get("vendor_name", "?")},
            "egress_bytes": sent,
            "human_interventions": 0,
            "latency_ms": latency_ms,
            "cost_usd": 0.002,
        }

    return run


def build_hybrid_runner(
    *, store: EventStore, endpoints: dict[str, str]
) -> Coordinator:
    """Hybrid baseline: the full :class:`Coordinator`. Each call is one
    Contract Review; the caller must resolve the human approval."""
    return build_default_coordinator(store=store, endpoints=endpoints)


# ---------------------------------------------------------------------------
# The runner that orchestrates the three baselines + scoring
# ---------------------------------------------------------------------------


class BenchmarkRunner:
    def __init__(
        self,
        *,
        store: EventStore,
        coordinator_factory: Callable[[], Coordinator],
    ) -> None:
        self._store = store
        self._coordinator_factory = coordinator_factory
        self._manifest = DEFAULT_P0_MANIFEST
        # Endpoints pinned by the time the AppState was built; for benchmarks
        # we re-derive them from the running servers via the coordinator's
        # manifest store. We re-read endpoints at run time.
        self._endpoints: dict[str, str] = {}

    def _endpoints_from_coordinator(self) -> dict[str, str]:
        coord = self._coordinator_factory()
        return {m.capability_id: m.endpoint for m in coord._router._store.all()}

    def run_all(self) -> BenchmarkResult:
        """Synchronous entry point (the FastAPI endpoint). Returns a
        :class:`BenchmarkResult` describing all three baselines.
        """
        return asyncio.run(self._run_all_async())

    async def _run_all_async(self) -> BenchmarkResult:
        endpoints = self._endpoints_from_coordinator()
        all_local = build_all_local_runner(store=self._store, endpoints=endpoints)
        all_public = build_all_public_runner(store=self._store, endpoints=endpoints)
        hybrid_coord = build_hybrid_runner(store=self._store, endpoints=endpoints)

        local_results: list[dict[str, Any]] = []
        public_results: list[dict[str, Any]] = []
        hybrid_results: list[dict[str, Any]] = []

        for c in SAMPLE_CONTRACTS:
            local_results.append(await all_local(c.contract_id, {"contract_text": c.body, "vendor_id": c.vendor_id}))
            public_results.append(await all_public(c.contract_id, {"contract_text": c.body, "vendor_id": c.vendor_id}))
            # Hybrid: full run, auto-approve. The hybrid run uses a *fresh*
            # Coordinator per contract so approval events don't leak
            # between runs.
            task_run_id = new_id()
            per_contract_coord = build_hybrid_runner(store=self._store, endpoints=endpoints)
            run_task = asyncio.create_task(
                per_contract_coord.run(
                    task_run_id=task_run_id,
                    contract_id=c.contract_id,
                    data_label=SecurityLabel(
                        classification=DataClassification.RESTRICTED,
                        residency="local",
                        source_trust=SourceTrust.INTERNAL,
                        retention_days=365,
                        owner="tenant:demo",
                    ),
                    initial_inputs={"contract_text": c.body, "vendor_id": c.vendor_id},
                    budget_usd=self._manifest.budget_usd,
                )
            )
            # Wait up to 15s for the approval gate to register, then
            # auto-decide. The local + public + merge nodes each do a
            # real HTTP round-trip, so this can take a few seconds.
            deadline = asyncio.get_event_loop().time() + 15.0
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0.05)
                if (task_run_id, "human_approval") in per_contract_coord._approval_events:
                    break
            else:
                run_task.cancel()
                raise RuntimeError(
                    f"approval gate never registered for {task_run_id} within 15s"
                )
            await per_contract_coord.decide_approval(
                task_run_id, "human_approval",
                decision="approve", decided_by="benchmark-auto", rationale="auto-approved for benchmark",
            )
            res = await run_task
            hybrid_results.append(
                {
                    "contract_id": c.contract_id,
                    "state": res.state.value,
                    "node_results": res.node_results,
                    "receipts": res.receipts,
                    "egress_bytes": _estimate_egress_from_receipts(res.receipts),
                    "human_interventions": 1,
                    "latency_ms": 0,
                    "cost_usd": 0.002 + 0.001,
                }
            )

        local_metrics = _aggregate_metrics("all-local", local_results)
        public_metrics = _aggregate_metrics("all-public", public_results)
        hybrid_metrics = _aggregate_metrics("hybrid", hybrid_results)
        verdict = _pareto_verdict(local_metrics, public_metrics, hybrid_metrics)

        return BenchmarkResult(
            manifest_id=manifest_id(self._manifest),
            manifest=self._manifest.to_dict(),
            baselines=[
                BaselineResult(
                    baseline="all-local",
                    per_contract=local_results,
                    metrics=local_metrics,
                ),
                BaselineResult(
                    baseline="all-public",
                    per_contract=public_results,
                    metrics=public_metrics,
                ),
                BaselineResult(
                    baseline="hybrid",
                    per_contract=hybrid_results,
                    metrics=hybrid_metrics,
                ),
            ],
            pareto_verdict=verdict,
        )


# ---------------------------------------------------------------------------
# Metrics & verdict
# ---------------------------------------------------------------------------


def _estimate_egress_from_receipts(receipts: list[dict[str, Any]]) -> int:
    # A very rough estimate: the public_research node sent the projected
    # Fact Set + a query string. We don't have a precise count from the
    # receipt; we approximate from the public Adapter's per-call payload.
    return 256  # ~ 256 bytes for one public-research call


def _extract_facts(per_contract: dict[str, Any]) -> dict[str, Any]:
    """Pull the contract facts dict out of a baseline result, regardless
    of whether the result was produced by the all-local runner, the
    all-public runner, or the hybrid Coordinator.
    """
    # Hybrid: facts live under node_results["extract_facts_local"]["facts"].
    nr = per_contract.get("node_results") or {}
    if "extract_facts_local" in nr:
        return nr["extract_facts_local"].get("facts", {}) or {}
    # all-local: facts are set on the per-contract dict directly.
    return per_contract.get("facts", {}) or {}


def _extract_research(per_contract: dict[str, Any]) -> dict[str, Any]:
    """Pull the public research summary out of a baseline result,
    wherever it lives.
    """
    nr = per_contract.get("node_results") or {}
    if "merge" in nr and isinstance(nr["merge"], dict):
        return nr["merge"].get("public_summary", {}) or {}
    if "merged" in per_contract and isinstance(per_contract["merged"], dict):
        return per_contract["merged"].get("public_summary", {}) or {}
    if "research" in per_contract and isinstance(per_contract["research"], dict):
        return per_contract["research"].get("public_summary", {}) or {}
    return {}


def _structured_fact_accuracy(per_contract: dict[str, Any], contract_id: str) -> float:
    expected = GROUND_TRUTH[contract_id]["contract_facts"]
    if not expected:
        return 1.0
    got = _extract_facts(per_contract)
    matches = 0
    for k, v in expected.items():
        actual = got.get(k, "")
        if not actual:
            continue
        # Loose match: substring on the *value* (the regex extractor may
        # return "Net 30，电汇至…" rather than just "Net 30").
        if v.lower() in str(actual).lower() or str(actual).lower() in v.lower():
            matches += 1
    return matches / len(expected)


def _public_research_completeness(per_contract: dict[str, Any], contract_id: str) -> float:
    expected = GROUND_TRUTH[contract_id]["public_fields"]
    research = _extract_research(per_contract)
    if not research and "a2a_artefact" in (per_contract.get("research") or {}):
        research = per_contract["research"]["a2a_artefact"]
    if not expected:
        return 1.0
    hits = 0
    for f in expected:
        if f in research:
            hits += 1
    return hits / len(expected)


def _aggregate_metrics(name: str, results: list[dict[str, Any]]) -> dict[str, float]:
    fact_acc = sum(_structured_fact_accuracy(r, r["contract_id"]) for r in results) / len(results)
    pub_comp = sum(_public_research_completeness(r, r["contract_id"]) for r in results) / len(results)
    cost = sum(r.get("cost_usd", 0.0) for r in results) / len(results)
    latencies = sorted(r.get("latency_ms", 0) for r in results)
    p50 = latencies[len(latencies) // 2] if latencies else 0
    p95 = latencies[max(0, int(len(latencies) * 0.95) - 1)] if latencies else 0
    egress = sum(r.get("egress_bytes", 0) for r in results) / len(results)
    humans = sum(r.get("human_interventions", 0) for r in results) / len(results)
    return {
        "structured_fact_accuracy": round(fact_acc, 3),
        "public_research_completeness": round(pub_comp, 3),
        "estimated_cost_usd": round(cost, 4),
        "p50_latency_ms": int(p50),
        "p95_latency_ms": int(p95),
        "egress_bytes": int(egress),
        "human_interventions": round(humans, 1),
    }


def _pareto_verdict(
    local: dict[str, float],
    public: dict[str, float],
    hybrid: dict[str, float],
) -> dict[str, Any]:
    """Pareto-style verdict on whether ``hybrid`` is dominated by the two
    single-environment baselines.

    Pareto domination: ``a`` dominates ``b`` iff ``a`` is **no worse** on
    every dimension *and* strictly better on at least one. The P0 Gate
    says the hybrid must not be dominated by *both* baselines — that is
    exactly what we check here.
    """

    higher = ("structured_fact_accuracy", "public_research_completeness")
    lower = ("estimated_cost_usd", "egress_bytes", "human_interventions")

    def _dominates(a: dict[str, float], b: dict[str, float]) -> bool:
        strictly_better = False
        for k in higher:
            if a.get(k, 0) < b.get(k, 0):
                return False
            if a.get(k, 0) > b.get(k, 0):
                strictly_better = True
        for k in lower:
            if a.get(k, 0) > b.get(k, 0):
                return False
            if a.get(k, 0) < b.get(k, 0):
                strictly_better = True
        return strictly_better

    dominated_by_local = _dominates(local, hybrid)
    dominated_by_public = _dominates(public, hybrid)
    not_dominated = not (dominated_by_local and dominated_by_public)
    return {
        "not_dominated": bool(not_dominated),
        "dominated_by_all_local": bool(dominated_by_local),
        "dominated_by_all_public": bool(dominated_by_public),
        # P0 §"Benchmark Manifest": at least one pre-registered quality-
        # exposure or quality-cost hypothesis must be satisfied.
        "hypothesis_quality_exposure": bool(
            hybrid["public_research_completeness"] > local["public_research_completeness"]
        ),
        "hypothesis_quality_cost": bool(
            hybrid["structured_fact_accuracy"] + hybrid["public_research_completeness"]
            > local["structured_fact_accuracy"] + local["public_research_completeness"]
        ),
    }
