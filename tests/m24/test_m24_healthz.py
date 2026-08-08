"""M24 W4 — /healthz exposes the M24 backlog signals.

The healthz probe in M11 only knew about capabilities,
tenants, and published cards. M24 added three signals
that a SRE wants to see on every probe:

* ``orchestra_m24_pending_approvals`` — persistent
  approvals still waiting on a human.
* ``orchestra_m24_deletion_backlog`` — DeletionJobs in
  ``pending`` or ``running`` state.
* ``orchestra_m24_active_break_glass`` — break-glass
  grants still inside the active window.

The body must include all three; the test below fails the
build if a regression drops any of them.
"""
from __future__ import annotations

from starlette.testclient import TestClient

from orchestra.api.app import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def test_healthz_includes_m24_backlog_signals() -> None:
    client = _client()
    r = client.get("/healthz")
    assert r.status_code == 200, r.text
    body = r.json()
    # M24 milestone is reported.
    assert body.get("milestone") == "M24"
    # The m24_backlog check is present and has the three counts.
    check = body.get("checks", {}).get("m24_backlog")
    assert check is not None, f"m24_backlog check missing: {body}"
    for key in (
        "pending_approvals",
        "deletion_jobs_in_flight",
        "active_break_glass_grants",
    ):
        assert key in check, f"missing M24 key: {key}"
        assert isinstance(check[key], int)
        assert check[key] >= 0


def test_healthz_overall_status_shape() -> None:
    """The overall response must still be ``ok`` or ``degraded``
    and must include the checks map. This is the contract a
    SRE probe and the load balancer rely on.
    """
    client = _client()
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in {"ok", "degraded"}
    assert isinstance(body["checks"], dict)
