"""LIT-004: Minimal Interaction Coordinator + Event Store + Receipts.

The Coordinator owns the *lifecycle* of a Plan. It:

1. Walks the Plan in topological order.
2. Asks the :class:`~orchestra.registry.router.Router` to bind each node.
3. Issues a :class:`~orchestra.core.schema.NodeGrant` for the bound
   capability.
4. Invokes the Adapter and collects the output.
5. Emits an :class:`~orchestra.core.schema.AuditEvent` for every state
   transition.
6. Builds a signed :class:`~orchestra.core.schema.SignedReceipt` per node.

The Event Store is PostgreSQL. The Receipt is a COSE-like HMAC envelope
(see :mod:`orchestra.core.hashing`).
"""
from orchestra.coordinator.engine import Coordinator, CoordinatorResult
from orchestra.coordinator.event_store import EventStore, EventStoreUnavailable
from orchestra.coordinator.node_grant import NodeGrantIssuer
from orchestra.coordinator.receipt import ReceiptBuilder

__all__ = [
    "EventStore",
    "EventStoreUnavailable",
    "NodeGrantIssuer",
    "ReceiptBuilder",
    "Coordinator",
    "CoordinatorResult",
]
