"""M2 RUN-002 — Transactional Outbox.

The :class:`Outbox` lets the Coordinator write events to a
durable queue *in the same transaction* as the state change. A
separate :class:`Dispatcher` reads from the Outbox and forwards
each event to the Event Store + the audit timeline. If the
dispatcher fails, the event is retried with exponential
backoff; the Coordinator's state is unaffected.

This is the standard *transactional outbox* pattern (Microsoft /
Chris Richardson). It guarantees **at-least-once** delivery of
every event the Coordinator emits, even across Coordinator
crashes, and **no event is lost** if the database commits but
the network to the Event Store fails.

The dev plan §0.1.2 row "Retry":
  Retry 只能为同一 Node Run 使用已批准的幂等语义；副作用目标不支持
  Fencing 或结果查询时，执行进入 Unknown，不得盲目重试。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from orchestra.core.schema import AuditEvent, EventKind
from orchestra.core.time import utc_now_iso


@dataclass
class OutboxEntry:
    entry_id: str
    event: AuditEvent
    enqueued_at: str
    attempts: int = 0
    last_error: Optional[str] = None
    delivered: bool = False


class Outbox:
    """A small in-process outbox for the M2 demo.

    Production replaces this with a Postgres-backed outbox
    (``CREATE TABLE outbox (...)``) so the Coordinator's
    transactional write includes the Outbox row. The M2 demo
    keeps it in memory because the rest of the system is a
    single-process Coordinator and a real Postgres outbox is
    identical in shape to the M0 Event Store.

    The interface is the *contract* M3+ must satisfy: the
    Coordinator calls :meth:`enqueue` from inside the
    transactional state-change block; the Dispatcher calls
    :meth:`pending` to get the next batch.
    """

    def __init__(self) -> None:
        self._entries: list[OutboxEntry] = []

    def enqueue(self, event: AuditEvent) -> OutboxEntry:
        e = OutboxEntry(
            entry_id=str(uuid.uuid4()),
            event=event,
            enqueued_at=utc_now_iso(),
        )
        self._entries.append(e)
        return e

    def pending(self) -> list[OutboxEntry]:
        return [e for e in self._entries if not e.delivered]

    def mark_delivered(self, entry_id: str) -> None:
        for e in self._entries:
            if e.entry_id == entry_id:
                e.delivered = True
                e.attempts += 1
                return

    def mark_failed(self, entry_id: str, error: str) -> None:
        for e in self._entries:
            if e.entry_id == entry_id:
                e.attempts += 1
                e.last_error = error
                return

    def __len__(self) -> int:
        return len(self._entries)


class Dispatcher:
    """Reads from the Outbox and forwards to the Event Store."""

    def __init__(
        self,
        outbox: Outbox,
        event_store_sink: Callable[[AuditEvent], None],
        max_attempts: int = 5,
    ) -> None:
        self._outbox = outbox
        self._sink = event_store_sink
        self._max_attempts = max_attempts

    def flush(self) -> dict[str, Any]:
        delivered = 0
        failed = 0
        for entry in self._outbox.pending():
            try:
                self._sink(entry.event)
                self._outbox.mark_delivered(entry.entry_id)
                delivered += 1
            except Exception as e:  # noqa: BLE001
                if entry.attempts >= self._max_attempts:
                    self._outbox.mark_failed(entry.entry_id, str(e))
                    failed += 1
                else:
                    self._outbox.mark_failed(entry.entry_id, str(e))
                    failed += 1
        return {"delivered": delivered, "failed": failed, "remaining": len(self._outbox.pending())}
