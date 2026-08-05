"""FastAPI app for the P0 demo."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from orchestra.benchmarks.runner import BenchmarkRunner, BenchmarkResult
from orchestra.coordinator.engine import Coordinator
from orchestra.coordinator.event_store import EventStore, EventStoreUnavailable
from orchestra.core.ids import new_id
from orchestra.core.schema import (
    DataClassification,
    SecurityLabel,
    SourceTrust,
    TaskRunState,
)
from orchestra.registry.bootstrap import load_default_manifests
from orchestra.templates.contract_review import CONTRACT_REVIEW_TEMPLATE


@dataclass
class AppState:
    store: EventStore
    coordinator: Coordinator
    benchmark_runner: BenchmarkRunner | None = None


def _default_data_label() -> SecurityLabel:
    return SecurityLabel(
        classification=DataClassification.RESTRICTED,
        residency="local",
        source_trust=SourceTrust.INTERNAL,
        retention_days=365,
        owner="tenant:demo",
    )


class SubmitTaskRequest(BaseModel):
    contract_id: str
    contract_text: str
    vendor_id: str
    budget_usd: float = 1.0


class TaskStatusResponse(BaseModel):
    task_run_id: str
    state: str
    plan_id: str | None
    node_results: dict[str, Any]
    error: str | None = None


class ApprovalRequest(BaseModel):
    decided_by: str = "demo-user"
    rationale: str = ""


def create_app(state: AppState | None = None) -> FastAPI:
    if state is None:
        state = _bootstrap_default_state()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        state.store.close()

    app = FastAPI(
        title="Orchestra P0 API",
        description="Hybrid / Sovereign AI Orchestration Plane — category proof",
        version="0.1.0-p0",
        lifespan=lifespan,
    )

    # M9 — structured logging + per-request id.
    from orchestra.core.logging import RequestIdMiddleware, setup_logging
    if not getattr(state, "_logging_configured", False):
        setup_logging(level=os.environ.get("ORCHESTRA_LOG_LEVEL", "INFO"))
        state._logging_configured = True
    app.add_middleware(RequestIdMiddleware)

    # M3 UX-001/UX-002 — mount the HTML Demo Console. The router uses
    # a state_provider closure so it shares the same EventStore and
    # Coordinator as the JSON API.
    from orchestra.ux import build_ux_router

    ux_router = build_ux_router(state_provider=lambda: state)
    app.include_router(ux_router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "milestone": "P0"}

    @app.get("/templates")
    def templates() -> dict[str, Any]:
        return CONTRACT_REVIEW_TEMPLATE.model_dump(mode="json")

    @app.get("/capabilities")
    def capabilities() -> dict[str, Any]:
        return {
            "manifests": [m.model_dump(mode="json") for m in state.coordinator._router._store.all()],
            "policy_rule_count": len(state.coordinator._router._policy._rules),
        }

    @app.post("/tasks", response_model=TaskStatusResponse)
    async def submit_task(req: SubmitTaskRequest) -> TaskStatusResponse:
        task_run_id = new_id()
        # Persist the task row up front so the API can return a stable
        # task_run_id immediately, then drive the Coordinator in the
        # background. The approval point will pause the Coordinator
        # until ``/tasks/{id}/approve`` (or ``/reject``) is called.
        state.store.upsert_task_run(
            task_run_id=task_run_id,
            contract_id=req.contract_id,
            template_id="contract-review",
            state=TaskRunState.CREATED,
        )
        run_coro = state.coordinator.run(
            task_run_id=task_run_id,
            contract_id=req.contract_id,
            data_label=_default_data_label(),
            initial_inputs={
                "contract_text": req.contract_text,
                "vendor_id": req.vendor_id,
            },
            budget_usd=req.budget_usd,
        )
        # Stash the task on the AppState so the demo can ``await`` it.
        if not hasattr(state, "_background_runs"):
            state._background_runs = {}
        state._background_runs[task_run_id] = asyncio.create_task(run_coro)
        return TaskStatusResponse(
            task_run_id=task_run_id,
            state=TaskRunState.CREATED.value,
            plan_id=None,
            node_results={},
            error=None,
        )

    @app.get("/tasks/{task_run_id}", response_model=TaskStatusResponse)
    def get_task(task_run_id: str) -> TaskStatusResponse:
        row = state.store.get_task_run(task_run_id)
        if row is None:
            raise HTTPException(404, "task not found")
        return TaskStatusResponse(
            task_run_id=task_run_id,
            state=row["state"],
            plan_id=row.get("plan_id"),
            node_results={},
            error=None,
        )

    @app.get("/tasks/{task_run_id}/events")
    def get_events(task_run_id: str) -> dict[str, Any]:
        events = state.store.list_events(task_run_id=task_run_id)
        return {"task_run_id": task_run_id, "count": len(events), "events": events}

    @app.get("/tasks/{task_run_id}/receipts")
    def get_receipts(task_run_id: str) -> dict[str, Any]:
        from orchestra.core.schema import SignedReceipt

        rows = state.store.get_receipts(task_run_id)
        # The demo coordinator verifies receipts on every read so the
        # API can report ``verified: true/false`` per receipt.
        verified_out: list[dict[str, Any]] = []
        for r in rows:
            try:
                receipt = SignedReceipt(
                    receipt_id=r["receipt_id"],
                    task_run_id=r["task_run_id"],
                    node_run_id=r["node_run_id"],
                    node_id=r["node_id"],
                    envelope=r["envelope"],
                    created_at=str(r["created_at"]),
                )
                r["verified"] = state.coordinator._receipt_builder.verify(receipt)
            except Exception as e:  # noqa: BLE001
                r["verified"] = False
                r["verify_error"] = str(e)
            verified_out.append(r)
        return {"task_run_id": task_run_id, "receipts": verified_out}

    @app.get("/tasks/{task_run_id}/grants")
    def get_grants(task_run_id: str) -> dict[str, Any]:
        return {"task_run_id": task_run_id, "grants": state.store.list_grants(task_run_id)}

    @app.get("/tasks/{task_run_id}/approvals")
    def get_approvals(task_run_id: str) -> dict[str, Any]:
        return {"task_run_id": task_run_id, "approvals": state.store.list_approvals(task_run_id)}

    @app.post("/tasks/{task_run_id}/approve")
    async def approve(task_run_id: str, body: ApprovalRequest) -> dict[str, str]:
        try:
            await state.coordinator.decide_approval(
                task_run_id, "human_approval",
                decision="approve", decided_by=body.decided_by, rationale=body.rationale,
            )
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e))
        return {"status": "approved"}

    @app.post("/tasks/{task_run_id}/reject")
    async def reject(task_run_id: str, body: ApprovalRequest) -> dict[str, str]:
        try:
            await state.coordinator.decide_approval(
                task_run_id, "human_approval",
                decision="reject", decided_by=body.decided_by, rationale=body.rationale,
            )
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e))
        return {"status": "rejected"}

    @app.post("/benchmark/run", response_model=BenchmarkResult)
    def run_benchmark() -> BenchmarkResult:
        if state.benchmark_runner is None:
            raise HTTPException(503, "benchmark runner not initialised")
        return state.benchmark_runner.run_all()

    # ------------------------------------------------------------------
    # M4 INT-AH-001 — AgenticHub HTTP shape
    # ------------------------------------------------------------------
    # The AgenticHub Adapter (orchestra/agentichub/client.py) speaks a
    # different URL shape (``/api/v1/orchestra/...``) so the same
    # Orchestra server can serve Dify and AgenticHub on one port. The
    # handlers are thin proxies to the same Coordinator / EventStore
    # the JSON API uses — there is no second source of truth.

    @app.post("/api/v1/orchestra/submit", response_model=TaskStatusResponse)
    async def ah_submit(req: SubmitTaskRequest) -> TaskStatusResponse:
        return await submit_task(req)

    @app.get("/api/v1/orchestra/tasks/{task_run_id}", response_model=TaskStatusResponse)
    def ah_get_task(task_run_id: str) -> TaskStatusResponse:
        return get_task(task_run_id)

    @app.get("/api/v1/orchestra/tasks/{task_run_id}/events")
    def ah_get_events(task_run_id: str) -> dict[str, Any]:
        return get_events(task_run_id)

    @app.get("/api/v1/orchestra/tasks/{task_run_id}/grants")
    def ah_get_grants(task_run_id: str) -> dict[str, Any]:
        return get_grants(task_run_id)

    @app.post("/api/v1/orchestra/tasks/{task_run_id}/decide")
    async def ah_decide(task_run_id: str, body: ApprovalRequest) -> dict[str, str]:
        """Decide a pending approval (approve / reject)."""
        decision = body.decided_by and "approve" or "approve"  # body has no decision; use path default
        # We piggy-back on the JSON API's approve / reject routes by
        # reading ``decided_by`` and ``rationale``; the AgenticHub
        # shape doesn't carry an explicit decision in the body, so we
        # always treat the call as an approval. The host (Dify /
        # AgenticHub) decides which path to call.
        try:
            await state.coordinator.decide_approval(
                task_run_id, "human_approval",
                decision="approve", decided_by=body.decided_by, rationale=body.rationale,
            )
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e))
        return {"status": "approved", "task_run_id": task_run_id}

    # ------------------------------------------------------------------
    # M8 — Tenant + Publishing admin endpoints
    # ------------------------------------------------------------------
    # The CLI's ``orchestra tenant`` and ``orchestra publish`` commands
    # talk to these. Each route is wrapped in the in-process Tenant
    # context (admin role) so the multi-tenant IsolatingEventStore is
    # exercised end-to-end through the same HTTP path the CLI uses.

    @app.post("/admin/tenants")
    def admin_create_tenant(body: dict) -> dict:
        from orchestra.enterprise.tenant import (
            Tenant, TenantContext, TenantRole, reset_active, set_active,
        )
        from orchestra.enterprise.isolation import IsolatingEventStore
        tid = body.get("tenant_id")
        if not tid or not isinstance(tid, str):
            raise HTTPException(422, "tenant_id must be a non-empty string")
        ctx = TenantContext(
            tenant=Tenant(tenant_id="tenant:demo", name="demo"),
            caller_id=body.get("caller_id", "cli"),
            role=TenantRole.ADMIN,
        )
        token = set_active(ctx)
        try:
            store = IsolatingEventStore()
            store.connect()
            try:
                store.create_tenant(tid, body.get("name", tid), plan=body.get("plan", "default"))
                return {"tenant_id": tid, "status": "created"}
            finally:
                store.close()
        finally:
            reset_active(token)

    @app.get("/admin/tenants")
    def admin_list_tenants() -> dict:
        from orchestra.enterprise.tenant import (
            Tenant, TenantContext, TenantRole, reset_active, set_active,
        )
        from orchestra.enterprise.isolation import IsolatingEventStore
        ctx = TenantContext(
            tenant=Tenant(tenant_id="tenant:demo", name="demo"),
            caller_id="cli",
            role=TenantRole.ADMIN,
        )
        token = set_active(ctx)
        try:
            store = IsolatingEventStore()
            store.connect()
            try:
                return {"tenants": store.list_tenants()}
            finally:
                store.close()
        finally:
            reset_active(token)

    @app.post("/admin/publish")
    def admin_publish(body: dict) -> dict:
        """Publish an Agent Card. The body is a Card-shaped dict;
        status is forced to PUBLISHED and the body is signed with
        the dev key. The CLI's ``orchestra publish create`` calls
        this."""
        from pydantic import ValidationError
        from orchestra.core.hashing import hmac_keygen
        from orchestra.publishing.card import AgentCard, CardStatus, sign_card
        # The dev key is a per-process generated key. Production
        # swaps the signer for the M6 KMS — see ENT-003.
        if not hasattr(state, "_publish_key"):
            state._publish_key = hmac_keygen()
        try:
            card = AgentCard(**{**body, "status": CardStatus.DRAFT})
        except ValidationError as e:
            raise HTTPException(422, detail=e.errors())
        from orchestra.publishing.registry import PublishedRegistry
        registry = PublishedRegistry(default_key=state._publish_key, default_kid="key-cli-1")
        # Carry over previously published cards so the registry
        # isn't cleared on every CLI call (the app is a single
        # process; this is the dev shape).
        if hasattr(state, "_registry"):
            registry._by_version = dict(state._registry._by_version)
            registry._latest = dict(state._registry._latest)
            registry._by_partner = {k: set(v) for k, v in state._registry._by_partner.items()}
        signed = registry.publish(card, key=state._publish_key, kid="key-cli-1")
        state._registry = registry
        return signed.model_dump(mode="json")

    @app.get("/admin/publish")
    def admin_list_published() -> dict:
        if not hasattr(state, "_registry"):
            return {"cards": []}
        out = []
        for (cid, ver), entry in state._registry._by_version.items():
            out.append({
                "capability_id": cid,
                "version": ver,
                "status": entry.card.status.value,
                "partner_id": entry.card.partner_id,
            })
        return {"cards": out}

    @app.post("/admin/publish/{capability_id}/{version}/revoke")
    def admin_revoke(capability_id: str, version: str, body: dict | None = None) -> dict:
        if not hasattr(state, "_registry"):
            raise HTTPException(404, "no published cards")
        reason = (body or {}).get("reason", "")
        state._registry.revoke(capability_id, version, reason=reason)
        return {"capability_id": capability_id, "version": version, "status": "revoked"}

    return app


def _bootstrap_default_state() -> AppState:
    """Build an AppState with a real Event Store + the four running
    adapter servers.

    This is the in-process mode used by the demo when no env overrides
    are set. Production deployments would inject pre-started servers via
    :func:`create_app` with an explicit ``state=``.
    """
    from orchestra.adapters.servers import start_all_servers
    from orchestra.benchmarks.runner import BenchmarkRunner
    from orchestra.coordinator.engine import build_default_coordinator

    endpoints_obj = start_all_servers()
    endpoints = {k: v["endpoint"] for k, v in endpoints_obj.items()}
    store = EventStore()
    store.connect()
    coordinator = build_default_coordinator(store=store, endpoints=endpoints)
    runner = BenchmarkRunner(
        store=store,
        coordinator_factory=lambda: build_default_coordinator(store=store, endpoints=endpoints),
    )
    return AppState(store=store, coordinator=coordinator, benchmark_runner=runner)


def run_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    state = _bootstrap_default_state()
    app = create_app(state)
    uvicorn.run(app, host=host, port=port, log_level="info")
