"""M16 — Orchestra Partner SDK (Python).

A thin, typed client for the M4 AgenticHub HTTP shape (and the
JSON API it shadows). The SDK turns the standard error
envelope (RFC 7807) into typed exceptions so a partner
application handles failures the same way it handles any
other library call:

    from orchestra_sdk import OrchestraClient
    from orchestra_sdk.errors import RateLimitError, TaskNotFound

    with OrchestraClient(base_url="https://api.partner-a.com", tenant_id="acme") as client:
        task = client.submit_task(
            contract_id="ctr-001",
            contract_text=open("contract.txt").read(),
            vendor_id="vendor-42",
        )
        final = client.wait_for_completion(task.task_run_id, timeout=300)
        if final.state == "succeeded":
            print(final.node_results)

Design notes:

  * The SDK uses :mod:`httpx` (already a server dep) so a
    partner who runs the server and the SDK in the same
    environment doesn't pull a second HTTP client. ``httpx``
    is the modern choice and handles connection pooling +
    keepalive out of the box.
  * The SDK is **stateless**: the :class:`OrchestraClient`
    holds a base URL + a tenant id and creates a fresh
    :class:`httpx.Client` per session. A partner that
    wants a long-lived client can pass ``httpx.Client(...)``
    to the constructor and reuse it.
  * Error parsing is centralised in :func:`_raise_for_status`
    so every method raises the same exception types. A
    partner can catch :class:`OrchestraError` to handle
    every Orchestra-specific failure.
  * No business logic. The SDK is a transport wrapper; the
    server is the source of truth for what a task is, what
    the approval flow looks like, and what the receipt
    structure is. Adding a method here is "POST /foo with
    body X, return JSON parsed as Y" — nothing more.
"""

from __future__ import annotations

from typing import Any

from orchestra_sdk.client import (
    OrchestraClient,
    TaskStatus,
    TaskEvent,
    TaskReceipt,
    NodeGrant,
    Approval,
)
from orchestra_sdk.errors import (
    OrchestraError,
    RateLimitError,
    PayloadTooLargeError,
    TaskNotFoundError,
    ValidationError,
    NotFoundError,
    DependencyFailureError,
    InternalError,
    ProblemDetail,
)

__all__ = [
    "OrchestraClient",
    "TaskStatus",
    "TaskEvent",
    "TaskReceipt",
    "NodeGrant",
    "Approval",
    "OrchestraError",
    "RateLimitError",
    "PayloadTooLargeError",
    "TaskNotFoundError",
    "ValidationError",
    "NotFoundError",
    "DependencyFailureError",
    "InternalError",
    "ProblemDetail",
]
