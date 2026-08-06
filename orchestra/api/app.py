"""FastAPI app for the P0 demo."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from orchestra.benchmarks.runner import BenchmarkResult, BenchmarkRunner
from orchestra.coordinator.engine import Coordinator
from orchestra.coordinator.event_store import EventStore
from orchestra.core.ids import new_id
from orchestra.core.schema import (
    DataClassification,
    SecurityLabel,
    SourceTrust,
    TaskRunState,
)
from orchestra.observability import (
    HTTPMetricsMiddleware,
    Metrics,
    builtin_metrics,
    render_prometheus,
)
from orchestra.templates.contract_review import CONTRACT_REVIEW_TEMPLATE

# M15 — OpenAPI metadata + CORS configuration. Imported here
# rather than at module level so the heavy fastapi import is
# deferred until the first call to ``create_app`` (the test
# path that mocks the app doesn't need CORS).
from orchestra.api.openapi import TAGS_METADATA, apply_cors, cors_origins_from_env


@dataclass
class AppState:
    store: EventStore
    coordinator: Coordinator
    benchmark_runner: BenchmarkRunner | None = None
    # M13 — Prometheus metrics registry. Owned by the app process;
    # the ``/metrics`` route renders it and the HTTPMetricsMiddleware
    # ticks the per-request counters. Production swaps the registry
    # for prometheus_client / OTel without changing the wire format.
    metrics: Metrics = field(default_factory=builtin_metrics)
    # M14 — per-tenant rate limiter. Configured from the env at
    # bootstrap; the dev path defaults to a permissive bucket so
    # local testing isn't throttled, while ``ORCHESTRA_RATE_LIMIT_RPS``
    # lets a SRE dial it for pilot traffic.
    rate_limiter: Any = None  # RateLimiter | None
    # M14 — request size cap. Requests larger than this are
    # rejected with 413 before the body reaches the application.
    max_request_bytes: int = 1 * 1024 * 1024  # 1 MiB
    # M17 — webhook dispatcher. The dev path uses a synchronous
    # in-process dispatcher; production swaps for a queue-backed
    # worker that survives process restarts.
    webhook_dispatcher: Any = None  # WebhookDispatcher | None
    # M18 — per-task delivery history. The dev path is an
    # in-memory ring buffer; production swaps for a durable
    # store.
    webhook_history: Any = None  # DeliveryHistory | None
    # M20 — per-task live event bus. The Coordinator publishes
    # every audit event here in addition to the EventStore so
    # a partner's SSE subscription sees the timeline live.
    event_bus: Any = None  # EventBus | None


def _default_data_label() -> SecurityLabel:
    return SecurityLabel(
        classification=DataClassification.RESTRICTED,
        residency="local",
        source_trust=SourceTrust.INTERNAL,
        retention_days=365,
        owner="tenant:demo",
    )


def _webhook_config_from_request(req: SubmitTaskRequest):
    """Return a :class:`WebhookConfig` when the request
    supplies both ``webhook_url`` and ``webhook_secret``;
    otherwise ``None`` (no webhook).

    The dev path treats an empty string the same as
    ``None`` so a partner who submits ``webhook_url: ""``
    by mistake doesn't accidentally wire a delivery
    that goes to a path that 404s.
    """
    if not req.webhook_url or not req.webhook_secret:
        return None
    from orchestra.webhooks import WebhookConfig

    return WebhookConfig(url=req.webhook_url, secret=req.webhook_secret)


async def _wrap_with_webhook(coro, *, task_run_id: str, state_provider):
    """Await the Coordinator, then dispatch a webhook on
    terminal state.

    The wrapper is intentionally minimal: it just runs
    the Coordinator to completion and hands the result
    to the dispatcher. A production swap would move the
    dispatch into a queue (SQS / Redis / Kafka) so the
    HTTP call doesn't block the in-process task driver.
    """
    state = state_provider()
    result = await coro
    config = getattr(state, "_webhook_configs", {}).get(task_run_id)
    if config is None:
        return result
    # Only dispatch on terminal states. ``running`` /
    # ``created`` / ``planned`` are not terminal; the
    # Coordinator's return state is the post-run state.
    dispatcher = getattr(state, "_webhook_dispatcher", None)
    if dispatcher is None:
        # No dispatcher is wired; the dev path silently
        # skips the dispatch. The partner can still poll.
        return result
    from orchestra.core.ids import new_id
    from orchestra.webhooks import WebhookDeliveryRecord

    delivery_id = new_id()
    try:
        delivery = dispatcher.deliver(
            config,
            task_run_id=task_run_id,
            state=result.state.value,
            plan_id=result.plan.plan_id if result.plan else None,
            node_results=result.node_results,
            error=result.error,
            delivery_id=delivery_id,
        )
    except Exception:  # noqa: BLE001
        # A dispatcher exception must not crash the
        # in-process task driver. The partner's webhook
        # endpoint is their problem; the task itself
        # succeeded.
        return result
    # M18 — record the delivery in the in-memory history
    # so a partner who queries ``GET /admin/webhooks/{id}``
    # sees the outcome. A production swap persists to
    # Postgres / DynamoDB. M19 — the record also stores
    # the partner's URL + secret so a manual retry can
    # re-fire the original payload without the operator
    # having to re-supply the config.
    history = getattr(state, "_webhook_history", None)
    if history is not None:
        history.record(
            WebhookDeliveryRecord.from_delivery(
                delivery,
                task_run_id=task_run_id,
                state=result.state.value,
                delivery_id=delivery_id,
                webhook_url=config.url,
                webhook_secret=config.secret,
                plan_id=result.plan.plan_id if result.plan else None,
                node_results=result.node_results,
                payload_error=result.error,
            )
        )
    return result


class SubmitTaskRequest(BaseModel):
    contract_id: str
    contract_text: str
    vendor_id: str
    budget_usd: float = 1.0
    # M17 — optional webhook callback. When both are set,
    # the dev path POSTs a signed payload to ``webhook_url``
    # when the task reaches a terminal state (``succeeded``
    # / ``failed`` / ``cancelled``). The signature is in
    # the ``X-Orchestra-Signature`` header; the partner
    # verifies with ``HMAC(secret, body)``.
    webhook_url: str | None = None
    webhook_secret: str | None = None


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
        # M13 — flip the ``orchestra_up`` gauge to 1 so a SRE can
        # alert on the process being live (the alternative — no
        # metric at all — is ambiguous: a missing export could mean
        # a down process, a missing metric, or a scrape problem).
        # The gauge is registered by ``builtin_metrics()`` so this
        # is just a .set() on the existing instance.
        up_gauge = state.metrics.gauge(
            "orchestra_up", "1 if the process is alive and serving requests."
        )
        up_gauge.set(1.0)
        try:
            yield
        finally:
            up_gauge.set(0.0)
            state.store.close()

    app = FastAPI(
        title="Orchestra API",
        description=(
            "Hybrid / Sovereign AI Orchestration Plane. The "
            "control plane between applications (Dify, Coze, "
            "AgenticHub, custom UIs) and execution resources "
            "(local models, public models, A2A agents, MCP "
            "tools, human approvers). See the white paper for "
            "the product definition and ADR-0002 for the "
            "P0 / M1+ boundary."
        ),
        version="0.1.0-m14",
        # M15 — group the endpoints by surface so a partner
        # developer reading /docs finds the right call without
        # scrolling through every route.
        openapi_tags=TAGS_METADATA,
        # M15 — pin the docs URL. ``/docs`` (Swagger UI) and
        # ``/redoc`` are the FastAPI defaults; the explicit
        # declaration makes the surface discoverable from the
        # OpenAPI spec itself.
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # M15 — CORS. Config-driven via ``ORCHESTRA_CORS_ORIGINS``.
    # An empty value (the dev default) means CORS is off — a
    # browser hitting the API without an allow-list still gets
    # blocked, which is the right posture for a backend that
    # is only consumed by SDKs / curl. Production deployments
    # set the env var to the partner-UI origin list.
    apply_cors(app, origins=cors_origins_from_env())

    # M9 — structured logging + per-request id.
    from orchestra.core.logging import RequestIdMiddleware, setup_logging

    if not getattr(state, "_logging_configured", False):
        setup_logging(level=os.environ.get("ORCHESTRA_LOG_LEVEL", "INFO"))
        state._logging_configured = True
    app.add_middleware(RequestIdMiddleware)
    # M13 — per-request Prometheus metrics. The middleware records
    # orchestra_http_requests_total and the request-duration histogram
    # for every HTTP exchange (including /metrics itself).
    app.add_middleware(HTTPMetricsMiddleware, metrics=state.metrics)
    # M14 — per-tenant rate limit + request size cap. Both run AFTER
    # the metrics middleware so a 429 / 413 still ticks the
    # request counter and the duration histogram. A SRE can
    # therefore graph throttle pressure on the same dashboard as
    # throughput.
    from orchestra.observability import (
        RateLimitMiddleware,
        RequestSizeLimitMiddleware,
    )

    if state.rate_limiter is not None:
        app.add_middleware(RateLimitMiddleware, limiter=state.rate_limiter)
    app.add_middleware(
        RequestSizeLimitMiddleware,
        max_bytes=state.max_request_bytes,
        metrics=state.metrics,
    )

    # M3 UX-001/UX-002 — mount the HTML Demo Console. The router uses
    # a state_provider closure so it shares the same EventStore and
    # Coordinator as the JSON API.
    from orchestra.ux import build_ux_router

    ux_router = build_ux_router(state_provider=lambda: state)
    app.include_router(ux_router)

    # M16 — standard error envelope (RFC 7807 Problem Details). Every
    # 4xx and 5xx response carries the same shape; partners parse it
    # once and turn each error into a typed exception. The handlers
    # run on the M9 request-id contextvar so the ``instance`` field
    # points at a real log line.
    from fastapi.exceptions import RequestValidationError
    from orchestra.api.errors import (
        PROBLEM_JSON,
        problem_from_http_exception,
    )
    from orchestra.core.logging import current_request_id

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request, exc: HTTPException):  # noqa: ARG001
        problem = problem_from_http_exception(
            exc,
            status=exc.status_code,
            request_id=current_request_id() or "",
        )
        return Response(
            content=json.dumps(problem.to_dict()),
            status_code=exc.status_code,
            media_type=PROBLEM_JSON,
            headers=getattr(exc, "headers", None) or {},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _starlette_http_exception_handler(request, exc: StarletteHTTPException):  # noqa: ARG001
        # Starlette raises its own HTTPException for 404 (no
        # matching route) and 405 (wrong method on a known
        # route). Funnel both through the same ProblemDetail
        # shape so a partner's error parser sees one format.
        problem = problem_from_http_exception(
            exc,
            status=exc.status_code,
            request_id=current_request_id() or "",
        )
        return Response(
            content=json.dumps(problem.to_dict()),
            status_code=exc.status_code,
            media_type=PROBLEM_JSON,
            headers=getattr(exc, "headers", None) or {},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(request, exc: RequestValidationError):  # noqa: ARG001
        # Pydantic validation errors carry a list of {loc, msg, type}
        # dicts; we surface the first one's message in the detail and
        # include the full list in the ``orchestra`` extension.
        from orchestra.api.errors import PROBLEM_TYPES, problem_type_uri

        errors = exc.errors() if hasattr(exc, "errors") else []
        first_msg = errors[0].get("msg", "validation failed") if errors else "validation failed"
        problem_dict = {
            "type": problem_type_uri("validation_error"),
            "title": PROBLEM_TYPES["validation_error"],
            "status": 422,
            "detail": first_msg,
            "instance": current_request_id() or "",
            "orchestra": {
                "request_id": current_request_id() or "",
                "errors": errors,
            },
        }
        return Response(
            content=json.dumps(problem_dict),
            status_code=422,
            media_type=PROBLEM_JSON,
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request, exc: Exception):  # noqa: ARG001
        # 500 must never leak the raw traceback; the request id is
        # what an operator greps for in the logs.
        from orchestra.api.errors import PROBLEM_TYPES, problem_type_uri

        problem_dict = {
            "type": problem_type_uri("internal_error"),
            "title": PROBLEM_TYPES["internal_error"],
            "status": 500,
            "detail": "Unhandled server-side failure; see logs.",
            "instance": current_request_id() or "",
            "orchestra": {
                "request_id": current_request_id() or "",
                "error_type": type(exc).__name__,
            },
        }
        return Response(
            content=json.dumps(problem_dict),
            status_code=500,
            media_type=PROBLEM_JSON,
        )

    @app.get(
        "/healthz",
        summary="Live health check (real cluster state)",
        tags=["Health"],
    )
    def healthz() -> dict[str, Any]:
        """Live health check.

        The P0-era `{"status": "ok", "milestone": "P0"}` is
        wrong now: the dev path is at M11. The endpoint
        reports the real cluster state so a SRE probe or a
        load balancer can decide whether the instance is
        healthy.

        ``status`` is one of:
          * ``ok`` — API up, DB reachable, M0+ capabilities
            registered, multi-tenant store connected.
          * ``degraded`` — API up but at least one subsystem
            is failing (e.g. DB down). The body lists the
            failing checks.
        """
        checks: dict[str, Any] = {}
        overall = "ok"
        # 1. Capabilities registered (M0+).
        try:
            caps = state.coordinator._router._store.all()  # noqa: SLF001
            n_caps = len(caps)
            checks["capabilities"] = {
                "status": "ok",
                "count": n_caps,
            }
            if n_caps == 0:
                checks["capabilities"]["status"] = "fail"
                overall = "degraded"
        except Exception as e:  # noqa: BLE001
            checks["capabilities"] = {"status": "fail", "error": str(e)}
            overall = "degraded"
        # 2. Tenant store (M6).
        tenant_count = 0
        try:
            from orchestra.enterprise.isolation import IsolatingEventStore

            store = IsolatingEventStore()
            store.connect()
            try:
                tenant_count = len(store.list_tenants())
                checks["tenants"] = {
                    "status": "ok",
                    "count": tenant_count,
                }
            finally:
                store.close()
        except Exception as e:  # noqa: BLE001
            checks["tenants"] = {"status": "fail", "error": str(e)}
            overall = "degraded"
        # 3. Published cards (M5).
        n_published = 0
        if hasattr(state, "_registry"):
            n_published = len(state._registry._by_version)  # noqa: SLF001
        checks["published_cards"] = {"status": "ok", "count": n_published}
        # M13 — keep the cardinality-bounded gauges fresh so a scrape
        # that's not aligned with /healthz still sees a current count.
        try:
            state.metrics.gauge(
                "orchestra_tenants_total", "Total tenants in the multi-tenant store."
            ).set(float(tenant_count))
            state.metrics.gauge(
                "orchestra_capabilities_total", "Total registered capabilities."
            ).set(float(checks["capabilities"].get("count", 0)))
            # The published-cards gauge is owned by the Registry;
            # only set it here if the registry isn't driving the
            # gauge itself (e.g. CLI-only path without metrics).
            if hasattr(state, "_registry") and not getattr(state._registry, "_metrics", None):
                state.metrics.gauge(
                    "orchestra_published_cards_total", "Total published Agent Cards."
                ).set(float(n_published))
        except Exception:  # noqa: BLE001
            # Gauges are observability, not control flow; never
            # let a metric failure make /healthz unhealthy.
            pass
        return {
            "status": overall,
            "version": "0.1.0-m11",
            "milestone": "M11",
            "checks": checks,
            "tenant_count": tenant_count,
            "capability_count": checks["capabilities"].get("count", 0),
            "published_card_count": n_published,
        }

    @app.get(
        "/metrics",
        summary="Prometheus text-format metrics",
        tags=["Metrics"],
    )
    def metrics() -> Response:
        """Prometheus text-format metrics export.

        The body is the standard ``text/plain; version=0.0.4``
        exposition format. Any production scraper (Prometheus,
        VictoriaMetrics, Grafana Agent) consumes it without a
        custom adapter. The ``Content-Type`` is what
        prometheus_client returns; matching it lets a SRE point
        any standard scraper at this endpoint.
        """
        body = render_prometheus(state.metrics)
        return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get(
        "/templates",
        summary="Get the contract-review template",
        tags=["Templates"],
    )
    def templates() -> dict[str, Any]:
        return CONTRACT_REVIEW_TEMPLATE.model_dump(mode="json")

    @app.get(
        "/capabilities",
        summary="List registered capabilities + policy rules",
        tags=["Capabilities"],
    )
    def capabilities() -> dict[str, Any]:
        return {
            "manifests": [
                m.model_dump(mode="json") for m in state.coordinator._router._store.all()
            ],
            "policy_rule_count": len(state.coordinator._router._policy._rules),
        }

    @app.post(
        "/tasks",
        response_model=TaskStatusResponse,
        summary="Submit a task",
        tags=["Tasks"],
        responses={
            200: {
                "description": "Task created. The Coordinator is driving it in the background.",
                "content": {
                    "application/json": {
                        "example": {
                            "task_run_id": "eafa3a59-c4be-41b0-b8e8-a8a262209874",
                            "state": "created",
                            "plan_id": None,
                            "node_results": {},
                            "error": None,
                        }
                    }
                },
            },
            422: {
                "description": "Validation error (RFC 7807 problem body).",
                "content": {
                    "application/problem+json": {
                        "example": {
                            "type": "urn:orchestra:problem:validation_error",
                            "title": "Request body or query failed validation.",
                            "status": 422,
                            "detail": "Field required",
                            "instance": "req-1f2e3d4c5b6a",
                            "orchestra": {
                                "request_id": "req-1f2e3d4c5b6a",
                                "errors": [
                                    {
                                        "loc": ["body", "contract_text"],
                                        "msg": "Field required",
                                        "type": "missing",
                                    }
                                ],
                            },
                        }
                    }
                },
            },
            429: {
                "description": "Rate-limited (per-tenant token bucket).",
                "content": {
                    "application/problem+json": {
                        "example": {
                            "type": "urn:orchestra:problem:rate_limited",
                            "title": "Token bucket exhausted; retry after the Retry-After interval.",
                            "status": 429,
                            "detail": "Token bucket exhausted; retry after the Retry-After interval.",
                            "instance": "req-7c8b9a0d1e2f",
                            "orchestra": {
                                "request_id": "req-7c8b9a0d1e2f",
                                "tenant": "partner-smoke",
                                "retry_after_seconds": 1,
                            },
                        }
                    }
                },
            },
        },
        openapi_extra={
            "requestBody": {
                "content": {
                    "application/json": {
                        "example": {
                            "contract_id": "ctr-2024-001",
                            "contract_text": "MASTER SERVICES AGREEMENT\n\nThis Agreement...",
                            "vendor_id": "vendor-42",
                            "budget_usd": 1.0,
                            "webhook_url": "https://partner.example.com/orchestra/callback",
                            "webhook_secret": "shared-secret-from-partner-portal",
                        }
                    }
                }
            }
        },
    )
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
        # M17 — if a webhook is configured, the in-process
        # driver awaits the Coordinator and then dispatches
        # the terminal-state payload to the partner URL. The
        # background task is the same asyncio.Task the demo
        # already keeps; the wrapper only adds the dispatch.
        if not hasattr(state, "_background_runs"):
            state._background_runs = {}
        if not hasattr(state, "_webhook_configs"):
            state._webhook_configs = {}
        webhook_config = _webhook_config_from_request(req)
        if webhook_config is not None:
            state._webhook_configs[task_run_id] = webhook_config
            run_coro = _wrap_with_webhook(
                run_coro,
                task_run_id=task_run_id,
                state_provider=lambda: state,
            )
        state._background_runs[task_run_id] = asyncio.create_task(run_coro)
        return TaskStatusResponse(
            task_run_id=task_run_id,
            state=TaskRunState.CREATED.value,
            plan_id=None,
            node_results={},
            error=None,
        )

    @app.get(
        "/tasks/{task_run_id}",
        response_model=TaskStatusResponse,
        summary="Get task status",
        tags=["Tasks"],
    )
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

    @app.get(
        "/tasks/{task_run_id}/events",
        summary="List task events (audit timeline)",
        tags=["Tasks"],
    )
    def get_events(task_run_id: str) -> dict[str, Any]:
        events = state.store.list_events(task_run_id=task_run_id)
        return {"task_run_id": task_run_id, "count": len(events), "events": events}

    @app.get(
        "/tasks/{task_run_id}/events/stream",
        summary="Stream task events over Server-Sent Events (SSE)",
        tags=["Tasks"],
        responses={
            200: {
                "description": (
                    "An SSE stream of audit events. Each ``data:`` line is a JSON object. "
                    "The connection closes when the task reaches a terminal state. "
                    "Late subscribers see the per-task history first, then live events."
                ),
                "content": {
                    "text/event-stream": {
                        "example": 'data: {"task_run_id":"eafa3a59","kind":"task.received","payload":{"contract_id":"ctr-2024-001"}}\n\n',
                    }
                },
            },
        },
    )
    async def stream_events(task_run_id: str):
        """Stream audit events for a task as Server-Sent Events.

        The format is the standard ``text/event-stream`` (one
        ``data: <json>`` line per event, blank line separator).
        A late subscriber sees the per-task history first,
        then live events. The connection closes when the
        task reaches a terminal state (the bus sends a
        ``None`` sentinel that the handler turns into a
        final ``event: done`` line and a clean close).
        """
        from fastapi.responses import StreamingResponse
        import json as _json

        bus = getattr(state, "event_bus", None)
        if bus is None:
            # No bus wired; the dev path should always have
            # one, but defensive: 503.
            return Response(
                content=_json.dumps(
                    {
                        "type": "urn:orchestra:problem:dependency_failure",
                        "title": "Event bus is not initialised.",
                        "status": 503,
                        "detail": "no event bus; check app bootstrap",
                    }
                ),
                status_code=503,
                media_type="application/problem+json",
            )

        # Pre-fill the per-task history (so a partner who
        # subscribes mid-task sees the audit context).
        history = bus.replay(task_run_id)
        if not history and bus.is_closed(task_run_id):
            # The task is already terminal AND we have no
            # history (e.g. process restart). Emit a single
            # close event so the client doesn't hang.
            async def empty():
                yield "event: done\ndata: {}\n\n"

            return StreamingResponse(empty(), media_type="text/event-stream")

        async def event_stream():
            sub_queue = await bus.subscribe(task_run_id)
            try:
                # The subscriber's queue is pre-filled
                # with the per-task history, then live
                # events, then a None sentinel on close.
                # Read until the sentinel; emit a
                # ``data:`` line for every event and a
                # terminal ``event: done`` line.
                while True:
                    ev = await sub_queue.get()
                    if ev is None:
                        yield "event: done\ndata: {}\n\n"
                        break
                    yield f"data: {_json.dumps(ev, default=str)}\n\n"
            finally:
                bus.unsubscribe(task_run_id, sub_queue)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.get(
        "/tasks/{task_run_id}/receipts",
        summary="List signed receipts (with verification status)",
        tags=["Tasks"],
    )
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

    @app.get(
        "/tasks/{task_run_id}/grants",
        summary="List Node Grants issued for the task",
        tags=["Tasks"],
    )
    def get_grants(task_run_id: str) -> dict[str, Any]:
        return {"task_run_id": task_run_id, "grants": state.store.list_grants(task_run_id)}

    @app.get(
        "/tasks/{task_run_id}/approvals",
        summary="List approval decisions (audit timeline)",
        tags=["Tasks"],
    )
    def get_approvals(task_run_id: str) -> dict[str, Any]:
        return {"task_run_id": task_run_id, "approvals": state.store.list_approvals(task_run_id)}

    @app.post(
        "/tasks/{task_run_id}/approve",
        summary="Approve a paused task",
        tags=["Tasks"],
    )
    async def approve(task_run_id: str, body: ApprovalRequest) -> dict[str, str]:
        try:
            await state.coordinator.decide_approval(
                task_run_id,
                "human_approval",
                decision="approve",
                decided_by=body.decided_by,
                rationale=body.rationale,
            )
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e))
        return {"status": "approved"}

    @app.post(
        "/tasks/{task_run_id}/reject",
        summary="Reject a paused task",
        tags=["Tasks"],
    )
    async def reject(task_run_id: str, body: ApprovalRequest) -> dict[str, str]:
        try:
            await state.coordinator.decide_approval(
                task_run_id,
                "human_approval",
                decision="reject",
                decided_by=body.decided_by,
                rationale=body.rationale,
            )
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e))
        return {"status": "rejected"}

    @app.post(
        "/benchmark/run",
        response_model=BenchmarkResult,
        summary="Run the 3-baseline benchmark",
        tags=["Capabilities"],
    )
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

    @app.post(
        "/api/v1/orchestra/submit",
        response_model=TaskStatusResponse,
        summary="[AgenticHub] Submit a task",
        tags=["AgenticHub"],
    )
    async def ah_submit(req: SubmitTaskRequest) -> TaskStatusResponse:
        return await submit_task(req)

    @app.get(
        "/api/v1/orchestra/tasks/{task_run_id}",
        response_model=TaskStatusResponse,
        summary="[AgenticHub] Get task status",
        tags=["AgenticHub"],
    )
    def ah_get_task(task_run_id: str) -> TaskStatusResponse:
        return get_task(task_run_id)

    @app.get(
        "/api/v1/orchestra/tasks/{task_run_id}/events",
        summary="[AgenticHub] List task events",
        tags=["AgenticHub"],
    )
    def ah_get_events(task_run_id: str) -> dict[str, Any]:
        return get_events(task_run_id)

    @app.get(
        "/api/v1/orchestra/tasks/{task_run_id}/grants",
        summary="[AgenticHub] List Node Grants",
        tags=["AgenticHub"],
    )
    def ah_get_grants(task_run_id: str) -> dict[str, Any]:
        return get_grants(task_run_id)

    @app.post(
        "/api/v1/orchestra/tasks/{task_run_id}/decide",
        summary="[AgenticHub] Decide a pending approval",
        tags=["AgenticHub"],
    )
    async def ah_decide(task_run_id: str, body: ApprovalRequest) -> dict[str, str]:
        """Decide a pending approval (approve / reject)."""
        decision = (
            body.decided_by and "approve" or "approve"
        )  # body has no decision; use path default
        # We piggy-back on the JSON API's approve / reject routes by
        # reading ``decided_by`` and ``rationale``; the AgenticHub
        # shape doesn't carry an explicit decision in the body, so we
        # always treat the call as an approval. The host (Dify /
        # AgenticHub) decides which path to call.
        try:
            await state.coordinator.decide_approval(
                task_run_id,
                "human_approval",
                decision="approve",
                decided_by=body.decided_by,
                rationale=body.rationale,
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

    @app.post(
        "/admin/tenants",
        summary="Create a tenant",
        tags=["Admin"],
    )
    def admin_create_tenant(body: dict) -> dict:
        from orchestra.enterprise.isolation import IsolatingEventStore
        from orchestra.enterprise.tenant import (
            Tenant,
            TenantContext,
            TenantRole,
            reset_active,
            set_active,
        )

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

    @app.get(
        "/admin/tenants",
        summary="List tenants",
        tags=["Admin"],
    )
    def admin_list_tenants() -> dict:
        from orchestra.enterprise.isolation import IsolatingEventStore
        from orchestra.enterprise.tenant import (
            Tenant,
            TenantContext,
            TenantRole,
            reset_active,
            set_active,
        )

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

    @app.post(
        "/admin/publish",
        summary="Publish an Agent Card",
        tags=["Admin"],
    )
    def admin_publish(body: dict) -> dict:
        """Publish an Agent Card. The body is a Card-shaped dict;
        status is forced to PUBLISHED and the body is signed with
        the dev key. The CLI's ``orchestra publish create`` calls
        this."""
        from pydantic import ValidationError

        from orchestra.core.hashing import hmac_keygen
        from orchestra.publishing.card import AgentCard, CardStatus

        # The dev key is a per-process generated key. Production
        # swaps the signer for the M6 KMS — see ENT-003.
        if not hasattr(state, "_publish_key"):
            state._publish_key = hmac_keygen()
        try:
            card = AgentCard(**{**body, "status": CardStatus.DRAFT})
        except ValidationError as e:
            raise HTTPException(422, detail=e.errors())
        from orchestra.publishing.registry import PublishedRegistry

        registry = PublishedRegistry(
            default_key=state._publish_key,
            default_kid="key-cli-1",
            metrics=state.metrics,
        )
        # Carry over previously published cards so the registry
        # isn't cleared on every CLI call (the app is a single
        # process; this is the dev shape).
        if hasattr(state, "_registry"):
            registry._by_version = dict(state._registry._by_version)
            registry._latest = dict(state._registry._latest)
            registry._by_partner = {k: set(v) for k, v in state._registry._by_partner.items()}
            # M13 — preserve the metrics handle so the gauge stays
            # consistent across multiple CLI / admin calls.
            registry._metrics = state._registry._metrics
            if registry._m_published_gauge is not None:
                registry._m_published_gauge.set(float(len(registry._by_version)))
        signed = registry.publish(card, key=state._publish_key, kid="key-cli-1")
        state._registry = registry
        return signed.model_dump(mode="json")

    @app.get(
        "/admin/publish",
        summary="List published Agent Cards",
        tags=["Admin"],
    )
    def admin_list_published() -> dict:
        if not hasattr(state, "_registry"):
            return {"cards": []}
        out = []
        for (cid, ver), entry in state._registry._by_version.items():
            out.append(
                {
                    "capability_id": cid,
                    "version": ver,
                    "status": entry.card.status.value,
                    "partner_id": entry.card.partner_id,
                }
            )
        return {"cards": out}

    @app.post(
        "/admin/publish/{capability_id}/{version}/revoke",
        summary="Revoke a published Agent Card",
        tags=["Admin"],
    )
    def admin_revoke(capability_id: str, version: str, body: dict | None = None) -> dict:
        if not hasattr(state, "_registry"):
            raise HTTPException(404, "no published cards")
        reason = (body or {}).get("reason", "")
        state._registry.revoke(capability_id, version, reason=reason)
        return {"capability_id": capability_id, "version": version, "status": "revoked"}

    @app.get(
        "/admin/webhooks/{task_run_id}",
        summary="List webhook delivery history for a task",
        tags=["Admin"],
        responses={
            200: {
                "description": "Delivery history for the task. The dev path keeps the last 16 records per task; production persists.",
                "content": {
                    "application/json": {
                        "example": {
                            "task_run_id": "eafa3a59-c4be-41b0-b8e8-a8a262209874",
                            "count": 1,
                            "deliveries": [
                                {
                                    "delivery_id": "8c3a2b91-3d4e-4f2a-9b1c-2e5d6f7a8b9c",
                                    "task_run_id": "eafa3a59-c4be-41b0-b8e8-a8a262209874",
                                    "state": "succeeded",
                                    "delivered": True,
                                    "attempts": 1,
                                    "last_status": 200,
                                    "error": "",
                                    "attempt_started_at": "2026-08-06T01:19:31.046+00:00",
                                }
                            ],
                        }
                    }
                },
            },
            404: {
                "description": "No delivery records for this task (no webhook was registered, or the task id is wrong).",
                "content": {
                    "application/problem+json": {
                        "example": {
                            "type": "urn:orchestra:problem:not_found",
                            "title": "Resource not found.",
                            "status": 404,
                            "detail": "no webhook history for task no-such-task",
                            "instance": "req-9b8c7d6e5f4a",
                        }
                    }
                },
            },
        },
    )
    def admin_webhook_history(task_run_id: str) -> dict:
        """Return the in-memory delivery history for a
        task. A partner whose webhook never fires queries
        this to see whether the dispatcher reached the
        partner, what status the partner returned, and
        what the last error was. The dev path keeps the
        last 16 records per task; production persists."""
        history = getattr(state, "webhook_history", None)
        records = history.for_task(task_run_id) if history is not None else []
        return {
            "task_run_id": task_run_id,
            "count": len(records),
            "deliveries": [r.to_dict() for r in records],
        }

    @app.post(
        "/admin/webhooks/{task_run_id}/retry",
        summary="Manually retry the latest failed webhook delivery",
        tags=["Admin"],
        responses={
            200: {
                "description": "Retry accepted. The new delivery uses a fresh delivery_id; the original record stays in the history so a SRE can compare outcomes.",
                "content": {
                    "application/json": {
                        "example": {
                            "task_run_id": "eafa3a59-c4be-41b0-b8e8-a8a262209874",
                            "retried": True,
                            "new_delivery_id": "f7a8b9c0-1d2e-3f4a-5b6c-7d8e9f0a1b2c",
                            "delivered": True,
                            "attempts": 1,
                            "last_status": 200,
                            "error": "",
                        }
                    }
                },
            },
            404: {
                "description": "No failed delivery to retry (the task has no history, or every prior delivery already succeeded).",
                "content": {
                    "application/problem+json": {
                        "example": {
                            "type": "urn:orchestra:problem:not_found",
                            "title": "Resource not found.",
                            "status": 404,
                            "detail": "no failed webhook delivery for task eafa3a59-c4be-41b0-b8e8-a8a262209874",
                            "instance": "req-1a2b3c4d5e6f",
                        }
                    }
                },
            },
        },
    )
    def admin_webhook_retry(task_run_id: str) -> dict:
        """Re-fire the most recent failed delivery.

        A SRE who notices a partner's endpoint is back
        online uses this to re-deliver the latest
        failed payload without re-submitting the task.
        The retry uses the original partner URL + secret
        (stored on the record) and a fresh ``delivery_id``
        so a partner's dedup logic sees it as a new
        attempt. The original record stays in the
        history; the new record is appended alongside it.
        """
        history = getattr(state, "webhook_history", None)
        if history is None:
            raise HTTPException(503, "webhook history is not initialised")
        last = history.last_failed(task_run_id)
        if last is None:
            raise HTTPException(
                404,
                f"no failed webhook delivery for task {task_run_id}",
            )
        dispatcher = getattr(state, "webhook_dispatcher", None)
        if dispatcher is None:
            raise HTTPException(503, "webhook dispatcher is not initialised")
        from orchestra.core.ids import new_id
        from orchestra.webhooks import WebhookConfig, WebhookDeliveryRecord

        # Re-fire the original payload with a fresh
        # delivery_id. The partner dedupes on the id; a
        # new id means a new logical attempt.
        new_delivery_id = new_id()
        delivery = dispatcher.deliver(
            WebhookConfig(url=last.webhook_url, secret=last.webhook_secret),
            task_run_id=last.task_run_id,
            state=last.state,
            plan_id=last.plan_id,
            node_results=last.node_results or {},
            error=last.payload_error,
            delivery_id=new_delivery_id,
        )
        # Append the new attempt to the history so the
        # ``GET /admin/webhooks/{id}`` view reflects
        # the retry.
        history.record(
            WebhookDeliveryRecord.from_delivery(
                delivery,
                task_run_id=last.task_run_id,
                state=last.state,
                delivery_id=new_delivery_id,
                webhook_url=last.webhook_url,
                webhook_secret=last.webhook_secret,
                plan_id=last.plan_id,
                node_results=last.node_results,
                payload_error=last.payload_error,
            )
        )
        return {
            "task_run_id": last.task_run_id,
            "retried": True,
            "new_delivery_id": new_delivery_id,
            "delivered": delivery.delivered,
            "attempts": delivery.attempts,
            "last_status": delivery.last_status,
            "error": delivery.error,
        }

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
    from orchestra.observability import Metrics, builtin_metrics
    from orchestra.runtime.rate_limit import RateLimiter, TokenBucket
    from orchestra.streaming import EventBus
    from orchestra.webhooks import DeliveryHistory, WebhookDispatcher

    endpoints_obj = start_all_servers()
    endpoints = {k: v["endpoint"] for k, v in endpoints_obj.items()}
    store = EventStore()
    store.connect()
    # M20 — the bus is constructed before the Coordinator so
    # the Coordinator can publish into it from the first
    # ``_emit`` call.
    event_bus = EventBus()
    coordinator = build_default_coordinator(store=store, endpoints=endpoints, event_bus=event_bus)
    runner = BenchmarkRunner(
        store=store,
        coordinator_factory=lambda: build_default_coordinator(
            store=store, endpoints=endpoints, event_bus=event_bus
        ),
    )
    # M14 — wire a per-tenant token-bucket rate limiter. The dev
    # default is permissive (1000 RPS, 1000 burst) so local testing
    # isn't throttled; a SRE dialing pilot traffic down sets
    # ``ORCHESTRA_RATE_LIMIT_RPS`` and ``ORCHESTRA_RATE_LIMIT_BURST``.
    metrics = builtin_metrics()
    rps = float(os.environ.get("ORCHESTRA_RATE_LIMIT_RPS", "1000"))
    burst = float(os.environ.get("ORCHESTRA_RATE_LIMIT_BURST", "1000"))
    limiter = RateLimiter(
        bucket_factory=lambda _key: TokenBucket(capacity=burst, refill_rate=rps),
        metrics=metrics,
    )
    max_body = int(os.environ.get("ORCHESTRA_MAX_REQUEST_BYTES", str(1 * 1024 * 1024)))
    # M17 — the dispatcher is process-local in dev. The
    # ``ORCHESTRA_WEBHOOK_RETRY_BUDGET`` env override lets a
    # SRE tighten the retry window for a flaky partner without
    # code changes.
    return AppState(
        store=store,
        coordinator=coordinator,
        benchmark_runner=runner,
        metrics=metrics,
        rate_limiter=limiter,
        max_request_bytes=max_body,
        webhook_dispatcher=WebhookDispatcher(),
        webhook_history=DeliveryHistory(),
        event_bus=event_bus,
    )


def run_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    state = _bootstrap_default_state()
    app = create_app(state)
    uvicorn.run(app, host=host, port=port, log_level="info")
