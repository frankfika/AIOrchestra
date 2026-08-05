"""M2 RUN-002 — Fencing guard.

The :class:`FencingGuard` is the runtime check the Event Store
and the Outbox perform on every write. It compares the
incoming :class:`FencingToken` against the highest token it has
already accepted for the same ``cell_id`` (or, when
``per_node_run`` is True, for the same ``node_run_id``).

A stale token is rejected with :class:`StaleFencingToken`. This
prevents a *zombie* Worker (one that lost its Lease but kept
trying) from advancing the state machine.

Invariant #21 (Adapter/Runtime 被攻陷也不能访问计划外资源) is
the motivating invariant. The Fencing Guard is one of its
enforcement points; the other is the Node Grant.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from orchestra.core.errors import OrchestraError
from orchestra.runtime.lease import FencingToken


class StaleFencingToken(OrchestraError):
    """The incoming token is below the highest accepted token."""


@dataclass
class FencingGuard:
    """Tracks the highest token accepted per cell and per node-run.

    The guard is **monotonic** for the lifetime of the process
    (or until :meth:`reset` is called by the Reconciler after a
    failed Worker has been confirmed dead).
    """

    _high_water_per_cell: dict[str, int] = field(default_factory=dict)
    _high_water_per_node_run: dict[str, int] = field(default_factory=dict)

    def check(
        self,
        token: FencingToken,
        node_run_id: str,
    ) -> None:
        cell_high = self._high_water_per_cell.get(token.cell_id, -1)
        node_high = self._high_water_per_node_run.get(node_run_id, -1)
        if token.value < cell_high or token.value < node_high:
            raise StaleFencingToken(
                f"token {token} rejected: cell_high={cell_high}, "
                f"node_high={node_high}"
            )
        # Accept and advance.
        if token.value > cell_high:
            self._high_water_per_cell[token.cell_id] = token.value
        if token.value > node_high:
            self._high_water_per_node_run[node_run_id] = token.value

    def current_high_water(self, cell_id: str) -> int:
        return self._high_water_per_cell.get(cell_id, -1)

    def reset_node_run(self, node_run_id: str) -> None:
        """Drop the per-node-run high-water (used by the Reconciler
        when a Worker is confirmed dead).
        """
        self._high_water_per_node_run.pop(node_run_id, None)
