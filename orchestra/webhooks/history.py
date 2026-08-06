"""M18 — Webhook delivery history.

A partner whose webhook never fires has no way to know
*why* unless the dev path keeps a record of past
attempts. The :class:`DeliveryHistory` is a per-task
ring buffer of :class:`WebhookDeliveryRecord` entries
that the API exposes via ``GET /admin/webhooks/{task_id}``.

The dev path is in-process; production swaps for a
durable store (Postgres / DynamoDB) without changing
the wire format the API returns.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Optional


@dataclass
class WebhookDeliveryRecord:
    """A single attempt's outcome.

    ``attempt_started_at`` is the wall-clock second the
    dispatcher issued the HTTP call (ISO 8601 with
    timezone). ``response_status`` is the last HTTP
    status the partner returned (0 on transport
    failure). ``error`` is the empty string on success;
    a non-empty string describes the last failure
    ("ConnectionError", "HTTP 503", etc.).

    The record is what the partner sees in
    ``GET /admin/webhooks/{task_id}`` so they can
    diagnose a misconfigured endpoint without reading
    server logs. The record also carries the partner's
    ``webhook_url`` + ``webhook_secret`` + the
    ``plan_id`` + ``node_results`` + ``error`` from
    the original payload, so an operator who fixes the
    partner endpoint can re-fire the original delivery
    via :meth:`DeliveryHistory.retry_last`.
    """

    delivery_id: str
    task_run_id: str
    state: str
    delivered: bool
    attempts: int
    last_status: int
    error: str = ""
    attempt_started_at: str = ""
    # M19 — the partner URL + secret + the original
    # payload fields. Stored on the record so a manual
    # retry reuses the partner's original config without
    # requiring the operator to re-supply it.
    webhook_url: str = ""
    webhook_secret: str = ""
    plan_id: str | None = None
    node_results: dict[str, Any] | None = None
    payload_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "task_run_id": self.task_run_id,
            "state": self.state,
            "delivered": self.delivered,
            "attempts": self.attempts,
            "last_status": self.last_status,
            "error": self.error,
            "attempt_started_at": self.attempt_started_at,
        }

    @classmethod
    def from_delivery(
        cls,
        delivery: Any,
        *,
        task_run_id: str,
        state: str,
        delivery_id: str,
        webhook_url: str = "",
        webhook_secret: str = "",
        plan_id: str | None = None,
        node_results: dict[str, Any] | None = None,
        payload_error: str | None = None,
    ) -> "WebhookDeliveryRecord":
        return cls(
            delivery_id=delivery_id,
            task_run_id=task_run_id,
            state=state,
            delivered=bool(getattr(delivery, "delivered", False)),
            attempts=int(getattr(delivery, "attempts", 0)),
            last_status=int(getattr(delivery, "last_status", 0)),
            error=str(getattr(delivery, "error", "")),
            attempt_started_at=datetime.now(timezone.utc).isoformat(),
            webhook_url=webhook_url,
            webhook_secret=webhook_secret,
            plan_id=plan_id,
            node_results=node_results or {},
            payload_error=payload_error,
        )


class DeliveryHistory:
    """A per-task ring buffer of delivery records.

    The dev path keeps the last ``max_per_task`` records
    per task. When a task's record count exceeds the cap
    the oldest record is dropped — a SRE who needs older
    history reads it from the durable store in production.
    The cap defaults to 16 because a partner who retries
    3 times per task sees at most 1 record per task per
    state transition; 16 covers a full retry budget
    across 5 state transitions before rotating.
    """

    def __init__(self, *, max_per_task: int = 16) -> None:
        self._max = max_per_task
        self._lock = threading.Lock()
        self._by_task: dict[str, Deque[WebhookDeliveryRecord]] = {}

    def record(
        self,
        record: WebhookDeliveryRecord,
    ) -> None:
        """Append a record. The deque is bounded; the
        oldest record is dropped when the cap is hit."""
        with self._lock:
            buf = self._by_task.setdefault(record.task_run_id, deque(maxlen=self._max))
            buf.append(record)

    def for_task(self, task_run_id: str) -> list[WebhookDeliveryRecord]:
        """Return the records for a task, oldest first."""
        with self._lock:
            buf = self._by_task.get(task_run_id)
            if buf is None:
                return []
            return list(buf)

    def last_failed(self, task_run_id: str) -> WebhookDeliveryRecord | None:
        """Return the most recent failed (i.e. not
        delivered) record for a task, or ``None`` if the
        task has no failed deliveries.

        M19 — a manual retry endpoint uses this to find
        the record to re-fire. The contract is "the
        latest failure" because operators usually want
        to retry the most recent payload (which carries
        the most up-to-date state)."""
        with self._lock:
            buf = self._by_task.get(task_run_id)
            if buf is None:
                return None
            for record in reversed(buf):
                if not record.delivered:
                    return record
            return None

    def all_keys(self) -> list[str]:
        """The list of task ids with at least one record."""
        with self._lock:
            return list(self._by_task.keys())
