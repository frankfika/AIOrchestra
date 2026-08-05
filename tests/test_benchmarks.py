"""Benchmark runner test — runs the three baselines, asserts the
Pareto-not-dominated verdict.
"""
from __future__ import annotations

import os
import uuid

import pytest

from orchestra.adapters.servers import start_all_servers
from orchestra.benchmarks.runner import BenchmarkRunner
from orchestra.coordinator.engine import build_default_coordinator
from orchestra.coordinator.event_store import EventStore


pytestmark = pytest.mark.e2e


def test_three_baselines_with_pareto_verdict(dsn, db_available):
    if not db_available:
        pytest.skip("PostgreSQL not reachable")
    endpoints_obj = start_all_servers()
    endpoints = {k: v["endpoint"] for k, v in endpoints_obj.items()}
    store = EventStore(dsn)
    store.connect()
    try:
        def _factory():
            return build_default_coordinator(store=store, endpoints=endpoints)
        runner = BenchmarkRunner(store=store, coordinator_factory=_factory)
        result = runner.run_all()
        assert {b.baseline for b in result.baselines} == {"all-local", "all-public", "hybrid"}
        # Each baseline ran across all 3 contracts.
        for b in result.baselines:
            assert len(b.per_contract) == 3
            assert 0.0 <= b.metrics["structured_fact_accuracy"] <= 1.0
        # The hybrid baseline is the only one with both local facts AND
        # public research.
        hybrid = next(b for b in result.baselines if b.baseline == "hybrid")
        assert hybrid.metrics["public_research_completeness"] > 0.0
        # The all-public baseline must not have the contract facts.
        public = next(b for b in result.baselines if b.baseline == "all-public")
        assert public.metrics["structured_fact_accuracy"] == 0.0
        # The all-local baseline has 0 egress.
        local = next(b for b in result.baselines if b.baseline == "all-local")
        assert local.metrics["egress_bytes"] == 0
    finally:
        store.close()
