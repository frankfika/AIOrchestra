"""Event Store + Receipt tests (LIT-004)."""
from __future__ import annotations

import os
import uuid

import pytest

from orchestra.coordinator.event_store import EventStore, EventStoreUnavailable
from orchestra.core.hashing import hmac_keygen
from orchestra.core.ids import new_id
from orchestra.core.schema import (
    AuditEvent,
    EventKind,
    NodeRunState,
    SignedReceipt,
    TaskRunState,
)
from orchestra.coordinator.receipt import ReceiptBuilder


pytestmark = pytest.mark.e2e


def test_event_store_round_trip(dsn):
    store = EventStore(dsn)
    store.connect()
    task_run_id = f"trun-{uuid.uuid4().hex[:10]}"
    node_run_id = f"nrun-{uuid.uuid4().hex[:10]}"
    try:
        store.upsert_task_run(task_run_id, "c1", "t1", TaskRunState.CREATED, plan_id=None)
        store.update_task_state(task_run_id, TaskRunState.RUNNING)
        store.upsert_node_run(node_run_id, task_run_id, "n1", NodeRunState.RUNNING, "c1", "m:c1")
        ev = AuditEvent(task_run_id=task_run_id, node_run_id=node_run_id, kind=EventKind.NODE_STARTED, payload={"x": 1})
        store.append_event(ev)
        events = store.list_events(task_run_id=task_run_id)
        assert len(events) == 1
        assert events[0]["kind"] == "node.started"
        # seq should be 1
        assert events[0]["seq"] == 1
    finally:
        # Cleanup
        with store._tx() as c, c.cursor() as cur:
            cur.execute("DELETE FROM task_runs WHERE task_run_id=%s", (task_run_id,))
        store.close()


def test_receipt_round_trip():
    key = hmac_keygen()
    rb = ReceiptBuilder(key)
    r = rb.build(
        task_run_id="t", node_run_id="n", node_id="n1",
        plan_digest="plan:abc", capability_id="c1", manifest_id="m:c1",
        data_view={"name": "v", "shape": "reference", "fields": []},
        inputs={"x": 1}, outputs={"y": 2},
        started_at="2026-01-01T00:00:00.000Z",
        ended_at="2026-01-01T00:00:01.000Z",
        status="succeeded",
    )
    assert rb.verify(r)
    # tamper
    bad = SignedReceipt(
        receipt_id=r.receipt_id, task_run_id=r.task_run_id, node_run_id=r.node_run_id,
        node_id=r.node_id, envelope={**r.envelope, "signature": "x" * 30},
        created_at=r.created_at,
    )
    assert not rb.verify(bad)
