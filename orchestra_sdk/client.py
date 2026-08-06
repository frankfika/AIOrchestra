"""M16 — :class:`OrchestraClient`, the Partner SDK entry point.

The client is a thin transport wrapper around the M4
AgenticHub HTTP shape (``/api/v1/orchestra/...``). The shape
is preferred over the legacy JSON API because it carries a
distinct URL prefix — partners can mount the AgenticHub
adapter and the legacy JSON API on the same port and
gateway them differently at the load balancer.

A partner integration typically looks like:

    client = OrchestraClient(base_url="https://api.example.com",
                            tenant_id="acme")
    try:
        status = client.submit_task(
            contract_id="ctr-001",
            contract_text="...",
            vendor_id="vendor-42",
        )
        final = client.wait_for_completion(status.task_run_id,
                                           timeout=300)
    except RateLimitError as e:
        time.sleep(e.retry_after_seconds)
        ...  # retry
    except TaskNotFoundError:
        ...  # the task vanished
    finally:
        client.close()

The :meth:`wait_for_completion` helper polls the status
endpoint with a configurable interval. Partners that need
a push-style update can use :meth:`stream_events` (TODO
in M17) or wire a webhook to the server's kill-switch
callback (TODO).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from orchestra_sdk.errors import (
    NotFoundError,
    OrchestraError,
    ProblemDetail,
    TaskNotFoundError,
    exception_for_problem,
)


# The default polling interval for :meth:`wait_for_completion`.
# 500ms is short enough to feel responsive on a 30s task,
# long enough to not flood the server.
DEFAULT_POLL_INTERVAL = 0.5

# The default per-call timeout for the underlying HTTP client.
DEFAULT_TIMEOUT = 30.0

# The states at which a task is considered "settled" — i.e.
# the caller should stop polling. ``succeeded``, ``failed``,
# and ``cancelled`` are all terminal; ``running`` is not.
TERMINAL_STATES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})


@dataclass
class TaskStatus:
    """The parsed response from POST /tasks and GET /tasks/{id}."""

    task_run_id: str
    state: str
    plan_id: str | None = None
    node_results: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskStatus":
        return cls(
            task_run_id=d.get("task_run_id", ""),
            state=d.get("state", "unknown"),
            plan_id=d.get("plan_id"),
            node_results=d.get("node_results", {}),
            error=d.get("error"),
        )


@dataclass
class TaskEvent:
    """A single audit-timeline event from GET /tasks/{id}/events."""

    event_id: str
    task_run_id: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskEvent":
        return cls(
            event_id=str(d.get("event_id", "")),
            task_run_id=d.get("task_run_id", ""),
            kind=d.get("kind", ""),
            payload=d.get("payload", {}),
            created_at=str(d.get("created_at", "")),
        )


@dataclass
class TaskReceipt:
    """A signed receipt returned by GET /tasks/{id}/receipts."""

    receipt_id: str
    task_run_id: str
    node_run_id: str
    node_id: str
    envelope: dict[str, Any] = field(default_factory=dict)
    signature: str = ""
    verified: bool | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskReceipt":
        env = d.get("envelope", {})
        if not isinstance(env, dict):
            env = {}
        return cls(
            receipt_id=str(d.get("receipt_id", "")),
            task_run_id=d.get("task_run_id", ""),
            node_run_id=d.get("node_run_id", ""),
            node_id=d.get("node_id", ""),
            envelope=env,
            signature=d.get("signature", ""),
            verified=d.get("verified"),
        )


@dataclass
class NodeGrant:
    """A Node Grant from GET /tasks/{id}/grants."""

    grant_id: str
    node_id: str
    capability_id: str
    manifest_id: str
    signature: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "NodeGrant":
        return cls(
            grant_id=str(d.get("grant_id", "")),
            node_id=d.get("node_id", ""),
            capability_id=d.get("capability_id", ""),
            manifest_id=d.get("manifest_id", ""),
            signature=d.get("signature", ""),
        )


@dataclass
class Approval:
    """A human-approval decision from GET /tasks/{id}/approvals."""

    approval_id: str
    task_run_id: str
    node_id: str
    decision: str
    decided_by: str = ""
    rationale: str = ""
    requested_at: str = ""
    decided_at: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Approval":
        return cls(
            approval_id=str(d.get("approval_id", "")),
            task_run_id=d.get("task_run_id", ""),
            node_id=d.get("node_id", ""),
            decision=d.get("decision", ""),
            decided_by=d.get("decided_by", ""),
            rationale=d.get("rationale", ""),
            requested_at=d.get("requested_at", ""),
            decided_at=d.get("decided_at", ""),
        )


class OrchestraClient:
    """The Partner SDK entry point.

    A :class:`OrchestraClient` is a thin wrapper around an
    :class:`httpx.Client`. The client holds the base URL and
    the tenant id; every method translates a business call
    into an HTTP exchange and parses the response.

    The :class:`httpx.Client` is reusable for connection
    pooling; the partner can also close it with
    :meth:`close` when the surrounding application shuts
    down. The :class:`OrchestraClient` itself can be used
    as a context manager.
    """

    def __init__(
        self,
        *,
        base_url: str,
        tenant_id: str,
        bearer_token: str | None = None,
        http_client: httpx.Client | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._tenant_id = tenant_id
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout)
        self._owns_token = False
        self._bearer_token = bearer_token

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> "OrchestraClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def set_bearer_token(self, token: str) -> None:
        """Inject an OAuth/OIDC bearer token for subsequent calls."""
        self._bearer_token = token

    # ------------------------------------------------------------------
    # Task lifecycle
    # ------------------------------------------------------------------

    def submit_task(
        self,
        *,
        contract_id: str,
        contract_text: str,
        vendor_id: str,
        budget_usd: float = 1.0,
    ) -> TaskStatus:
        """Submit a contract-review task.

        Returns the initial :class:`TaskStatus` (``state=created``);
        the partner typically chains :meth:`wait_for_completion`
        to drive the task to a terminal state.
        """
        body = {
            "contract_id": contract_id,
            "contract_text": contract_text,
            "vendor_id": vendor_id,
            "budget_usd": budget_usd,
        }
        data = self._post("/api/v1/orchestra/submit", json=body)
        return TaskStatus.from_dict(data)

    def get_task(self, task_run_id: str) -> TaskStatus:
        """Fetch the current status of a task."""
        try:
            data = self._get(f"/api/v1/orchestra/tasks/{task_run_id}")
        except NotFoundError as e:
            # Repackage as TaskNotFoundError so a partner
            # who catches that specific exception sees a
            # message about the task, not "the resource".
            raise TaskNotFoundError(f"task {task_run_id} not found", problem=e.problem) from e
        return TaskStatus.from_dict(data)

    def get_events(self, task_run_id: str) -> list[TaskEvent]:
        """List the audit-timeline events for a task."""
        data = self._get(f"/api/v1/orchestra/tasks/{task_run_id}/events")
        return [TaskEvent.from_dict(e) for e in data.get("events", [])]

    def get_receipts(self, task_run_id: str) -> list[TaskReceipt]:
        """List the signed receipts for a task (with verification status)."""
        data = self._get(f"/api/v1/orchestra/tasks/{task_run_id}/receipts")
        return [TaskReceipt.from_dict(r) for r in data.get("receipts", [])]

    def get_grants(self, task_run_id: str) -> list[NodeGrant]:
        """List the Node Grants issued for a task."""
        data = self._get(f"/api/v1/orchestra/tasks/{task_run_id}/grants")
        return [NodeGrant.from_dict(g) for g in data.get("grants", [])]

    def get_approvals(self, task_run_id: str) -> list[Approval]:
        """List the approval decisions for a task."""
        data = self._get(f"/api/v1/orchestra/tasks/{task_run_id}/approvals")
        return [Approval.from_dict(a) for a in data.get("approvals", [])]

    def approve(
        self, task_run_id: str, *, decided_by: str = "sdk", rationale: str = ""
    ) -> dict[str, Any]:
        """Approve a paused task at the human-approval node."""
        return self._post(
            f"/api/v1/orchestra/tasks/{task_run_id}/decide",
            json={"decided_by": decided_by, "rationale": rationale},
        )

    def reject(
        self, task_run_id: str, *, decided_by: str = "sdk", rationale: str = ""
    ) -> dict[str, Any]:
        """Reject a paused task. The M4 AgenticHub shape is
        one-decision-only (always an approval at the call site);
        the JSON API exposes an explicit /reject. The SDK
        surfaces both as separate methods so a partner can
        call whichever they prefer."""
        return self._post(
            f"/tasks/{task_run_id}/reject",
            json={"decided_by": decided_by, "rationale": rationale},
        )

    def wait_for_completion(
        self,
        task_run_id: str,
        *,
        timeout: float = 300.0,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> TaskStatus:
        """Poll until the task reaches a terminal state.

        Raises :class:`TimeoutError` (built-in) if the task
        doesn't settle within ``timeout`` seconds. Raises
        :class:`TaskNotFoundError` if the task id is wrong.
        Any other :class:`OrchestraError` from the underlying
        call propagates unchanged.
        """
        deadline = time.monotonic() + timeout
        while True:
            status = self.get_task(task_run_id)
            if status.is_terminal:
                return status
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"task {task_run_id} did not complete within {timeout}s (state={status.state})"
                )
            time.sleep(poll_interval)

    # ------------------------------------------------------------------
    # Capability discovery
    # ------------------------------------------------------------------

    def list_capabilities(self) -> dict[str, Any]:
        """Return the capability manifest store (what the Router sees)."""
        return self._get("/capabilities")

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        h = {
            "X-Tenant-Id": self._tenant_id,
            "Accept": "application/json",
        }
        if self._bearer_token:
            h["Authorization"] = f"Bearer {self._bearer_token}"
        return h

    def _get(self, path: str) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        r = self._http.get(url, headers=self._headers())
        return _parse(r)

    def _post(self, path: str, *, json: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        r = self._http.post(url, headers=self._headers(), json=json)
        return _parse(r)


def _parse(response: httpx.Response) -> dict[str, Any]:
    """Parse an httpx response into a dict, or raise a typed error.

    The success path returns the parsed JSON. The failure
    path parses the ProblemDetail body (RFC 7807) and raises
    the right :class:`OrchestraError` subclass.
    """
    if response.is_success:
        # The /healthz and /metrics endpoints return non-JSON
        # bodies; partners who hit those via the SDK are doing
        # something we don't support, so a clear error beats
        # a confusing JSON parse.
        try:
            return response.json()
        except ValueError as e:
            raise OrchestraError(f"server returned non-JSON body: {response.text[:200]!r}") from e
    # Failure path.
    problem: ProblemDetail | None = None
    try:
        body = response.json()
        if isinstance(body, dict) and "type" in body and "title" in body:
            problem = ProblemDetail.from_dict(body)
    except ValueError:
        pass
    cls = exception_for_problem(problem) if problem else OrchestraError
    detail = problem.detail if problem else response.text
    msg = f"{response.status_code} {detail}"
    raise cls(msg, problem=problem)
