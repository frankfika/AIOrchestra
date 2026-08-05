"""M0.3 — Execution Event + Receipt schema (SPEC-003 acceptance)."""
from __future__ import annotations

import json

import pytest

from orchestra.core.hashing import hmac_keygen, hmac_sign
from orchestra.core.ids import digest_json
from orchestra.core.schema import (
    AuditEvent,
    EventKind,
    NodeGrant,
    SecurityLabel,
    SignedReceipt,
)


def test_event_carries_chain_pointer():
    a = AuditEvent(
        task_run_id="t1", node_run_id="n1", kind=EventKind.NODE_STARTED,
        payload={"node_id": "n1"},
    )
    b = AuditEvent(
        task_run_id="t1", node_run_id="n1", kind=EventKind.NODE_SUCCEEDED,
        payload={"node_id": "n1"}, prev_event_id=a.event_id,
    )
    assert b.prev_event_id == a.event_id
    # event_id is random per event
    assert a.event_id != b.event_id


def test_event_payload_is_json_serialisable():
    a = AuditEvent(
        task_run_id="t1", kind=EventKind.POLICY_DECISION,
        payload={"allow": False, "rule_id": "no-restricted-to-public", "invariant": "1"},
    )
    raw = a.model_dump_json()
    assert "no-restricted-to-public" in raw


def test_receipt_payload_digest_is_deterministic():
    from orchestra.coordinator.receipt import ReceiptBuilder

    key = hmac_keygen()
    rb = ReceiptBuilder(key)
    a = rb.build(
        task_run_id="t", node_run_id="n", node_id="n",
        plan_digest="plan:abc", capability_id="c1", manifest_id="m:c1",
        data_view={"name": "v", "shape": "reference", "fields": []},
        inputs={"x": 1}, outputs={"y": 2},
        started_at="2026-01-01T00:00:00.000Z",
        ended_at="2026-01-01T00:00:01.000Z",
        status="succeeded",
    )
    b = rb.build(
        task_run_id="t", node_run_id="n", node_id="n",
        plan_digest="plan:abc", capability_id="c1", manifest_id="m:c1",
        data_view={"name": "v", "shape": "reference", "fields": []},
        inputs={"x": 1}, outputs={"y": 2},
        started_at="2026-01-01T00:00:00.000Z",
        ended_at="2026-01-01T00:00:01.000Z",
        status="succeeded",
    )
    assert a.envelope["payload"]["inputs_digest"] == b.envelope["payload"]["inputs_digest"]
    assert a.envelope["payload"]["outputs_digest"] == b.envelope["payload"]["outputs_digest"]
    assert a.receipt_id != b.receipt_id  # receipts are unique per node run
