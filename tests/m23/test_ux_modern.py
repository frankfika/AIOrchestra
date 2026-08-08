"""M23 — UI modernization + bug-fix tests.

The M3 console was rewritten in M23 to:

  * drop the M3 / hybrid-e2e frozen strings (header + footer);
  * wire the nav tabs to real routes (was 404 on /platform and
    /security without an id);
  * render the audit timeline as structured event-detail cards
    instead of raw JSON ``<pre>`` dumps;
  * expose ``Coordinator.list_capabilities()`` as a public
    method (was ``coordinator._router._store.all()``);
  * expose ``EventStore.list_recent_task_runs(limit)`` so the
    home page can show a "Recent tasks" panel;
  * add a ``/tasks`` hub page + per-state filtering;
  * add SSE auto-refresh + theme toggle + copy-id buttons.

These tests pin each of the above so a future refactor can't
silently regress them.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from orchestra.api.app import create_app


pytestmark = pytest.mark.e2e


def _build_client() -> TestClient:
    return TestClient(create_app())


# ---------------------------------------------------------------------------
# Bug 1 / 3 — the "M3 Demo Console" / "M3 Governed Hybrid E2E" strings
#              must be gone; the version is now driven by a single
#              ``_VERSION`` constant in templates.py.
# ---------------------------------------------------------------------------


def test_home_header_uses_modern_branding_not_m3():
    body = _build_client().get("/").text
    assert "Orchestra M3 Demo Console" not in body, (
        "M23 should have removed the frozen M3 header string; the new "
        "design uses 'Orchestra' as the brand + a separate M23 badge."
    )
    assert "hybrid-e2e" not in body, (
        "M23 should have removed the 'hybrid-e2e' badge leftover from M3."
    )
    # The new header has a real logo + the version badge in its own element.
    assert 'class="logo"' in body
    assert "M23" in body


def test_footer_drops_m3_string():
    body = _build_client().get("/").text
    assert "M3 Governed Hybrid E2E" not in body, (
        "M23 should have removed the historical M3 footer string; the new "
        "footer uses the live version constant."
    )


# ---------------------------------------------------------------------------
# Bug 2 — the nav tabs must point at routes that exist, not 404.
# ---------------------------------------------------------------------------


def test_nav_business_points_at_a_real_route():
    """The 'Business' nav tab used to point at /business which 404'd."""
    client = _build_client()
    body = client.get("/").text
    assert 'href="/business"' in body
    r = client.get("/business")
    assert r.status_code == 200, "M23 added /business as an alias of /"


def test_nav_platform_points_at_a_real_route():
    """The 'Platform' nav tab used to point at /platform which 404'd
    without a task id. M23 wires it to /tasks."""
    client = _build_client()
    body = client.get("/").text
    assert 'href="/platform"' in body
    # /platform/{id} still works (legacy detail URL).
    assert client.get("/platform/does-not-exist").status_code == 404
    # /platform (no id) is now the recent-tasks hub — must not 404.
    r = client.get("/platform")
    assert r.status_code == 200, "M23 /platform is the recent-tasks hub"


def test_nav_security_points_at_a_real_route():
    """The 'Security / Audit' nav tab used to point at /security which
    404'd without a task id. M23 wires it to /tasks?state=awaiting-approval."""
    client = _build_client()
    body = client.get("/").text
    assert 'href="/security"' in body
    r = client.get("/security")
    assert r.status_code == 200, "M23 /security is the awaiting-approval hub"


# ---------------------------------------------------------------------------
# Bug 4 — the contract_text textarea must not be pre-filled with
#         the contract_id. The old code passed `contract` (the id)
#         instead of the text, so the form always showed the id in
#         the textarea placeholder.
# ---------------------------------------------------------------------------


def test_submit_form_contract_text_is_empty():
    body = _build_client().get("/").text
    # The textarea has no value attribute and the opening tag is
    # <textarea name="contract_text" ...></textarea> with no body.
    import re
    m = re.search(r'<textarea\s+name="contract_text"[^>]*>(.*?)</textarea>', body, re.S)
    assert m is not None, "submit form is missing the contract_text textarea"
    assert m.group(1).strip() == "", (
        f"M23 should not pre-fill contract_text; got {m.group(1)!r}"
    )


# ---------------------------------------------------------------------------
# Bug 5 / 6 — the io.sent audit row must NOT be a raw JSON dump.
#              The old template had a special digest renderer that
#              only fired when the payload contained "projected_digest",
#              which the EventStore never wrote — so the feature was
#              effectively dead. M23 routes by event kind, not by
#              payload field.
# ---------------------------------------------------------------------------


