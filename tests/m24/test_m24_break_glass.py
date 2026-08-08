"""M24 W2 — Break-glass two-person control (SEC-001).

ADR-0012. The two-person control state machine is the only legal
way to produce a Break-glass grant. These tests exercise:

* the effect-ceiling check (no ``disable_egress_pep``, no label
  downgrade, no ``egress.public`` for RESTRICTED);
* the two-approver sequence (applicant ≠ first, first ≠ second);
* cross-tenant denial;
* the bounded window (default 15 min, hard cap 4 h);
* the sweep that moves active → expired;
* the API surface (the 6 ``/admin/breakglass`` endpoints);
* the CLI surface (the ``breakglass`` subcommands).

DB-backed tests are gated behind ``db_available`` so a developer
without Postgres still sees a green smoke run.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import pytest

from orchestra.coordinator.event_store import EventStore
from orchestra.core.ids import new_id
from orchestra.enterprise.break_glass import (
    DEFAULT_WINDOW_SECONDS,
    HARD_MAX_WINDOW_SECONDS,
    BreakGlassEffectRejected,
    BreakGlassService,
    _check_effect_ceiling,
    _clamp_window,
)


pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Pure-function unit tests (no DB needed)
# ---------------------------------------------------------------------------


def test_check_effect_ceiling_rejects_disable_egress_pep() -> None:
    """A ``disable_egress_pep: True`` effect must be rejected at
    request time, before the approvers see it.
    """
    reason = _check_effect_ceiling(
        {"disable_egress_pep": True},
        {"resource_kind": "artifact", "resource_id": "art-1"},
    )
    assert reason is not None
    assert "disable_egress_pep" in reason


def test_check_effect_ceiling_rejects_label_downgrade() -> None:
    """A ``label_override`` lower than the resource's current
    classification must be rejected.
    """
    reason = _check_effect_ceiling(
        {"label_override": "public"},
        {"resource_kind": "artifact", "resource_id": "art-1",
         "current_classification": "internal"},
    )
    assert reason is not None
    assert "label_override" in reason


def test_check_effect_ceiling_rejects_egress_public_for_restricted() -> None:
    """``egress.public`` is forbidden for RESTRICTED resources."""
    reason = _check_effect_ceiling(
        {"egress_view_name": "egress.public"},
        {"resource_kind": "artifact", "resource_id": "art-1",
         "current_classification": "restricted"},
    )
    assert reason is not None
    assert "egress.public" in reason


def test_check_effect_ceiling_allows_safe_override() -> None:
    """A label raise (or a same-level override) is allowed."""
    reason = _check_effect_ceiling(
        {"label_override": "restricted"},
        {"current_classification": "internal"},
    )
    assert reason is None
    reason = _check_effect_ceiling(
        {"egress_view_name": "egress.public"},
        {"current_classification": "internal"},
    )
    assert reason is None


def test_clamp_window_defaults_to_15_minutes() -> None:
    """No caller / tenant override → 15 min default."""
    assert _clamp_window(None, None) == DEFAULT_WINDOW_SECONDS


def test_clamp_window_respects_tenant_max() -> None:
    """A tenant max of 5 min overrides a 1h request."""
    assert _clamp_window(3600, 300) == 300


def test_clamp_window_respects_hard_cap() -> None:
    """The 4h hard cap is the absolute ceiling."""
    assert _clamp_window(24 * 3600, None) == HARD_MAX_WINDOW_SECONDS


def test_clamp_window_uses_minimum_one() -> None:
    """A 0-second window is clamped up to 1 second."""
    assert _clamp_window(0, None) == 1


# ---------------------------------------------------------------------------
# DB-backed integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
def store(dsn: str) -> EventStore:
    s = EventStore(dsn)
    s.connect()
    return s


@pytest.fixture
def service(store: EventStore) -> BreakGlassService:
    return BreakGlassService(store=store)


@pytest.fixture
def fresh_tenant_id() -> str:
    return f"tenant:m24:{new_id()[:8]}"


def _cleanup(store: EventStore, tenant_id: str) -> None:
    """Best-effort delete so a test can run repeatedly."""
    try:
        with store._tx() as c, c.cursor() as cur:  # noqa: SLF001
            cur.execute(
                "DELETE FROM break_glass_requests WHERE tenant_id=%s",
                (tenant_id,),
            )
            cur.execute(
                "DELETE FROM events WHERE payload->>'tenant_id'=%s",
                (tenant_id,),
            )
            cur.execute(
                "DELETE FROM task_runs WHERE task_run_id LIKE 'bg-anchor-tenant:m24:%%'",
                (),
            )
    except Exception:  # noqa: BLE001
        pass


def test_two_distinct_approvers_activate_break_glass(
    service: BreakGlassService,
    store: EventStore,
    fresh_tenant_id: str,
) -> None:
    """W2-Gate scenario #1: two different approvers activate a
    break-glass (state goes to ``active``).
    """
    _cleanup(store, fresh_tenant_id)
    try:
        req = service.request(
            tenant_id=fresh_tenant_id,
            purpose="incident-2026-08-08",
            effect={"kind": "override_egress_view", "view": "egress.internal"},
            resource_scope={"resource_kind": "artifact", "resource_id": "art-1"},
            requested_by="alice@acme",
            ticket="INC-1234",
        )
        assert req.state.value == "requested"
        # First signature
        r1 = service.approve(
            request_id=req.request_id,
            approver="bob@acme",
            identity_tenant_id=fresh_tenant_id,
        )
        assert r1["applied"] is True
        assert r1["state"] == "first-approved"
        # Second signature (different identity)
        r2 = service.approve(
            request_id=req.request_id,
            approver="carol@acme",
            identity_tenant_id=fresh_tenant_id,
        )
        assert r2["applied"] is True
        assert r2["state"] == "active"
        assert service.is_active(req.request_id)
    finally:
        _cleanup(store, fresh_tenant_id)


def test_tenant_a_works_for_tenant_a(
    service: BreakGlassService,
    store: EventStore,
) -> None:
    """W2-Gate scenario #2: a tenant's break-glass works for
    that tenant (sanity; the cross-tenant denial is in the next
    test).
    """
    tid = f"tenant:m24:alice:{new_id()[:6]}"
    _cleanup(store, tid)
    try:
        req = service.request(
            tenant_id=tid,
            purpose="tenant-scope",
            effect={"kind": "override_egress_view", "view": "egress.internal"},
            resource_scope={"resource_kind": "artifact", "resource_id": "art-1"},
            requested_by="alice",
        )
        # First approval: tenant Alice's break-glass advances.
        r = service.approve(
            request_id=req.request_id,
            approver="bob",
            identity_tenant_id=tid,
        )
        assert r["applied"] is True
        assert r["state"] == "first-approved"
    finally:
        _cleanup(store, tid)


def test_applicant_cannot_approve_own_request(
    service: BreakGlassService,
    store: EventStore,
    fresh_tenant_id: str,
) -> None:
    """W2-Gate scenario #6: applicant ≠ approver.
    """
    _cleanup(store, fresh_tenant_id)
    try:
        req = service.request(
            tenant_id=fresh_tenant_id,
            purpose="self-approval-attempt",
            effect={"kind": "override_egress_view", "view": "egress.internal"},
            resource_scope={"resource_kind": "artifact", "resource_id": "art-1"},
            requested_by="alice",
        )
        r = service.approve(
            request_id=req.request_id,
            approver="alice",
            identity_tenant_id=fresh_tenant_id,
        )
        assert r["applied"] is False
        assert r["reason"] == "applicant_cannot_approve"
    finally:
        _cleanup(store, fresh_tenant_id)


def test_first_approver_cannot_approve_twice(
    service: BreakGlassService,
    store: EventStore,
    fresh_tenant_id: str,
) -> None:
    """W2-Gate scenario #7: first approver ≠ second approver.
    """
    _cleanup(store, fresh_tenant_id)
    try:
        req = service.request(
            tenant_id=fresh_tenant_id,
            purpose="double-signature",
            effect={"kind": "override_egress_view", "view": "egress.internal"},
            resource_scope={"resource_kind": "artifact", "resource_id": "art-1"},
            requested_by="alice",
        )
        r1 = service.approve(
            request_id=req.request_id,
            approver="bob",
            identity_tenant_id=fresh_tenant_id,
        )
        assert r1["applied"] is True
        # Bob tries again
        r2 = service.approve(
            request_id=req.request_id,
            approver="bob",
            identity_tenant_id=fresh_tenant_id,
        )
        assert r2["applied"] is False
        assert r2["reason"] == "already_approved_by_you"
    finally:
        _cleanup(store, fresh_tenant_id)


def test_cross_tenant_approve_denied(
    service: BreakGlassService,
    store: EventStore,
) -> None:
    """W2-Gate scenario #8: cross-tenant approve is denied.
    """
    tid_a = f"tenant:m24:alpha:{new_id()[:6]}"
    tid_b = f"tenant:m24:beta:{new_id()[:6]}"
    _cleanup(store, tid_a)
    _cleanup(store, tid_b)
    try:
        req = service.request(
            tenant_id=tid_a,
            purpose="cross-tenant-attack",
            effect={"kind": "override_egress_view", "view": "egress.internal"},
            resource_scope={"resource_kind": "artifact", "resource_id": "art-1"},
            requested_by="alice",
        )
        r = service.approve(
            request_id=req.request_id,
            approver="mallory",
            identity_tenant_id=tid_b,  # wrong tenant
        )
        assert r["applied"] is False
        assert r["reason"] == "cross_tenant"
    finally:
        _cleanup(store, tid_a)
        _cleanup(store, tid_b)


def test_revoke_unknown_request_returns_not_found(
    service: BreakGlassService,
) -> None:
    """W2-Gate scenario #9: revoking a non-existent request
    returns 404 (the service returns ``not_found``; the API
    turns that into 404).
    """
    r = service.revoke(
        request_id="bg:nonexistent",
        revoker="alice",
        identity_tenant_id="tenant:m24:phantom",
    )
    assert r["applied"] is False
    assert r["reason"] == "not_found"


def test_revoke_already_terminal_request_rejected(
    service: BreakGlassService,
    store: EventStore,
    fresh_tenant_id: str,
) -> None:
    """W2-Gate scenario #10: revoking an already-revoked
    request is rejected.
    """
    _cleanup(store, fresh_tenant_id)
    try:
        req = service.request(
            tenant_id=fresh_tenant_id,
            purpose="revoke-twice",
            effect={"kind": "override_egress_view", "view": "egress.internal"},
            resource_scope={"resource_kind": "artifact", "resource_id": "art-1"},
            requested_by="alice",
        )
        # First revoke succeeds.
        r1 = service.revoke(
            request_id=req.request_id,
            revoker="bob",
            identity_tenant_id=fresh_tenant_id,
            reason="false alarm",
        )
        assert r1["applied"] is True
        # Second revoke fails.
        r2 = service.revoke(
            request_id=req.request_id,
            revoker="carol",
            identity_tenant_id=fresh_tenant_id,
        )
        assert r2["applied"] is False
        assert r2["reason"] == "already_terminal"
    finally:
        _cleanup(store, fresh_tenant_id)


def test_break_glass_with_disable_egress_pep_rejected_at_request(
    service: BreakGlassService,
    store: EventStore,
    fresh_tenant_id: str,
) -> None:
    """W2-Gate scenario #15: a break-glass with
    ``disable_egress_pep=True`` is rejected at request time.
    """
    _cleanup(store, fresh_tenant_id)
    try:
        with pytest.raises(BreakGlassEffectRejected):
            service.request(
                tenant_id=fresh_tenant_id,
                purpose="smuggle-1",
                effect={"disable_egress_pep": True},
                resource_scope={"resource_kind": "artifact", "resource_id": "art-1"},
                requested_by="alice",
            )
    finally:
        _cleanup(store, fresh_tenant_id)


def test_sweep_moves_active_to_expired(
    service: BreakGlassService,
    store: EventStore,
) -> None:
    """W2-Gate scenario #5: ``sweep_expired`` moves active →
    expired when ``expires_at`` passes.
    """
    tid = f"tenant:m24:sweep:{new_id()[:6]}"
    _cleanup(store, tid)
    try:
        # Create + activate a break-glass with a 1-second window.
        req = service.request(
            tenant_id=tid,
            purpose="sweep-test",
            effect={"kind": "override_egress_view", "view": "egress.internal"},
            resource_scope={"resource_kind": "artifact", "resource_id": "art-1"},
            requested_by="alice",
            window_seconds=1,
        )
        r1 = service.approve(
            request_id=req.request_id, approver="bob", identity_tenant_id=tid
        )
        assert r1["applied"] is True
        r2 = service.approve(
            request_id=req.request_id, approver="carol", identity_tenant_id=tid
        )
        assert r2["applied"] is True
        assert service.is_active(req.request_id)
        # Sleep past the window.
        time.sleep(1.2)
        ids = service.sweep_expired()
        assert req.request_id in ids
        # After sweep the row is no longer active.
        assert not service.is_active(req.request_id)
        row = service.get(req.request_id)
        assert row is not None
        assert row["state"] == "expired"
    finally:
        _cleanup(store, tid)


# ---------------------------------------------------------------------------
# API smoke tests (use a real test app)
# ---------------------------------------------------------------------------


def _make_test_app_state() -> Any:
    """Construct an AppState with a real EventStore + the
    M24 services. Returns the state object; caller passes it
    to ``create_app(state)``.
    """
    from fastapi.testclient import TestClient

    from orchestra.adapters.servers import start_all_servers
    from orchestra.api.app import AppState, create_app
    from orchestra.coordinator.engine import build_default_coordinator
    from orchestra.enterprise.approval import ApprovalService
    from orchestra.enterprise.break_glass import BreakGlassService
    from orchestra.observability import builtin_metrics
    from orchestra.streaming import EventBus

    endpoints_obj = start_all_servers()
    endpoints = {k: v["endpoint"] for k, v in endpoints_obj.items()}
    store = EventStore()
    store.connect()
    event_bus = EventBus()
    approval_service = ApprovalService(store=store)
    break_glass_service = BreakGlassService(store=store)
    coordinator = build_default_coordinator(
        store=store,
        endpoints=endpoints,
        event_bus=event_bus,
        tenant_id="tenant:m24:apitest",
        approval_service=approval_service,
        break_glass_service=break_glass_service,
    )
    state = AppState(
        store=store,
        coordinator=coordinator,
        metrics=builtin_metrics(),
        event_bus=event_bus,
        approval_service=approval_service,
        break_glass_service=break_glass_service,
        tenant_id="tenant:m24:apitest",
    )
    return state


@pytest.fixture
def client() -> Any:
    state = _make_test_app_state()
    from fastapi.testclient import TestClient

    from orchestra.api.app import create_app

    app = create_app(state)
    with TestClient(app) as c:
        # Stash the store on the client so cleanup helpers can
        # reach it. FastAPI doesn't expose app state as a
        # public attribute the way some test clients do.
        c._orchestra_state = state  # type: ignore[attr-defined]
        yield c
    state.store.close()


def _bg_cleanup(client: Any, tid: str) -> None:
    state = client._orchestra_state  # type: ignore[attr-defined]
    with state.store._tx() as c, c.cursor() as cur:  # noqa: SLF001
        cur.execute(
            "DELETE FROM break_glass_requests WHERE tenant_id=%s", (tid,)
        )
        cur.execute(
            "DELETE FROM events WHERE payload->>'tenant_id'=%s", (tid,)
        )


def test_api_break_glass_request_then_two_approves(
    client: Any,
) -> None:
    """API smoke: POST /admin/breakglass, two POSTs to /approve,
    GET to confirm state = active.
    """
    tid = f"tenant:m24:api:{new_id()[:6]}"
    try:
        # 1. Request
        r = client.post(
            "/admin/breakglass",
            headers={"X-Orchestra-Actor": "alice@apitest"},
            json={
                "tenant_id": tid,
                "purpose": "api-test-incident",
                "effect": {"kind": "override_egress_view", "view": "egress.internal"},
                "resource_scope": {"resource_kind": "artifact", "resource_id": "art-1"},
                "ticket": "INC-API-1",
                "window_seconds": 600,
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        rid = body["request_id"]
        assert body["state"] == "requested"
        assert body["window_seconds"] == 600

        # 2. First approve
        r = client.post(
            f"/admin/breakglass/{rid}/approve",
            headers={"X-Orchestra-Actor": "bob@apitest"},
            json={"rationale": "first signature"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "first-approved"

        # 3. Second approve
        r = client.post(
            f"/admin/breakglass/{rid}/approve",
            headers={"X-Orchestra-Actor": "carol@apitest"},
            json={"rationale": "second signature"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "active"

        # 4. GET
        r = client.get(f"/admin/breakglass/{rid}")
        assert r.status_code == 200
        assert r.json()["state"] == "active"
        assert r.json()["second_approver"] == "carol@apitest"
    finally:
        _bg_cleanup(client, tid)


def test_api_break_glass_rejects_disable_egress_pep(client: Any) -> None:
    """API surface: a ``disable_egress_pep`` effect is rejected
    with 400 (the service raises ``BreakGlassEffectRejected``;
    the API wraps it in an HTTPException).
    """
    r = client.post(
        "/admin/breakglass",
        headers={"X-Orchestra-Actor": "alice@apitest"},
        json={
            "tenant_id": "tenant:m24:apitest",
            "purpose": "smuggle-attempt",
            "effect": {"disable_egress_pep": True},
            "resource_scope": {"resource_kind": "artifact", "resource_id": "art-1"},
        },
    )
    assert r.status_code == 400, r.text
    assert "disable_egress_pep" in r.json()["detail"]


def test_api_break_glass_revoke_then_double_revoke(client: Any) -> None:
    """API: revoke succeeds, second revoke is 409 ``already_terminal``."""
    tid = f"tenant:m24:revoke:{new_id()[:6]}"
    try:
        r = client.post(
            "/admin/breakglass",
            headers={"X-Orchestra-Actor": "alice@apitest"},
            json={
                "tenant_id": tid,
                "purpose": "revoke-then-revoke",
                "effect": {"kind": "override_egress_view", "view": "egress.internal"},
                "resource_scope": {"resource_kind": "artifact", "resource_id": "art-1"},
            },
        )
        rid = r.json()["request_id"]
        # First revoke (from "requested" state).
        r = client.post(
            f"/admin/breakglass/{rid}/revoke",
            headers={"X-Orchestra-Actor": "bob@apitest"},
            json={"reason": "false alarm"},
        )
        assert r.status_code == 200, r.text
        # Second revoke must be rejected.
        r = client.post(
            f"/admin/breakglass/{rid}/revoke",
            headers={"X-Orchestra-Actor": "carol@apitest"},
            json={"reason": "second try"},
        )
        assert r.status_code == 409, r.text
        assert "already_terminal" in r.json()["detail"]
    finally:
        _bg_cleanup(client, tid)


def test_api_break_glass_get_missing_returns_404(client: Any) -> None:
    """W2-Gate scenario #9 (API path)."""
    r = client.get("/admin/breakglass/bg:nonexistent")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# CLI smoke test (no network — invokes the subcommand directly)
