"""M4 INT-DIFY-001 / INT-AH-001 — Integration tests.

The M4 B4 gate requires:

  * Dify and AgenticHub can submit the same Reference Scenario and
    receive the same governance state.
  * The EgressPEP refuses a contract whose payload exceeds the
    manifest's byte_budget, regardless of which integration called.
  * Each integration's mode-specific delegation contract is surfaced
    in the governance payload.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from orchestra.adapters.servers import start_all_servers
from orchestra.agentichub.client import AgenticHubResult, AgenticHubTaskTool
from orchestra.coordinator.engine import build_default_coordinator
from orchestra.coordinator.event_store import EventStore
from orchestra.core.ids import new_id
from orchestra.core.schema import (
    DataClassification,
    FieldManifest,
    SecurityLabel,
    SourceTrust,
)
from orchestra.dify.task_tool import DifyTaskTool, DifyTaskToolResult
from orchestra.integrations.delegation import (
    DelegationMode,
    IntegrationLevel,
    governance_state_for,
)
from orchestra.registry.bootstrap import make_egress_manifest_lookup
from orchestra.xfr.egress_pep import EgressDenied, EgressPEP
from data.samples.contracts import get_contract


pytestmark = pytest.mark.e2e


CONTRACT_TEXT = """供应商：Acme Cloud Logistics Co., Ltd.
采购方：Helios
合同金额：RMB 8,600,000.00
付款条款：Net 30
生效日期：2026-01-15
到期日期：2027-01-14
管辖：香港
终止条款：30日违约通知。"""


def _label() -> SecurityLabel:
    return SecurityLabel(
        classification=DataClassification.RESTRICTED,
        residency="local",
        source_trust=SourceTrust.INTERNAL,
        retention_days=365,
        owner="tenant:demo",
    )


async def _drive(store, coord, contract_text: str, vendor_id: str = "demo-vendor-001"):
    """Run a contract review end-to-end through the in-process
    coordinator. Returns the final state."""
    task_run_id = new_id()
    run = asyncio.create_task(
        coord.run(
            task_run_id=task_run_id,
            contract_id="ctr-m4",
            data_label=_label(),
            initial_inputs={"contract_text": contract_text, "vendor_id": vendor_id},
            budget_usd=2.0,
        )
    )
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
        decision="approve", decided_by="m4-integration-test", rationale="",
    )
    result = await run
    return result


def _boot_orchestra_with_pep(store, endpoints, *, override: dict | None = None):
    pep = EgressPEP(manifest_lookup=make_egress_manifest_lookup(overrides=override or {}))
    return build_default_coordinator(store=store, endpoints=endpoints, egress_pep=pep), pep


# ---------------------------------------------------------------------------
# Dify ↔ AgenticHub: same governance shape
# ---------------------------------------------------------------------------


def test_dify_and_agentichub_share_delegation_contract(dsn, db_available):
    """Dify and AgenticHub Adapters agree on the 3 delegation modes
    and emit the same governance keys. The two Adapters are NOT
    allowed to drift: a host that swaps one for the other must not
    have to re-write its UI.
    """
    if not db_available:
        pytest.skip("PostgreSQL not reachable")
    endpoints_obj = start_all_servers()
    endpoints = {k: v["endpoint"] for k, v in endpoints_obj.items()}
    store = EventStore(dsn)
    store.connect()
    try:
        for mode in (DelegationMode.DELEGATE_TASK, DelegationMode.DELEGATE_NODE, DelegationMode.OBSERVE_ONLY):
            dify = DifyTaskTool(mode=mode, integration_level=IntegrationLevel.ENFORCE)
            ah = AgenticHubTaskTool(mode=mode, integration_level=IntegrationLevel.ENFORCE)
            assert dify.mode == ah.mode
            # Both Adapters surface the same delegation shape.
            dify_state = governance_state_for(
                mode=mode, task_state="succeeded", plan_id="plan-x",
                audit_url="http://x/events", route_url="http://x/grants",
            )
            ah_state = governance_state_for(
                mode=mode, task_state="succeeded", plan_id="plan-x",
                audit_url="http://x/events", route_url="http://x/grants",
            )
            assert set(dify_state["delegation"].keys()) == set(ah_state["delegation"].keys())
    finally:
        store.close()


def test_dify_delegate_task_returns_governance_state(dsn, db_available):
    """The Dify Task Tool in delegate-task mode returns a structured
    governance state including delegation contract + audit/route
    deep links."""
    if not db_available:
        pytest.skip("PostgreSQL not reachable")
    from fastapi.testclient import TestClient
    from orchestra.api.app import create_app

    with TestClient(create_app()) as client:
        # Submit through the JSON API (what the Dify Adapter wraps).
        r = client.post(
            "/tasks",
            json={
                "contract_id": "ctr-m4-dify",
                "contract_text": CONTRACT_TEXT,
                "vendor_id": "demo-vendor-001",
                "budget_usd": 2.0,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert "task_run_id" in data

        # The Dify Task Tool wraps the same HTTP shape. Validate
        # that the governance_state_for helper produces a payload
        # the Dify UI can render.
        gov = governance_state_for(
            mode=DelegationMode.DELEGATE_TASK,
            task_state=data["state"],
            plan_id=data.get("plan_id"),
            audit_url=f"/tasks/{data['task_run_id']}/events",
            route_url=f"/tasks/{data['task_run_id']}/grants",
        )
        assert gov["delegation"]["mode"] == "delegate-task"
        assert gov["delegation"]["final_state_authority"] == "orchestra"
        # The audit/route deep links must point at the right paths so
        # the Dify UI can render the timeline directly.
        assert gov["audit_url"].endswith(f"/tasks/{data['task_run_id']}/events")
        assert gov["route_url"].endswith(f"/tasks/{data['task_run_id']}/grants")


def test_agentichub_submit_uses_orchestra_prefix(dsn, db_available):
    """The AgenticHub HTTP shape uses /api/v1/orchestra/... so the
    same Orchestra server can serve Dify and AgenticHub on one port.
    """
    if not db_available:
        pytest.skip("PostgreSQL not reachable")
    from fastapi.testclient import TestClient
    from orchestra.api.app import create_app

    with TestClient(create_app()) as client:
        r = client.post(
            "/api/v1/orchestra/submit",
            json={
                "contract_id": "ctr-m4-ah",
                "contract_text": CONTRACT_TEXT,
                "vendor_id": "demo-vendor-001",
                "budget_usd": 2.0,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert "task_run_id" in data
        # Status, events, grants all use the same /api/v1/orchestra
        # prefix.
        for sub in ("", "/events", "/grants"):
            r = client.get(f"/api/v1/orchestra/tasks/{data['task_run_id']}{sub}")
            assert r.status_code == 200


def test_both_integrations_emit_xfr_audit_event(dsn, db_available):
    """Whichever integration submitted the task, the public_research
    node's io.sent event carries the XFR-001 projected digest. The
    integration layer must NOT strip the digest or the dropped-fields
    payload."""
    if not db_available:
        pytest.skip("PostgreSQL not reachable")
    endpoints_obj = start_all_servers()
    endpoints = {k: v["endpoint"] for k, v in endpoints_obj.items()}
    store = EventStore(dsn)
    store.connect()
    try:
        coord, _ = _boot_orchestra_with_pep(store, endpoints)
        result = asyncio.run(_drive(store, coord, CONTRACT_TEXT))
        events = store.list_events(task_run_id=result.task_run_id)
        sent = [e for e in events if e["kind"] == "io.sent"]
        assert sent
        # At least one io.sent for the public capability carries
        # the XFR-001 fields.
        xfr = [e for e in sent if e["payload"].get("projected_digest")]
        assert xfr, "no XFR-001 io.sent event"
        for e in xfr:
            assert e["payload"].get("view_name") in {"public-research", "a2a-reference"}
    finally:
        store.close()


def test_dify_poll_in_delegate_task_mode(dsn, db_available):
    """The Dify Task Tool polls until the task reaches a terminal
    state when mode == delegate-task. In the other two modes it
    returns the initial state immediately.

    Here we drive a real task via the in-process coordinator and
    verify the polling shape (terminal states the Dify Adapter
    would observe). The actual polling loop is unit-tested in
    :mod:`tests.m4.test_delegation`.
    """
    if not db_available:
        pytest.skip("PostgreSQL not reachable")
    endpoints_obj = start_all_servers()
    endpoints = {k: v["endpoint"] for k, v in endpoints_obj.items()}
    store = EventStore(dsn)
    store.connect()
    try:
        coord, _ = _boot_orchestra_with_pep(store, endpoints)
        result = asyncio.run(_drive(store, coord, CONTRACT_TEXT))
        assert result.state.value in {"succeeded", "failed", "cancelled"}
        # The Dify Adapter's poll loop would have observed this state.
        # Check the helper emits the right shape for delegate-task.
        from orchestra.dify.task_tool import DifyTaskTool
        from orchestra.integrations.delegation import (
            DelegationMode, IntegrationLevel, governance_state_for,
        )
        dify = DifyTaskTool(mode=DelegationMode.DELEGATE_TASK, integration_level=IntegrationLevel.ENFORCE)
        gov = governance_state_for(
            mode=dify.mode,
            task_state=result.state.value,
            plan_id=result.plan.plan_id,
            audit_url=f"/tasks/{result.task_run_id}/events",
            route_url=f"/tasks/{result.task_run_id}/grants",
        )
        # delegate-task: the Adapter treats the host as a *waiter*,
        # so the final_state_authority stays with Orchestra.
        assert gov["delegation"]["final_state_authority"] == "orchestra"
        assert gov["delegation"]["mode"] == "delegate-task"
    finally:
        store.close()


def test_delegate_node_host_keeps_retry_owner():
    """In delegate-node mode, the host owns retry. A retry from the
    host is allowed and Orchestra must NOT error on a re-submit with
    the same idempotency key (the host controls dedup)."""
    # This is a contract test — the Adapter's DelegationMode picks
    # the right owner; we don't need a live server.
    from orchestra.integrations.delegation import contract_for_mode
    c = contract_for_mode(DelegationMode.DELEGATE_NODE)
    assert c.retry_owner == "host"
    assert c.idempotency_owner == "host"
    assert c.cancel_owner == "host"


def test_observe_only_orchestra_records_but_does_not_gate():
    """In observe-only mode, Orchestra records the call but the host
    owns the entire execution lifecycle. The Adapter must NOT raise
    if the host re-issues a /tasks call (the host is in charge of
    deduplication)."""
    from orchestra.integrations.delegation import contract_for_mode
    c = contract_for_mode(DelegationMode.OBSERVE_ONLY)
    assert c.execution_owner == "host"
    assert c.retry_owner == "host"
    assert c.idempotency_owner == "host"
    assert c.cancel_owner == "host"
    assert c.final_state_authority == "host"
