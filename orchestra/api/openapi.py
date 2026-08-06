"""M15 — OpenAPI metadata + CORS configuration.

A SRE who has never seen the dev path before should be able
to open ``/docs`` and answer four questions in five minutes:

  1. What can a partner call?
  2. Which endpoint is the right one for "submit a task"?
  3. What's the shape of the response, and what does each
     field mean?
  4. What error codes can each endpoint return?

The defaults FastAPI gives us (every endpoint in a flat list,
no grouping, no per-endpoint summary) are not enough. This
module provides:

  * :data:`TAGS_METADATA` — the canonical endpoint groupings
    (Tasks / Templates / Capabilities / Health / Metrics /
    AgenticHub / Admin / UX). The same names are reused in
    the CLI's ``orchestra --help`` so a SRE sees one
    vocabulary across the surface.
  * :func:`cors_origins_from_env` — read the
    ``ORCHESTRA_CORS_ORIGINS`` env var and parse it into the
    list-of-strings shape :class:`fastapi.middleware.cors.CORSMiddleware`
    expects. ``*`` means "any origin"; an empty string means
    "no CORS at all" (the dev default — a browser hitting
    the API without an explicit allow-list still gets
    blocked, which is the right posture for a backend).
  * :func:`apply_cors` — wire the CORS middleware with the
    standard partner-UI methods + headers.
"""

from __future__ import annotations

import os
from typing import Any


# The canonical OpenAPI tag grouping. The order is the order
# the tags appear in /docs and /redoc; the most-used tags
# come first so a SRE finds the right endpoint without
# scrolling.
TAGS_METADATA: list[dict[str, Any]] = [
    {
        "name": "Tasks",
        "description": (
            "Submit + drive a task through the Coordinator. "
            "POST /tasks returns a task_run_id; the human "
            "approval node pauses for /tasks/{id}/approve or "
            "/tasks/{id}/reject."
        ),
    },
    {
        "name": "Templates",
        "description": (
            "The fixed contract-review template the demo "
            "ships. Production swap: a partner-specific "
            "template loaded from the registry."
        ),
    },
    {
        "name": "Capabilities",
        "description": (
            "The capability manifest store (what the Router "
            "sees) and the 3-baseline benchmark runner."
        ),
    },
    {
        "name": "Health",
        "description": (
            "Liveness + readiness for SRE probes. "
            "Exempt from rate limiting so a load balancer "
            "can always check the instance."
        ),
    },
    {
        "name": "Metrics",
        "description": (
            "Prometheus text-format export. Standard "
            "scrapers (Prometheus, VictoriaMetrics, "
            "Grafana Agent) consume it without a custom "
            "adapter."
        ),
    },
    {
        "name": "AgenticHub",
        "description": (
            "The M4 AgenticHub HTTP shape — the same "
            "Coordinator and EventStore as the JSON API, "
            "different URL prefix. Partners that integrated "
            "with AgenticHub use these endpoints directly."
        ),
    },
    {
        "name": "Admin",
        "description": (
            "The M8 admin endpoints: tenant + publish "
            "management. The CLI's ``orchestra tenant`` and "
            "``orchestra publish`` commands call these. "
            "Tenant-scoped RBAC is enforced at the call site."
        ),
    },
    {
        "name": "UX",
        "description": (
            "The M3 HTML Demo Console. Human-facing flow "
            "for trying the dev path without a curl "
            "incantation."
        ),
    },
]


def cors_origins_from_env() -> list[str]:
    """Parse ``ORCHESTRA_CORS_ORIGINS`` into a list of origins.

    The env var format is a comma-separated list:

      * ``ORCHESTRA_CORS_ORIGINS=""`` — CORS disabled (the
        dev default; browsers get blocked).
      * ``ORCHESTRA_CORS_ORIGINS="*"`` — every origin is
        allowed. The dev path accepts this for the
        ``/docs`` partner-explorer UI.
      * ``ORCHESTRA_CORS_ORIGINS="https://a.com,https://b.com"``
        — explicit allow-list. Production deployments use
        this shape.

    A single ``*`` is preserved as a single-element list
    so :class:`CORSMiddleware` recognises the wildcard.
    """
    raw = os.environ.get("ORCHESTRA_CORS_ORIGINS", "").strip()
    if not raw:
        return []
    if raw == "*":
        return ["*"]
    return [o.strip() for o in raw.split(",") if o.strip()]


def apply_cors(app: Any, *, origins: list[str]) -> None:
    """Wire CORS on the FastAPI app.

    No-op when ``origins`` is empty. The middleware accepts
    the standard partner-UI methods (GET, POST, PUT, PATCH,
    DELETE, OPTIONS) and the headers a SDK needs to read
    (``X-Request-Id`` so the M9 trace id propagates through
    the browser) plus ``Content-Type`` (for JSON bodies).

    When ``origins`` contains ``*``, the credentials flag
    is disabled because the CORS spec forbids
    ``Access-Control-Allow-Credentials: true`` with a
    wildcard origin. Partners that need credentials use
    the explicit allow-list shape.
    """
    if not origins:
        return
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials="*" not in origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "Authorization",
            "X-Tenant-Id",
            "X-Request-Id",
            "X-Forwarded-For",
        ],
        max_age=600,  # 10 min — preflight cache, partner SDK speed
    )