# ---------------------------------------------------------------------------


def test_cli_break_glass_request_parses_json() -> None:
    """The CLI's ``--effect`` / ``--resource-scope`` must accept
    valid JSON. ``--actor`` overrides the default ``cli``.
    """
    from orchestra.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "breakglass",
            "request",
            "--tenant",
            "tenant:acme",
            "--purpose",
            "incident-x",
            "--effect",
            json.dumps({"kind": "override_egress_view", "view": "egress.internal"}),
            "--resource-scope",
            json.dumps({"resource_kind": "artifact", "resource_id": "art-1"}),
            "--actor",
            "alice",
        ]
    )
    assert args.tenant == "tenant:acme"
    assert args.purpose == "incident-x"
    assert args.effect == json.dumps(
        {"kind": "override_egress_view", "view": "egress.internal"}
    )
    assert args.resource_scope == json.dumps(
        {"resource_kind": "artifact", "resource_id": "art-1"}
    )
    assert args.actor == "alice"


def test_cli_break_glass_sweep_and_approve_parses() -> None:
    """Smoke: ``sweep`` and ``approve`` subcommands parse."""
    from orchestra.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        ["breakglass", "sweep"]
    )
    assert args.command == "breakglass"
    assert args.breakglass_command == "sweep"
    args = parser.parse_args(
        [
            "breakglass",
            "approve",
            "bg:abcdef123456",
            "--actor",
            "bob",
            "--rationale",
            "looks legit",
        ]
    )
    assert args.request_id == "bg:abcdef123456"
    assert args.actor == "bob"
    assert args.rationale == "looks legit"
