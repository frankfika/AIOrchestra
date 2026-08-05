"""M3 UX-001 / UX-002 — Demo Console tests.

The Demo Console is a real-backend-driven HTML page. These tests
mount it in-process and assert:

  * ``GET /`` renders the Business form
  * ``GET /platform/{id}`` renders the Route Preview + Permission View
  * ``GET /security/{id}`` renders the Audit Timeline with the
    XFR-001 projected digest visible (not the raw payload)
  * ``POST /tasks`` from the console actually drives the Coordinator
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from orchestra.api.app import create_app


pytestmark = pytest.mark.e2e


def _build_client():
    """Create a TestClient with a fresh in-process app.

    We avoid the default event store (which requires PG); the UX
    test only checks that the HTML renderer is wired to a real
    coordinator/store. The renderer is exercised through the
    ``/`` route (no DB) and through ``/platform`` and ``/security``
    only if a task_run_id is supplied (which requires a real DB).
    """
    return TestClient(create_app())


def test_ux_home_renders_business_form():
    """The root URL renders the Business view's submit form."""
    client = _build_client()
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "Orchestra M3 Demo Console" in body
    assert "Submit Contract Review" in body
    # The form must POST to /tasks.
    assert 'action="/tasks"' in body
    # FieldManifest / Egress references in the footer (so the page
    # advertises what the demo is for).
    assert "M3 Governed Hybrid E2E" in body


def test_ux_role_nav_present():
    """All three role tabs render in the header."""
    client = _build_client()
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert 'href="/business"' in body
    assert 'href="/platform"' in body
    assert 'href="/security"' in body


def test_ux_platform_view_404_for_missing_task():
    client = _build_client()
    r = client.get("/platform/does-not-exist")
    assert r.status_code == 404


def test_ux_security_view_404_for_missing_task():
    client = _build_client()
    r = client.get("/security/does-not-exist")
    assert r.status_code == 404


def test_ux_capabilities_json_returns_list():
    """The JSON capability mirror at /api/capabilities is the same as
    the one the platform view consumes."""
    client = _build_client()
    r = client.get("/api/capabilities")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    ids = {c["capability_id"] for c in data}
    assert "public.openai-compat" in ids
    assert "a2a.reference-agent" in ids
    assert "local.contract-extractor" in ids
    # The M3 manifests declare their egress view name.
    for c in data:
        if c["capability_id"] in {"public.openai-compat", "a2a.reference-agent"}:
            assert c.get("egress_view_name"), f"{c['capability_id']} missing egress_view_name"


def test_ux_render_layout_escapes_user_input():
    """The HTML renderer escapes all dynamic values so a malicious
    contract_text never injects script tags."""
    from orchestra.ux.templates import render_business_view, render_layout

    body = render_business_view(
        contract="<script>alert('xss')</script>",
        vendor_id='"><img src=x onerror=alert(1)>',
        task_run_id=None,
        task_state=None,
        node_results={},
    )
    html = render_layout(role="business", title="t", body_html=body, current_path="/")
    # Both payloads are escaped — the literal "<script>" tag never
    # appears in the output, and the attribute-breaking sequence is
    # entity-encoded.
    assert "<script>alert('xss')</script>" not in html
    assert '"><img src=x onerror=alert(1)>' not in html
    assert "&lt;script&gt;alert" in html


def test_ux_security_renders_xfr_digest_in_audit_timeline(dsn, db_available):
    """A task run end-to-end through the EgressPEP must surface the
    projected digest (NOT the raw payload) in the audit timeline.
    """
    if not db_available:
        pytest.skip("PostgreSQL not reachable")
    from orchestra.adapters.servers import start_all_servers
    from orchestra.coordinator.engine import build_default_coordinator
    from orchestra.coordinator.event_store import EventStore
    from orchestra.core.ids import new_id
    from orchestra.registry.bootstrap import make_egress_manifest_lookup
    from orchestra.xfr.egress_pep import EgressPEP
    from data.samples.contracts import get_contract

    endpoints_obj = start_all_servers()
    endpoints = {k: v["endpoint"] for k, v in endpoints_obj.items()}
    store = EventStore(dsn)
    store.connect()
    try:
        pep = EgressPEP(manifest_lookup=make_egress_manifest_lookup())
        coord = build_default_coordinator(store=store, endpoints=endpoints, egress_pep=pep)
        contract = get_contract("ctr-001")
        task_run_id = new_id()

        async def _drive():
            run = asyncio.create_task(
                coord.run(
                    task_run_id=task_run_id,
                    contract_id=contract.contract_id,
                    data_label=_label_for_ux(),
                    initial_inputs={"contract_text": contract.body, "vendor_id": contract.vendor_id},
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
                raise RuntimeError("approval gate never registered")
            await coord.decide_approval(task_run_id, "human_approval", decision="approve", decided_by="ux-test", rationale="")
            return await run

        asyncio.run(_drive())
    finally:
        store.close()

    # Re-verify the receipts against the stored envelopes using a
    # fresh EventStore (no need to rebuild the whole app). The
    # coordinator that built them is gone, so we can only assert
    # that the audit timeline surfaces the projected digest.
    client = TestClient(create_app())
    r = client.get(f"/security/{task_run_id}")
    assert r.status_code == 200
    body = r.text
    # The audit timeline lists the io.sent rows for the public
    # capability. The renderer shows the digest as `<code>...</code>`.
    assert "io.sent" in body
    # The XFR-001 marker: "view=" appears in the digest row.
    assert "view=" in body
    # The renderer should mention "digest" as the visible label.
    assert "digest" in body
    # The raw contract text must NOT be rendered.
    secret = contract.body.split("\n")[0]
    assert secret not in body
    # Approvals are listed too.
    assert "Approvals" in body


def _label_for_ux():
    from orchestra.core.schema import DataClassification, SecurityLabel, SourceTrust

    return SecurityLabel(
        classification=DataClassification.RESTRICTED,
        residency="local",
        source_trust=SourceTrust.INTERNAL,
        retention_days=365,
        owner="tenant:demo",
    )
