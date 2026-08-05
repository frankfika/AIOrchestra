"""M2 — Fenced Runtime (RUN-001/002, IDN-001, EVD-001, EVD-002).

P0 had a single-process Coordinator with no Lease, no Fencing
Token, no Outbox, no Reconciler, and no Merkle log. M2 adds:

  - :class:`Lease` with a monotonically-increasing Fencing Token
  - :class:`FencingGuard` that rejects stale Workers
  - :class:`Outbox` (transactional): the Coordinator writes
    events to the Outbox; a Dispatcher flushes them to the
    Event Store; failures are retried with backoff
  - :class:`Reconciler` that periodically checks Node Run state
    and either drives the Coordinator forward (Unknown handling)
    or surfaces the gap to the audit timeline
  - :class:`CredentialBroker` (IDN-001) that issues + verifies
    Node Grants with proper rotation semantics
  - :class:`MerkleLog` (EVD-001) that chains Event hashes for
    per-tenant (Cell) tamper-evidence
  - :class:`verify_receipt_offline` CLI (EVD-002) that re-verifies
    a signed Receipt without DB access
"""
from orchestra.runtime.lease import Lease, LeaseState, FencingToken
from orchestra.runtime.fencing import FencingGuard, StaleFencingToken
from orchestra.runtime.outbox import Outbox, OutboxEntry
from orchestra.runtime.reconciler import Reconciler, ReconcilerReport
from orchestra.runtime.credential_broker import CredentialBroker
from orchestra.runtime.state_machine import TaskRunStateMachine, NodeRunStateMachine
from orchestra.evidence.merkle import MerkleLog, MerkleProof

__all__ = [
    "Lease",
    "LeaseState",
    "FencingToken",
    "FencingGuard",
    "StaleFencingToken",
    "Outbox",
    "OutboxEntry",
    "Reconciler",
    "ReconcilerReport",
    "CredentialBroker",
    "TaskRunStateMachine",
    "NodeRunStateMachine",
    "MerkleLog",
    "MerkleProof",
]