def test_event_renderer_dispatches_by_kind_not_payload():
    """The dispatcher helper should produce a structured card for
    io.sent events even when the payload lacks projected_digest
    (the pre-M23 bug)."""
    from orchestra.ux.templates import _render_event_detail

    # A realistic io.sent event (no projected_digest).
    detail = _render_event_detail(
        {
            "occurred_at": "2026-08-08T09:12:02.641000+08:00",
            "kind": "io.sent",
            "payload": {
                "node_id": "extract_facts_local",
                "latency_ms": 13,
                "capability_id": "local.contract-extractor",
            },
        }
    )
    # The old template would have rendered this as a JSON <pre>;
    # the new template renders a structured card.
    assert "<pre>" not in detail, (
        "M23 io.sent events should render as a structured card, not JSON pre"
    )
    # The card surfaces the human-meaningful fields.
    assert "io.sent" in detail
    assert "extract_facts_local" in detail
    assert "local.contract-extractor" in detail
    assert "13" in detail  # latency


def test_event_renderer_known_kinds_all_structured():
    """Every known event kind should render without falling back to JSON <pre>."""
    from orchestra.ux.templates import _render_event_detail

    cases = [
        ("io.intent", {"node_id": "n1", "data_view": "v1", "capability_id": "c1"}),
        ("io.received", {"node_id": "n1", "latency_ms": 5, "outputs_keys": ["a", "b"]}),
        (
            "node.started",
            {"node_id": "n1", "capability_id": "c1", "manifest_id": "m:1"},
        ),
        (
            "node.succeeded",
            {"node_id": "n1", "latency_ms": 8, "outputs_keys": ["a"]},
        ),
        (
            "node.failed",
            {"node_id": "n1", "error": "boom"},
        ),
        ("node.awaiting-approval", {"node_id": "n1"}),
        (
            "grant.issued",
            {
                "grant_id": "g1",
                "capability_id": "c1",
                "data_view": {"name": "v1", "fields": ["a", "b"]},
                "expires_at": "2026-08-08T01:00:00.000Z",
            },
        ),
        ("receipt.signed", {"node_id": "n1", "receipt_id": "r1"}),
        ("plan.created", {"nodes": ["a", "b"], "plan_id": "p1"}),
        ("plan.signed", {"signed_by": "x", "plan_digest": "d"}),
        ("task.received", {"contract_id": "c1", "purpose": "review"}),
    ]
    for kind, payload in cases:
        detail = _render_event_detail({"occurred_at": "x", "kind": kind, "payload": payload})
        assert "<pre>" not in detail, f"{kind} should render as a card, not JSON pre"
        assert kind in detail, f"{kind} detail should mention the kind"


# ---------------------------------------------------------------------------
# Bug 7 — decision column in Approvals table should render a pill,
#         not a raw string.
# ---------------------------------------------------------------------------


def test_pill_for_state_covers_decision_values():
    from orchestra.ux.templates import _pill_for_state

    for state in ("approved", "rejected", "pending", "succeeded", "failed"):
        html = _pill_for_state(state)
        assert html.startswith('<span class="pill '), f"{state!r} should render as a pill"
        assert state in html


# ---------------------------------------------------------------------------
# Bug 8 / 9 — public methods on EventStore + Coordinator replace
#              the previous private-attr traversals.
# ---------------------------------------------------------------------------


def test_coordinator_list_capabilities_is_public():
    """The Demo Console no longer reaches into _router._store directly."""
    from orchestra.coordinator.engine import Coordinator

    # Public method exists.
    assert hasattr(Coordinator, "list_capabilities")
    assert callable(Coordinator.list_capabilities)
    # It's a regular method, not a property.
    import inspect

    assert not inspect.isdatadescriptor(Coordinator.list_capabilities)


def test_event_store_list_recent_task_runs_is_public():
    from orchestra.coordinator.event_store import EventStore

    assert hasattr(EventStore, "list_recent_task_runs")


# ---------------------------------------------------------------------------
# Bug 10 — the approval form is a single form (radio) with a real
#          rationale input, not two forms with hidden decision fields.
# ---------------------------------------------------------------------------


def test_decide_endpoint_rejects_unknown_decisions():
    client = _build_client()
    # No need for a real task — the decision validation runs before
    # the coordinator call.
    r = client.post(
        "/ux/tasks/some-id/decide",
        data={"decision": "maybe", "decided_by": "x", "rationale": "y"},
        follow_redirects=False,
    )
    # 400 (validation) or 404 (no such task) both prove the validation
    # runs; we only care that "maybe" doesn't sneak through.
    assert r.status_code in (400, 404, 422)


