"""M3 UX-001 / UX-002 — FastAPI router for the Demo Console.

M23 — UI modernization + bug-fix pass. Mounted at the API root
by :func:`orchestra.api.app.create_app`. Exposes:

  * ``GET  /``                 → Business view (UX-002)
  * ``GET  /business``         → Alias of ``/`` (so the nav tab
                                 actually works — previously it
                                 404'd because the link was
                                 ``/business`` but only ``/``
                                 existed).
  * ``GET  /tasks``            → Recent tasks list (M23). Powers
                                 the home page "Recent" panel
                                 and the ``/platform`` /
                                 ``/security`` nav tabs (which
                                 previously 404'd without an
                                 id).
  * ``POST /ux/tasks``         → Submit a contract review
  * ``POST /ux/tasks/{id}/decide`` → Single approval form
                                 (replaces the old two-form
                                 approve/reject block).
  * ``GET  /platform/{id}``    → Route Preview + Permission View
  * ``GET  /security/{id}``    → Audit Timeline + Receipts +
                                 Approvals
  * ``GET  /api/capabilities`` (JSON) — public mirror
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from orchestra.ux.templates import (
    render_business_view,
    render_layout,
    render_legal_hold_view,
    render_platform_view,
    render_recent_tasks,
    render_security_view,
    render_task_list,
)

logger = logging.getLogger(__name__)


def _derive_node_runs(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Roll up the audit timeline into a per-node summary.

    The event stream carries every node lifecycle transition
    (started / succeeded / failed / awaiting-approval). The
    Platform view wants one row per node with the latest state,
    so we walk the events once and keep the most recent status.

    Returns a list of dicts with keys: node_id, state,
    capability_id, last_event_at, latency_ms.
    """
    by_node: dict[str, dict[str, Any]] = {}
    for e in events:
        kind = e.get("kind", "")
        payload = e.get("payload", {})
        node_id = payload.get("node_id")
        if not node_id:
            continue
        rec = by_node.setdefault(
            node_id,
            {
                "node_id": node_id,
                "state": "pending",
                "capability_id": payload.get("capability_id", ""),
                "last_event_at": e.get("occurred_at", ""),
                "latency_ms": None,
            },
        )
        rec["capability_id"] = rec["capability_id"] or payload.get("capability_id", "")
        rec["last_event_at"] = e.get("occurred_at", rec["last_event_at"])
        if kind == "node.started":
            rec["state"] = "running"
        elif kind == "node.succeeded":
            rec["state"] = "succeeded"
            if payload.get("latency_ms") is not None:
                rec["latency_ms"] = payload["latency_ms"]
        elif kind == "node.failed":
            rec["state"] = "failed"
        elif kind == "node.awaiting-approval":
            rec["state"] = "awaiting-approval"
    return list(by_node.values())


