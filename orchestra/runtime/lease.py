"""M2 RUN-002 — Lease + Fencing Token.

A :class:`Lease` is a *short-lived, single-Worker* execution
permit for a Node Run. The Lease carries a :class:`FencingToken`
— a monotonically-increasing integer — that the Worker must
present to the Event Store and the Coordinator on every state
change. A Worker holding a stale token is rejected
(:class:`StaleFencingToken`).

Why a Token and not just a Capability reference? Because the
Capability reference does not change when the Lease is revoked
mid-execution; only the Fencing Token does. A Worker that
"survived" a revocation cannot affect state with a stale Token.

The dev plan §0.1.2 row "Lease" says:
  ``Lease`` — 某一次 Node Run 的执行租约，携带单调递增 Fencing Token；
            Grant 不等于 Lease，二者不能互相替代

So the Lease is per-Node-Run, not per-Node-Grant. The Grant is
the *authorisation* (what the Worker may do); the Lease is the
*concurrency token* (which Worker is currently driving this Run).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class LeaseState(str, Enum):
    PENDING = "pending"          # issued, not yet claimed by a Worker
    ACTIVE = "active"            # claimed by a Worker
    EXPIRED = "expired"          # heartbeat timeout
    REVOKED = "revoked"          # admin Kill Switch (invariant #14)
    COMPLETED = "completed"      # Node Run reached a terminal state


@dataclass
class FencingToken:
    """Monotonically increasing integer scoped to a Cell (tenant)."""

    cell_id: str
    value: int

    def __post_init__(self) -> None:
        if self.value < 0:
            raise ValueError("FencingToken value must be non-negative")

    def __str__(self) -> str:
        return f"{self.cell_id}:{self.value}"


@dataclass
class Lease:
    """A short-lived, single-Worker execution permit for a Node Run."""

    lease_id: str
    task_run_id: str
    node_run_id: str
    cell_id: str
    fencing_token: FencingToken
    state: LeaseState = LeaseState.PENDING
    issued_at: str = ""
    claimed_at: Optional[str] = None
    expires_at: str = ""
    worker_id: Optional[str] = None
    heartbeat_count: int = 0
    revoked_reason: Optional[str] = None