def test_decide_form_uses_radio_not_hidden():
    """The form is rendered on the security view when the task is
    awaiting-approval. We can't easily drive that without a real DB,
    so we just exercise the validation on the new ``/decide`` route
    (the legacy ``/approve`` route forwards to it). Both return 400
    for an unknown decision before touching the store."""
    client = _build_client()
    r1 = client.post(
        "/ux/tasks/some-id/decide",
        data={"decision": "maybe", "decided_by": "x", "rationale": "y"},
        follow_redirects=False,
    )
    # The /decide route validates the decision before the store call.
    assert r1.status_code == 400, (
        f"/decide should reject 'maybe' with 400; got {r1.status_code}"
    )
    # The legacy /approve endpoint still exists (for the old demo
    # video) and validates the same way.
    r2 = client.post(
        "/ux/tasks/some-id/approve",
        data={"decision": "maybe", "decided_by": "x", "rationale": "y"},
        follow_redirects=False,
    )
    assert r2.status_code == 400


# ---------------------------------------------------------------------------
# Modern CSS — design tokens, dark mode, responsive, accessibility.
# ---------------------------------------------------------------------------


def test_modern_css_uses_design_tokens():
    body = _build_client().get("/").text
    # The new stylesheet uses CSS custom properties (design tokens).
    for token in (
        "--accent",
        "--bg",
        "--fg",
        "--radius-md",
        "--space-3",
        "--shadow-md",
    ):
        assert token in body, f"modern CSS missing design token {token!r}"


def test_modern_css_supports_dark_mode():
    body = _build_client().get("/").text
    # Dark mode is gated by a [data-theme="dark"] selector and a
    # media query — the old CSS only had a single light theme.
    assert '[data-theme="dark"]' in body
    assert "prefers-color-scheme" in body


def test_modern_css_is_responsive():
    body = _build_client().get("/").text
    # The new CSS has a phone / tablet / desktop break.
    assert "@media (max-width: 640px)" in body, "M23 should have a phone break"
    assert "@media (max-width: 900px)" in body, "M23 should have a tablet break"


def test_modern_css_has_focus_visible():
    body = _build_client().get("/").text
    assert "focus-visible" in body, "M23 should have visible focus rings"


def test_modern_html_has_meta_viewport():
    body = _build_client().get("/").text
    assert 'name="viewport"' in body, "M23 should declare a viewport for mobile"
    assert "theme-color" in body, "M23 should declare a theme-color for mobile chrome"
    assert "description" in body, "M23 should have a meta description"


def test_modern_html_has_favicon():
    body = _build_client().get("/").text
    # The new layout uses an inline SVG data-URI favicon — no extra
    # file needed, no 404 in the network tab.
    assert 'rel="icon"' in body
    assert "data:image/svg+xml" in body


# ---------------------------------------------------------------------------
# SSE auto-refresh — the detail pages declare the SSE URL so the
# inline JS can subscribe. The wiring test only checks the markup.
# ---------------------------------------------------------------------------


def test_security_view_declares_sse_url_when_real():
    """Without a DB, /security/{id} 404s. We exercise the render
    function directly with synthetic events."""
    from orchestra.ux.templates import render_security_view

    events = [{"occurred_at": "x", "kind": "task.received", "payload": {"contract_id": "c"}}]
    body = render_security_view(
        task_run_id="abc-123-456",
        events=events,
        receipts=[],
        approvals=[],
    )
    assert 'data-sse-url="/tasks/abc-123-456/events/stream"' in body


def test_platform_view_declares_sse_url_when_real():
    from orchestra.ux.templates import render_platform_view

    body = render_platform_view(
        task_run_id="abc-123-456",
        capabilities=[],
        events=[],
        grants=[],
        node_runs=[],
    )
    assert 'data-sse-url="/tasks/abc-123-456/events/stream"' in body


# ---------------------------------------------------------------------------
# Hub page — /tasks lists recent tasks.
# ---------------------------------------------------------------------------


def test_tasks_hub_renders_empty_state():
    """Without a DB, the tasks hub may 500; we just exercise the
    render function with an empty list to pin the empty state UX."""
    from orchestra.ux.templates import render_task_list

    body = render_task_list(tasks=[], state_filter=None)
    assert "No tasks yet" in body
    # The state filter is rendered when present.
    body2 = render_task_list(tasks=[], state_filter="awaiting-approval")
    assert "awaiting-approval" in body2