def build_ux_router(*, state_provider: Callable[[], Any]) -> APIRouter:
    router = APIRouter()

    def _state():
        return state_provider()

    # ------------------------------------------------------------------
    # Business view — ``/`` and ``/business`` are aliases. M23 fix:
    # nav tabs point at ``/business`` but only ``/`` existed, so
    # clicking the Business tab on a sub-page worked but clicking
    # it from anywhere else (a hard refresh, a shared link) would
    # 404 on the wrong shape. Now both paths render the same view.
    # ------------------------------------------------------------------

    def _render_home() -> HTMLResponse:
        state = _state()
        recent = state.store.list_recent_task_runs(limit=8)
        body = render_business_view(
            recent_tasks=recent,
        )
        return HTMLResponse(
            render_layout(
                role="business",
                title="Business",
                body_html=body,
                current_path="/",
            )
        )

    @router.get(
        "/",
        response_class=HTMLResponse,
        summary="Home (Business view)",
        tags=["UX"],
    )
    async def home(request: Request) -> HTMLResponse:  # noqa: ARG001
        return _render_home()

    @router.get(
        "/business",
        response_class=HTMLResponse,
        summary="Business view (alias of /)",
        tags=["UX"],
    )
    async def business(request: Request) -> HTMLResponse:  # noqa: ARG001
        return _render_home()

    @router.get(
        "/tasks",
        response_class=HTMLResponse,
        summary="Recent tasks list",
        tags=["UX"],
    )
    async def task_list(
        request: Request,
        state_filter: str | None = Query(default=None, alias="state"),
    ) -> HTMLResponse:
        """M23 — Recent tasks hub page.

        Powers the ``/platform`` and ``/security`` nav tabs
        (which previously 404'd without an id). When a
        ``?state=`` query is supplied the list is filtered to
        that state, so the Security / Audit tab can show only
        tasks awaiting approval or recently failed.
        """
        state = _state()
        tasks = state.store.list_recent_task_runs(limit=30)
        if state_filter:
            tasks = [t for t in tasks if t.get("state") == state_filter]
        body = render_task_list(tasks=tasks, state_filter=state_filter)
        return HTMLResponse(
            render_layout(
                role="platform",
                title="Tasks",
                body_html=body,
                current_path="/tasks",
            )
        )

    @router.get(
        "/platform",
        response_class=HTMLResponse,
        summary="Platform hub (recent tasks)",
        tags=["UX"],
    )
    async def platform_hub(request: Request) -> HTMLResponse:  # noqa: ARG001
        return await task_list(request=request, state_filter=None)

    @router.get(
        "/security",
        response_class=HTMLResponse,
        summary="Security / Audit hub (tasks awaiting approval)",
        tags=["UX"],
    )
    async def security_hub(request: Request) -> HTMLResponse:  # noqa: ARG001
        return await task_list(request=request, state_filter="awaiting-approval")

    # ------------------------------------------------------------------
    # Submit — M23 fix: contract_text is no longer pre-filled with
    # the contract_id (templates.py previously rendered the wrong
    # default). The fire-and-forget background run is unchanged.
    # ------------------------------------------------------------------

    @router.post(
        "/ux/tasks",
        summary="Submit Task (HTML form)",
        tags=["UX"],
    )
    async def submit_task(
        contract_id: str = Form(...),
        vendor_id: str = Form(...),
        contract_text: str = Form(...),
        budget_usd: float = Form(2.0),
    ):
        state = _state()
        from orchestra.core.ids import new_id
        from orchestra.core.schema import (
            DataClassification,
            SecurityLabel,
            SourceTrust,
            TaskRunState,
        )

        task_run_id = new_id()
        state.store.upsert_task_run(
            task_run_id=task_run_id,
            contract_id=contract_id,
            template_id="contract-review",
            state=TaskRunState.CREATED,
        )
        # Fire-and-forget. The platform / security pages auto-refresh
        # via SSE (templates.py inline JS).
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

    # ------------------------------------------------------------------
    # Decide approval — M23 fix: replaced the two-form block
    # (approve / reject, each with hidden decision + magic-string
    # rationale) with a single form that takes the decision as
    # a radio. The user can now override the rationale.
    # ------------------------------------------------------------------

    @router.post(
        "/ux/tasks/{task_run_id}/decide",
        summary="Decide Approval (HTML form)",
        tags=["UX"],
    )
    async def decide_approval_from_console(
        task_run_id: str,
        decision: str = Form(...),
        decided_by: str = Form("console-user"),
        rationale: str = Form(""),
    ):
        state = _state()
        if decision not in ("approve", "reject"):
            raise HTTPException(400, "decision must be approve or reject")
        await state.coordinator.decide_approval(
            task_run_id,
            "human_approval",
            decision=decision,
            decided_by=decided_by,
            rationale=rationale or f"{decision}d from console",
        )
        return RedirectResponse(url=f"/security/{task_run_id}", status_code=303)

    # Kept for backward compat with any pre-M23 demo video that
    # captured the old two-form layout. New code should use
    # ``/ux/tasks/{id}/decide``.
    @router.post(
        "/ux/tasks/{task_run_id}/approve",
        summary="Approve (legacy — use /decide)",
        tags=["UX"],
        deprecated=True,
    )
    async def approve_legacy(
        task_run_id: str,
        decision: str = Form("approve"),
        decided_by: str = Form("console-user"),
        rationale: str = Form(""),
    ):
        return await decide_approval_from_console(
            task_run_id=task_run_id,
            decision=decision,
            decided_by=decided_by,
            rationale=rationale,
        )

    # ------------------------------------------------------------------
    # Platform view — uses the new public ``Coordinator.list_capabilities``
    # instead of ``coordinator._router._store.all()`` (M23 fix:
    # private-attr traversal).
    # ------------------------------------------------------------------

    @router.get(
        "/platform/{task_run_id}",
        response_class=HTMLResponse,
        summary="Platform View (Route Preview + Permission)",
        tags=["UX"],
    )
    async def platform_view(task_run_id: str) -> HTMLResponse:
        state = _state()
        row = state.store.get_task_run(task_run_id)
        if row is None:
            raise HTTPException(404, "task not found")
        events = state.store.list_events(task_run_id=task_run_id)
        grants = state.store.list_grants(task_run_id=task_run_id)
        # M23 — derive per-node state from the event stream so we
        # don't need a separate list_node_runs query. The events
        # are the source of truth; node_runs is a cache.
        node_runs = _derive_node_runs(events)
        capabilities = [m.model_dump(mode="json") for m in state.coordinator.list_capabilities()]
        body = render_platform_view(
            task_run_id=task_run_id,
            capabilities=capabilities,
            events=events,
            grants=grants,
            node_runs=node_runs,
        )
        return HTMLResponse(
            render_layout(
                role="platform",
                title=f"Platform — {task_run_id[:8]}",
                body_html=body,
                current_path=f"/platform/{task_run_id}",
            )
        )

    # ------------------------------------------------------------------
    # Security view — M23 fix: receipt verify now logs the failure
    # (was swallowing all exceptions silently, so a corrupted row
    # looked identical to a genuinely-bad signature). Approval form
    # is now a single form with radio decision + custom rationale.
    # ------------------------------------------------------------------

    @router.get(
        "/security/legal-hold",
        response_class=HTMLResponse,
        summary="Security Center — Legal Holds (ADR-0014)",
        tags=["UX"],
    )
    async def legal_hold_hub(
        message: str | None = Query(default=None),
        error: str | None = Query(default=None),
    ) -> HTMLResponse:
        """M24 DLM-001 — Legal Hold hub page.

        Lists active holds for the demo tenant + a "Create
        hold" form + a "Release" button per row. The page
        is mounted at ``/security/legal-hold`` so the
        existing ``/security/{task_run_id}`` route (which
        catches any unrecognised suffix) is preserved.
        """
        state = _state()
        # M24 — the dev path is single-tenant; the page
        # always shows the demo tenant. The production
        # swap pulls the tenant from the request context.
        tenant_id = "tenant:demo"
        mgr = getattr(state, "_lifecycle_manager", None)
        if mgr is None:
            # The app didn't wire the manager (e.g. a
            # caller passed in a custom AppState that
            # doesn't include one). We render an
            # explanatory card instead of crashing.
            body = f"""
<section class="card">
  <h2>Legal Hold Center</h2>
  <p class="empty"><span class="icon">⚠️</span>The LifecycleManager is not wired on this app state. Construct the app via <code>create_app()</code> to enable the Legal Hold UI.</p>
</section>
"""
            return HTMLResponse(
                render_layout(
                    role="security",
                    title="Legal Hold Center",
                    body_html=body,
                    current_path="/security/legal-hold",
                )
            )
        try:
            holds = mgr.list_holds(tenant_id, active_only=True)
        except Exception as e:  # noqa: BLE001
            holds = []
            error = f"failed to list holds: {e!s}"
        from orchestra.core.schema import ResourceKind

        kinds = [k.value for k in ResourceKind]
        body = render_legal_hold_view(
            tenant_id=tenant_id,
            active_holds=holds,
            resource_kinds=kinds,
            message=message,
            error=error,
        )
        return HTMLResponse(
            render_layout(
                role="security",
                title="Legal Hold Center",
                body_html=body,
                current_path="/security/legal-hold",
            )
        )

    @router.post(
        "/security/legal-hold/create",
        summary="Create a Legal Hold (HTML form)",
        tags=["UX"],
    )
    async def legal_hold_create_form(
        case_id: str = Form(...),
        reason: str = Form(...),
        created_by: str = Form("console-user"),
        resource_kind: str = Form(""),
        resource_id: str = Form(""),
    ) -> RedirectResponse:
        """Form action: create a hold and redirect back to
        the hub with a flash message.
        """
        state = _state()
        tenant_id = "tenant:demo"
        mgr = getattr(state, "_lifecycle_manager", None)
        if mgr is None:
            return RedirectResponse(
                url="/security/legal-hold?error=manager+not+wired",
                status_code=303,
            )
        # Parse the optional resource pair.
        rk_list: list[str] | None = None
        rid_list: list[str] | None = None
        if resource_kind and resource_id:
            rk_list = [resource_kind]
            rid_list = [resource_id]
        try:
            from orchestra.core.schema import ResourceKind

            rk_enum = [ResourceKind(rk_list[0])] if rk_list else None
            mgr.create_hold(
                tenant_id=tenant_id,
                case_id=case_id,
                reason=reason,
                created_by=created_by or "console-user",
                resource_kinds=rk_enum,
                resource_ids=rid_list,
            )
        except Exception as e:  # noqa: BLE001
            return RedirectResponse(
                url=f"/security/legal-hold?error={e!s}",
                status_code=303,
            )
        return RedirectResponse(
            url=f"/security/legal-hold?message=created+hold+{case_id}",
            status_code=303,
        )

    @router.post(
        "/security/legal-hold/release",
        summary="Release a Legal Hold (HTML form)",
        tags=["UX"],
    )
    async def legal_hold_release_form(
        hold_id: str = Form(...),
        reason: str = Form(""),
    ) -> RedirectResponse:
        state = _state()
        mgr = getattr(state, "_lifecycle_manager", None)
        if mgr is None:
            return RedirectResponse(
                url="/security/legal-hold?error=manager+not+wired",
                status_code=303,
            )
        try:
            mgr.release_hold(
                hold_id=hold_id,
                released_by="console-user",
                identity_tenant_id="tenant:demo",
                reason=reason or "released from console",
            )
        except Exception as e:  # noqa: BLE001
            return RedirectResponse(
                url=f"/security/legal-hold?error={e!s}",
                status_code=303,
            )
        return RedirectResponse(
            url=f"/security/legal-hold?message=released+hold+{hold_id[:12]}",
            status_code=303,
        )

    @router.get(
        "/security/{task_run_id}",
        response_class=HTMLResponse,
        summary="Security View (Audit Timeline + Receipts)",
        tags=["UX"],
    )
    async def security_view(task_run_id: str) -> HTMLResponse:
        state = _state()
        row = state.store.get_task_run(task_run_id)
        if row is None:
            raise HTTPException(404, "task not found")
        events = state.store.list_events(task_run_id=task_run_id)
        receipts = state.store.get_receipts(task_run_id)
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
            except Exception as exc:  # noqa: BLE001
                r["verified"] = False
                # M23 — log the underlying cause so a SRE can tell
                # a real signature failure from a corrupt row. The
                # UI shows a "verify error" pill, not the same
                # green check as a real verify.
                logger.warning(
                    "receipt verify failed for task=%s receipt=%s: %s",
                    task_run_id,
                    r.get("receipt_id", "")[:8],
                    exc,
                )
                r["verify_error"] = str(exc)
        approvals = state.store.list_approvals(task_run_id=task_run_id)
        body = render_security_view(
            task_run_id=task_run_id,
            events=events,
            receipts=receipts,
            approvals=approvals,
        )
        # M23 — single approval form. The form action is the new
        # ``/decide`` endpoint with a radio for decision. The
        # default rationale is a hint, not a magic string the
        # user has to live with.
        task_state = row.get("state", "")
        if task_state == "awaiting-approval" or any(
            e["kind"] == "node.awaiting-approval"
            and not any(a.get("decision") for a in approvals)
            for e in events
        ):
            body += f"""
<section class="card" data-test="approval-form">
  <h2>Decide Approval</h2>
  <form method="post" action="/ux/tasks/{task_run_id}/decide">
    <label class="radio"><input type="radio" name="decision" value="approve" checked /> Approve</label>
    <label class="radio"><input type="radio" name="decision" value="reject" /> Reject</label>
    <label>Rationale
      <input name="rationale" placeholder="e.g. vendor cleared by compliance" />
    </label>
    <label>Decided by
      <input name="decided_by" value="console-user" />
    </label>
    <button type="submit">Submit decision</button>
  </form>
</section>
"""
        return HTMLResponse(
            render_layout(
                role="security",
                title=f"Security — {task_run_id[:8]}",
                body_html=body,
                current_path=f"/security/{task_run_id}",
            )
        )

    # ------------------------------------------------------------------
    # JSON mirror
    # ------------------------------------------------------------------

    @router.get(
        "/api/capabilities",
        response_class=JSONResponse,
        summary="Capabilities JSON (public mirror)",
        tags=["UX"],
    )
    async def capabilities_json() -> JSONResponse:
        state = _state()
        return JSONResponse(
            [m.model_dump(mode="json") for m in state.coordinator.list_capabilities()]
        )

    # ------------------------------------------------------------------
    # M24 SEC-001 — Break-glass Security Center (ADR-0012)
    # ------------------------------------------------------------------
    # The Security Center tab. Lists active / recent break-glass
    # requests for a tenant, has a "Request" form, and Approve /
    # Revoke buttons that hit the same admin endpoints the CLI uses.

    def _bg_actor_for_ux(request: Request) -> str:
        # Dev path: the X-Orchestra-Actor header or a query
        # string. Production swaps to a verified OIDC claim
        # (pilot-readiness.md §4.2).
        return (
            request.headers.get("x-orchestra-actor")
            or request.query_params.get("actor")
            or "console-user"
        )

    @router.get(
        "/security/breakglass",
        response_class=HTMLResponse,
        summary="Break-glass Security Center (list + request form)",
        tags=["UX"],
    )
    async def break_glass_center(
        request: Request,
        tenant_id: str | None = Query(default=None),
        state_filter: str | None = Query(default=None, alias="state"),
    ) -> HTMLResponse:
        state = _state()
        service = getattr(state, "break_glass_service", None)
        scope_tenant = tenant_id or getattr(state, "tenant_id", None) or "tenant:demo"
        actor = _bg_actor_for_ux(request)
        rows: list[dict[str, Any]] = []
        if service is not None:
            rows = service.list_for_tenant(scope_tenant, state=state_filter)
        from orchestra.ux.templates import render_break_glass_view

        body = render_break_glass_view(
            requests=rows,
            tenant_id=scope_tenant,
            actor=actor,
        )
        return HTMLResponse(
            render_layout(
                role="security",
                title="Break-glass",
                body_html=body,
                current_path="/security/breakglass",
            )
        )

    @router.post(
        "/ux/security/breakglass/new",
        summary="Submit a break-glass request from the console",
        tags=["UX"],
    )
    async def break_glass_request_form(
        tenant_id: str = Form(...),
        purpose: str = Form(...),
        effect: str = Form("{}"),
        resource_scope: str = Form("{}"),
        ticket: str = Form(""),
        window_seconds: int | None = Form(default=None),
        actor: str = Form("console-user"),
    ):
        state = _state()
        service = getattr(state, "break_glass_service", None)
        if service is None:
            raise HTTPException(503, "break-glass service not initialised")
        import json as _json

        try:
            effect_obj = _json.loads(effect) if effect else {}
        except _json.JSONDecodeError as e:
            raise HTTPException(400, f"effect must be valid JSON: {e}")
        try:
            scope_obj = _json.loads(resource_scope) if resource_scope else {}
        except _json.JSONDecodeError as e:
            raise HTTPException(400, f"resource_scope must be valid JSON: {e}")
        try:
            service.request(
                tenant_id=tenant_id,
                purpose=purpose,
                effect=effect_obj,
                resource_scope=scope_obj,
                requested_by=actor or "console-user",
                ticket=ticket or None,
                window_seconds=window_seconds,
            )
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e))
        return RedirectResponse(url="/security/breakglass", status_code=303)

    @router.post(
        "/ux/security/breakglass/{request_id}/approve",
        summary="Sign a break-glass request from the console",
        tags=["UX"],
    )
    async def break_glass_approve_form(
        request_id: str, actor: str = Form("console-user"), rationale: str = Form("")
    ):
        state = _state()
        service = getattr(state, "break_glass_service", None)
        if service is None:
            raise HTTPException(503, "break-glass service not initialised")
        row = service.get(request_id)
        if row is None:
            raise HTTPException(404, "break-glass request not found")
        result = service.approve(
            request_id=request_id,
            approver=actor or "console-user",
            identity_tenant_id=row.get("tenant_id"),
            rationale=rationale or "",
        )
        if not result.get("applied"):
            raise HTTPException(409, f"approval not applied: {result.get('reason')}")
        return RedirectResponse(url="/security/breakglass", status_code=303)

    @router.post(
        "/ux/security/breakglass/{request_id}/revoke",
        summary="Revoke a break-glass request from the console",
        tags=["UX"],
    )
    async def break_glass_revoke_form(
        request_id: str, actor: str = Form("console-user"), reason: str = Form("")
    ):
        state = _state()
        service = getattr(state, "break_glass_service", None)
        if service is None:
            raise HTTPException(503, "break-glass service not initialised")
        row = service.get(request_id)
        if row is None:
            raise HTTPException(404, "break-glass request not found")
        result = service.revoke(
            request_id=request_id,
            revoker=actor or "console-user",
            identity_tenant_id=row.get("tenant_id"),
            reason=reason or "",
        )
        if not result.get("applied"):
            raise HTTPException(
                409, f"revoke not applied: {result.get('reason')}"
            )
        return RedirectResponse(url="/security/breakglass", status_code=303)

    @router.get(
        "/ux/security/breakglass/sweep",
        summary="Sweep expired break-glass requests (console shortcut)",
        tags=["UX"],
    )
    async def break_glass_sweep_redirect() -> RedirectResponse:
        state = _state()
        service = getattr(state, "break_glass_service", None)
        if service is not None:
            service.sweep_expired()
        return RedirectResponse(url="/security/breakglass", status_code=303)

    # ------------------------------------------------------------------
    # Helpers used by templates.render_recent_tasks
    # ------------------------------------------------------------------

    @router.get(
        "/ux/recent-tasks",
        response_class=HTMLResponse,
        summary="Recent tasks fragment (HTMX-style partial)",
        tags=["UX"],
    )
    async def recent_tasks_fragment(
        request: Request,  # noqa: ARG001
        limit: int = Query(default=8, ge=1, le=20),
    ) -> HTMLResponse:
        state = _state()
        recent = state.store.list_recent_task_runs(limit=limit)
        return HTMLResponse(render_recent_tasks(recent))

    return router
