"""M3 UX-001 / UX-002 — FastAPI router for the Demo Console.

Mounted at the API root by :func:`orchestra.api.app.create_app`. Exposes:

  * ``GET  /``              → Business view (UX-002)
  * ``POST /tasks``         → Submit a contract review (calls the same
                              Coordinator entry-point as ``/tasks``)
  * ``GET  /platform/{id}`` → Route Preview + Permission View
  * ``GET  /security/{id}`` → Audit Timeline + Receipts + Approvals
  * ``GET  /api/capabilities`` (JSON) — mirror of ``/capabilities`` for
                                       the platform view
"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from orchestra.ux.templates import (
    render_business_view,
    render_layout,
    render_platform_view,
    render_security_view,
)


def build_ux_router(*, state_provider: Callable[[], Any]) -> APIRouter:
    """Build the FastAPI router bound to a state provider.

    ``state_provider`` returns the :class:`orchestra.api.app.AppState`
    on each call so the router can talk to the same EventStore and
    Coordinator as the JSON API.
    """
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> HTMLResponse:
        body = render_business_view(
            contract="ctr-001", vendor_id="demo-vendor-001",
            task_run_id=None, task_state=None, node_results={},
        )
        return HTMLResponse(render_layout(role="business", title="Business", body_html=body, current_path="/"))

    @router.post("/tasks")
    async def submit_task(
        contract_id: str = Form(...),
        vendor_id: str = Form(...),
        contract_text: str = Form(...),
        budget_usd: float = Form(2.0),
    ):
        state = state_provider()
        from orchestra.core.ids import new_id
        from orchestra.core.schema import (
            DataClassification, SecurityLabel, SourceTrust, TaskRunState,
        )

        task_run_id = new_id()
        state.store.upsert_task_run(
            task_run_id=task_run_id,
            contract_id=contract_id,
            template_id="contract-review",
            state=TaskRunState.CREATED,
        )
        # Fire-and-forget. The UX page polls /platform and /security.
        import asyncio
        run = state.coordinator.run(
            task_run_id=task_run_id,
            contract_id=contract_id,
            data_label=SecurityLabel(
                classification=DataClassification.RESTRICTED,
                residency="local",
                source_trust=SourceTrust.INTERNAL,
                retention_days=365,
                owner="tenant:demo",
            ),
            initial_inputs={"contract_text": contract_text, "vendor_id": vendor_id},
            budget_usd=budget_usd,
        )
        if not hasattr(state, "_background_runs"):
            state._background_runs = {}
        state._background_runs[task_run_id] = asyncio.create_task(run)
        return RedirectResponse(url=f"/platform/{task_run_id}", status_code=303)

    @router.post("/tasks/{task_run_id}/approve")
    async def approve_from_console(
        task_run_id: str, decision: str = Form(...), decided_by: str = Form("console-user"), rationale: str = Form(""),
    ):
        state = state_provider()
        await state.coordinator.decide_approval(
            task_run_id, "human_approval",
            decision=decision, decided_by=decided_by, rationale=rationale,
        )
        return RedirectResponse(url=f"/security/{task_run_id}", status_code=303)

    @router.get("/platform/{task_run_id}", response_class=HTMLResponse)
    async def platform_view(task_run_id: str) -> HTMLResponse:
        state = state_provider()
        row = state.store.get_task_run(task_run_id)
        if row is None:
            raise HTTPException(404, "task not found")
        events = state.store.list_events(task_run_id=task_run_id)
        grants = state.store.list_grants(task_run_id=task_run_id)
        # Pull the live capability table from the router's store.
        capabilities = [m.model_dump(mode="json") for m in state.coordinator._router._store.all()]
        body = render_platform_view(
            task_run_id=task_run_id,
            capabilities=capabilities,
            events=events,
            grants=grants,
        )
        return HTMLResponse(render_layout(role="platform", title=f"Platform — {task_run_id[:8]}", body_html=body, current_path=f"/platform/{task_run_id}"))

    @router.get("/security/{task_run_id}", response_class=HTMLResponse)
    async def security_view(task_run_id: str) -> HTMLResponse:
        state = state_provider()
        row = state.store.get_task_run(task_run_id)
        if row is None:
            raise HTTPException(404, "task not found")
        events = state.store.list_events(task_run_id=task_run_id)
        receipts = state.store.get_receipts(task_run_id)
        # Verify each receipt.
        from orchestra.core.schema import SignedReceipt
        for r in receipts:
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
            except Exception:  # noqa: BLE001
                r["verified"] = False
        approvals = state.store.list_approvals(task_run_id=task_run_id)
        body = render_security_view(
            task_run_id=task_run_id,
            events=events,
            receipts=receipts,
            approvals=approvals,
        )
        # Append an approval form when the task is awaiting approval.
        task_state = row.get("state", "")
        if task_state == "awaiting-approval" or any(
            e["kind"] == "node.awaiting-approval" and not any(
                a.get("decision") for a in approvals
            ) for e in events
        ):
            body += f"""
<section class="card">
  <h2>Decide Approval</h2>
  <form method="post" action="/tasks/{task_run_id}/approve">
    <label>Rationale <input name="rationale" value="looks good" /></label>
    <label>Decided by <input name="decided_by" value="console-user" /></label>
    <input type="hidden" name="decision" value="approve" />
    <button type="submit">Approve</button>
  </form>
  <form method="post" action="/tasks/{task_run_id}/approve">
    <input type="hidden" name="decision" value="reject" />
    <input type="hidden" name="rationale" value="rejected from console" />
    <input type="hidden" name="decided_by" value="console-user" />
    <button type="submit">Reject</button>
  </form>
</section>
"""
        return HTMLResponse(render_layout(role="security", title=f"Security — {task_run_id[:8]}", body_html=body, current_path=f"/security/{task_run_id}"))

    @router.get("/api/capabilities", response_class=JSONResponse)
    async def capabilities_json() -> JSONResponse:
        state = state_provider()
        return JSONResponse([m.model_dump(mode="json") for m in state.coordinator._router._store.all()])

    return router
