"""M18 — Webhook delivery history tests.

A partner whose webhook never fires has no way to know
why unless the dev path keeps a record of past
attempts. The :class:`DeliveryHistory` ring buffer
records every attempt; the
``GET /admin/webhooks/{task_id}`` endpoint surfaces
the records to the partner.

The tests below cover the buffer primitive and the
API endpoint shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from orchestra.webhooks import DeliveryHistory, WebhookDeliveryRecord


# ---------------------------------------------------------------------------
# WebhookDeliveryRecord
# ---------------------------------------------------------------------------


def test_record_to_dict_carries_all_fields():
    r = WebhookDeliveryRecord(
        delivery_id="d-1",
        task_run_id="t-1",
        state="succeeded",
        delivered=True,
        attempts=1,
        last_status=200,
        error="",
        attempt_started_at="2026-08-06T01:00:00+00:00",
    )
    d = r.to_dict()
    assert d["delivery_id"] == "d-1"
    assert d["task_run_id"] == "t-1"
    assert d["state"] == "succeeded"
    assert d["delivered"] is True
    assert d["attempts"] == 1
    assert d["last_status"] == 200
    assert d["error"] == ""
    assert d["attempt_started_at"] == "2026-08-06T01:00:00+00:00"


def test_record_from_delivery_round_trips():
    @dataclass
    class FakeDelivery:
        delivered: bool
        attempts: int
        last_status: int
        error: str

    delivery = FakeDelivery(delivered=False, attempts=3, last_status=503, error="HTTP 503")
    r = WebhookDeliveryRecord.from_delivery(
        delivery, task_run_id="t-2", state="failed", delivery_id="d-2"
    )
    assert r.task_run_id == "t-2"
    assert r.state == "failed"
    assert r.delivered is False
    assert r.attempts == 3
    assert r.last_status == 503
    assert r.error == "HTTP 503"
    assert r.attempt_started_at  # ISO 8601 timestamp set on construction


# ---------------------------------------------------------------------------
# DeliveryHistory
# ---------------------------------------------------------------------------


def test_history_records_per_task():
    h = DeliveryHistory()
    h.record(
        WebhookDeliveryRecord(
            delivery_id="d1",
            task_run_id="tA",
            state="succeeded",
            delivered=True,
            attempts=1,
            last_status=200,
        )
    )
    h.record(
        WebhookDeliveryRecord(
            delivery_id="d2",
            task_run_id="tA",
            state="succeeded",
            delivered=True,
            attempts=1,
            last_status=200,
        )
    )
    h.record(
        WebhookDeliveryRecord(
            delivery_id="d3",
            task_run_id="tB",
            state="failed",
            delivered=False,
            attempts=3,
            last_status=503,
            error="HTTP 503",
        )
    )
    out_a = h.for_task("tA")
    out_b = h.for_task("tB")
    assert len(out_a) == 2
    assert len(out_b) == 1
    assert [r.delivery_id for r in out_a] == ["d1", "d2"]
    assert out_b[0].delivery_id == "d3"


def test_history_for_unknown_task_returns_empty():
    h = DeliveryHistory()
    assert h.for_task("never-seen") == []


def test_history_ring_buffer_drops_oldest():
    """The cap holds the buffer bounded; the oldest
    record is dropped when the cap is hit."""
    h = DeliveryHistory(max_per_task=2)
    for i in range(5):
        h.record(
            WebhookDeliveryRecord(
                delivery_id=f"d{i}",
                task_run_id="tA",
                state="succeeded",
                delivered=True,
                attempts=1,
                last_status=200,
            )
        )
    out = h.for_task("tA")
    assert len(out) == 2
    # The cap drops the oldest, so the latest 2 are d3 and d4.
    assert [r.delivery_id for r in out] == ["d3", "d4"]


def test_history_isolates_tasks():
    """One task's records don't leak into another's."""
    h = DeliveryHistory()
    h.record(
        WebhookDeliveryRecord(
            delivery_id="x",
            task_run_id="tA",
            state="succeeded",
            delivered=True,
            attempts=1,
            last_status=200,
        )
    )
    out = h.for_task("tB")
    assert out == []


def test_history_all_keys_lists_seen_tasks():
    h = DeliveryHistory()
    h.record(
        WebhookDeliveryRecord(
            delivery_id="d",
            task_run_id="tA",
            state="succeeded",
            delivered=True,
            attempts=1,
            last_status=200,
        )
    )
    h.record(
        WebhookDeliveryRecord(
            delivery_id="d",
            task_run_id="tB",
            state="succeeded",
            delivered=True,
            attempts=1,
            last_status=200,
        )
    )
    assert sorted(h.all_keys()) == ["tA", "tB"]


# ---------------------------------------------------------------------------
# API endpoint
# ---------------------------------------------------------------------------


def test_admin_webhook_history_returns_empty_for_unknown_task():
    from fastapi.testclient import TestClient
    from orchestra.api.app import create_app, AppState

    class StubStore:
        def get_task_run(self, _tid):
            return None

        def close(self):
            pass

    state = AppState(store=StubStore(), coordinator=None, benchmark_runner=None)
    client = TestClient(create_app(state))
    r = client.get("/admin/webhooks/never-seen")
    assert r.status_code == 200
    body = r.json()
    assert body["task_run_id"] == "never-seen"
    assert body["count"] == 0
    assert body["deliveries"] == []


def test_admin_webhook_history_returns_recorded_deliveries():
    from fastapi.testclient import TestClient
    from orchestra.api.app import create_app, AppState

    class StubStore:
        def get_task_run(self, _tid):
            return None

        def close(self):
            pass

    state = AppState(store=StubStore(), coordinator=None, benchmark_runner=None)
    state.webhook_history = DeliveryHistory()
    state.webhook_history.record(
        WebhookDeliveryRecord(
            delivery_id="d-99",
            task_run_id="t-99",
            state="succeeded",
            delivered=True,
            attempts=1,
            last_status=200,
        )
    )
    client = TestClient(create_app(state))
    r = client.get("/admin/webhooks/t-99")
    assert r.status_code == 200
    body = r.json()
    assert body["task_run_id"] == "t-99"
    assert body["count"] == 1
    assert body["deliveries"][0]["delivery_id"] == "d-99"
    assert body["deliveries"][0]["state"] == "succeeded"
    assert body["deliveries"][0]["delivered"] is True


def test_admin_webhook_history_is_tagged_for_docs():
    """The endpoint must show up under the Admin tag in
    /docs so a SRE looking for delivery diagnostics
    finds it in the right group."""
    from fastapi.testclient import TestClient
    from orchestra.api.app import create_app, AppState

    class StubStore:
        def get_task_run(self, _tid):
            return None

        def close(self):
            pass

    state = AppState(store=StubStore(), coordinator=None, benchmark_runner=None)
    client = TestClient(create_app(state))
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/admin/webhooks/{task_run_id}"]["get"]
    assert "Admin" in op.get("tags", [])
    assert op.get("summary")  # non-empty summary for the docs sidebar
