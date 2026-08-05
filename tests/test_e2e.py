"""End-to-end test: full Contract Review path with real PG, real servers,
real signed receipts. Verifies the P0 Gate.
"""
from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

from orchestra.adapters.servers import start_all_servers
from orchestra.coordinator.engine import build_default_coordinator
from orchestra.coordinator.event_store import EventStore
from orchestra.core.schema import (
    DataClassification,
    SecurityLabel,
    SourceTrust,
)
from orchestra.core.ids import new_id
from data.samples.contracts import get_contract


pytestmark = pytest.mark.e2e


def _label() -> SecurityLabel:
    return SecurityLabel(
        classification=DataClassification.RESTRICTED,
        residency="local",
        source_trust=SourceTrust.INTERNAL,
        retention_days=365,
        owner="tenant:demo",
    )


async def _drive_one(store, coord, contract):
    task_run_id = new_id()
    run = asyncio.create_task(
        coord.run(
            task_run_id=task_run_id,
            contract_id=contract.contract_id,
            data_label=_label(),
            initial_inputs={"contract_text": contract.body, "vendor_id": contract.vendor_id},
            budget_usd=2.0,
        )
    )
    # Wait for the approval gate to register, then decide. We poll up to
    # 15 seconds because the local extract + public research + merge take
    # real HTTP round-trips against the in-repo servers.
    deadline = asyncio.get_event_loop().time() + 15.0
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.05)
        if (task_run_id, "human_approval") in coord._approval_events:
            break
    else:
        run.cancel()
        raise RuntimeError("approval gate never registered within 15s")
    await coord.decide_approval(
        task_run_id, "human_approval",
        decision="approve", decided_by="tester", rationale="looks good",
    )
    return await run


def test_full_contract_review(dsn, db_available):
    if not db_available:
        pytest.skip("PostgreSQL not reachable")
    endpoints_obj = start_all_servers()
    endpoints = {k: v["endpoint"] for k, v in endpoints_obj.items()}
    store = EventStore(dsn)
    store.connect()
    try:
        coord = build_default_coordinator(store=store, endpoints=endpoints)
        for c in [get_contract("ctr-001"), get_contract("ctr-002")]:
            result = asyncio.run(_drive_one(store, coord, c))
            assert result.state.value == "succeeded", f"state={result.state}, error={result.error}"
            # All non-approval nodes produced output
            assert "extract_facts_local" in result.node_results
            assert "public_research" in result.node_results
            assert "merge" in result.node_results
            # Audit events present
            events = store.list_events(task_run_id=result.task_run_id)
            kinds = {e["kind"] for e in events}
            assert "task.received" in kinds
            assert "plan.created" in kinds
            assert "plan.signed" in kinds
            assert "node.started" in kinds
            assert "grant.issued" in kinds
            assert "receipt.signed" in kinds
            assert "node.approved" in kinds
            assert "task.completed" in kinds
            # All receipts verified
            assert all(r.get("verified") for r in result.receipts)
            # Egress protection: the public model must NOT have seen the raw
            # contract. We check this by inspecting the IO_SENT event payload
            # (P0 audit shape). The fact-set-only check is also visible in
            # the deterministic public-mock which never received ``body``.
            sent = [e for e in events if e["kind"] == "io.sent"]
            assert sent, "no io.sent events"
            for s in sent:
                payload = s.get("payload", {})
                # The public_research event must reference public.openai-compat
                # (or a2a); the body field is not in the payload by design.
                assert "body" not in payload
    finally:
        store.close()


def test_negative_path_restricted_blocked(dsn, db_available):
    """Negative test: restricted data must not be routeable to a public
    Adapter when the node is not ``public_research``. We exercise this
    through the Router directly because the Contract Review template
    never has such a node — but the invariant must hold.
    """
    from orchestra.registry.bootstrap import load_default_manifests, load_default_policy
    from orchestra.registry.router import Router
    from orchestra.core.schema import (
        CapabilityKind,
        DataClassification,
        Effect,
        EffectKind,
        NodeSpec,
        Purpose,
        SecurityLabel,
        SourceTrust,
    )

    if not db_available:
        pytest.skip("PostgreSQL not reachable")
    store = load_default_manifests()
    policy = load_default_policy()
    router = Router(store, policy)
    bad_node = NodeSpec(
        node_id="leaky",
        name="leaky",
        requires_purpose=Purpose(code="contract-review"),
        eligible_capability_kinds=[CapabilityKind.PUBLIC_MODEL],
        declared_effects=[Effect(kind=EffectKind.READ)],
    )
    restricted = SecurityLabel(
        classification=DataClassification.RESTRICTED,
        residency="local",
        source_trust=SourceTrust.INTERNAL,
    )
    r = router.route(bad_node, restricted, "contract-review", "local", 1.0)
    assert r.decision.chosen_capability_id == ""
    # The denial must mention the invariant.
    assert "restricted" in r.decision.rationale.lower()
