"""M17 — Webhook delivery.

When a task reaches a terminal state, the Coordinator
hands the result to :class:`WebhookDispatcher`, which
POSTs it to the partner-configured URL. The delivery is
signed with HMAC-SHA-256 so the partner can verify the
body they receive is the body the server sent.

Design notes:

  * The dispatcher is **synchronous** in the dev path —
    ``deliver()`` blocks until the partner's webhook
    endpoint returns 2xx or the retry budget is spent.
    A production swap plugs in a real queue (Redis /
    SQS / Kafka) and an out-of-process worker; the
    signature + payload format is unchanged.
  * The payload includes a unique ``delivery_id`` per
    attempt. A partner who re-delivers can dedupe on it
    (idempotency key).
  * The signature is in the ``X-Orchestra-Signature``
    header as ``sha256=<hex>``. The partner computes
    ``HMAC(secret, body)`` and compares; the body is the
    raw POST bytes (not the JSON-stringified version).
  * Retry budget: 3 attempts with exponential backoff
    (1s, 2s, 4s). After 3 failures the delivery is
    recorded as failed and the partner can query
    ``GET /admin/webhooks/{task_id}`` (TODO in M18) to
    see the failure.

The :class:`WebhookDispatcher` is intentionally
dependency-free: no Celery, no RQ. A pilot partner
whose webhook is a single Lambda function or an API
Gateway route works out of the box.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import httpx


# The default retry schedule. ``MAX_ATTEMPTS`` is the
# total number of tries (the initial attempt + retries);
# ``BACKOFF_SCHEDULE[i]`` is the seconds to wait before
# attempt ``i`` (0-indexed).
MAX_ATTEMPTS = 3
BACKOFF_SCHEDULE: tuple[float, ...] = (1.0, 2.0, 4.0)

# The header name. The prefix mirrors the convention
# Stripe / GitHub use so a partner who already verifies
# webhooks from those vendors only changes the secret.
SIGNATURE_HEADER = "X-Orchestra-Signature"

# The header carrying the unique delivery id. A partner
# stores this + the body and dedupes on retries.
DELIVERY_ID_HEADER = "X-Orchestra-Delivery-Id"

# The header carrying the event type. ``task.succeeded``,
# ``task.failed``, ``task.cancelled`` are the three
# terminal-state event types a partner might bind to.
EVENT_TYPE_HEADER = "X-Orchestra-Event-Type"


@dataclass
class WebhookConfig:
    """The partner-supplied webhook configuration.

    A partner registers a URL when submitting a task. The
    secret is the HMAC key the partner generated; the
    server uses it to sign the body. A partner who
    doesn't want signed deliveries can pass any string
    (the signature still validates, but the partner
    ignores it on receive).
    """

    url: str
    secret: str

    def is_valid(self) -> bool:
        # Defensive: a malformed URL or empty secret is a
        # misconfiguration a SRE should catch at submit
        # time, not at delivery time.
        return bool(self.url) and bool(self.secret) and self.url.startswith(("http://", "https://"))


@dataclass
class WebhookDelivery:
    """The result of a :meth:`WebhookDispatcher.deliver` call.

    ``delivered`` is True when the partner's endpoint
    returned 2xx within the retry budget. ``attempts`` is
    the number of HTTP calls actually made. ``last_status``
    is the last HTTP status the partner returned (0 if
    every attempt failed at the transport layer).
    ``delivery_id`` is the unique id; a partner dedupes
    on it.
    """

    delivered: bool
    attempts: int
    last_status: int
    delivery_id: str
    error: str = ""


def sign_body(secret: str, body: bytes) -> str:
    """Return the ``sha256=<hex>`` signature for ``body``."""
    sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def build_payload(
    *,
    task_run_id: str,
    state: str,
    plan_id: str | None,
    node_results: dict[str, Any],
    error: str | None,
    delivery_id: str,
) -> dict[str, Any]:
    """Build the JSON body the partner receives.

    The shape is stable across server versions; adding
    a field is non-breaking, removing / renaming is.
    The ``task_run_id`` and ``state`` are the two
    fields a partner definitely needs; the rest is
    context.
    """
    return {
        "event": f"task.{state}",
        "delivery_id": delivery_id,
        "task_run_id": task_run_id,
        "state": state,
        "plan_id": plan_id,
        "node_results": node_results,
        "error": error,
        "delivered_at": int(time.time()),
    }


class WebhookDispatcher:
    """POSTs task-completion payloads to a partner URL.

    The dispatcher is constructed once and shared across
    tasks. Each :meth:`deliver` call is independent
    (the retry loop is per-call, not per-dispatcher) so
    a flaky partner doesn't slow down deliveries to a
    healthy one.
    """

    def __init__(
        self,
        *,
        http_client: Optional[httpx.Client] = None,
        max_attempts: int = MAX_ATTEMPTS,
        backoff_schedule: tuple[float, ...] = BACKOFF_SCHEDULE,
        sleep: Callable[[float], None] = time.sleep,
        # ``on_attempt`` is a debug hook tests use to fast-
        # forward through the backoff without sleeping. The
        # dispatcher itself doesn't log per-attempt; the
        # partner-side observability is the body the
        # partner receives.
        on_attempt: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(timeout=10.0)
        self._max_attempts = max_attempts
        self._backoff = backoff_schedule
        self._sleep = sleep
        self._on_attempt = on_attempt

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> "WebhookDispatcher":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def deliver(
        self,
        config: WebhookConfig,
        *,
        task_run_id: str,
        state: str,
        plan_id: str | None,
        node_results: dict[str, Any],
        error: str | None,
        delivery_id: str,
    ) -> WebhookDelivery:
        """POST a single terminal-state payload with retry.

        Returns a :class:`WebhookDelivery` describing the
        outcome. The partner-side observability is the
        body they receive; the dispatcher doesn't log
        per-attempt because a SRE who needs that
        diagnostic has it on the partner's side.
        """
        if not config.is_valid():
            return WebhookDelivery(
                delivered=False,
                attempts=0,
                last_status=0,
                delivery_id=delivery_id,
                error="invalid webhook config (url or secret missing or scheme not http/https)",
            )
        payload = build_payload(
            task_run_id=task_run_id,
            state=state,
            plan_id=plan_id,
            node_results=node_results,
            error=error,
            delivery_id=delivery_id,
        )
        # ``sort_keys=True`` so the body is deterministic;
        # the partner's signature verification is robust
        # against server-side key-order changes.
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        signature = sign_body(config.secret, body)
        headers = {
            "Content-Type": "application/json",
            SIGNATURE_HEADER: signature,
            DELIVERY_ID_HEADER: delivery_id,
            EVENT_TYPE_HEADER: f"task.{state}",
        }
        last_status = 0
        last_error = ""
        attempts = 0
        for attempt in range(self._max_attempts):
            if self._on_attempt is not None:
                self._on_attempt(attempt, self._max_attempts)
            attempts += 1
            try:
                response = self._http.post(config.url, content=body, headers=headers)
                last_status = response.status_code
                if 200 <= response.status_code < 300:
                    return WebhookDelivery(
                        delivered=True,
                        attempts=attempts,
                        last_status=response.status_code,
                        delivery_id=delivery_id,
                    )
                last_error = f"HTTP {response.status_code}"
            except httpx.HTTPError as e:
                last_error = f"{type(e).__name__}: {e}"
            # Backoff before the next attempt (skip after
            # the last one).
            if attempt + 1 < self._max_attempts:
                self._sleep(self._backoff[min(attempt, len(self._backoff) - 1)])
        return WebhookDelivery(
            delivered=False,
            attempts=attempts,
            last_status=last_status,
            delivery_id=delivery_id,
            error=last_error,
        )
