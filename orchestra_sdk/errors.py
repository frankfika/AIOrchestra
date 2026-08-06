"""M16 — SDK error types.

The server returns RFC 7807 ``application/problem+json`` on
every 4xx and 5xx (see :mod:`orchestra.api.errors`). The SDK
parses that envelope and raises a typed exception so a
partner's ``except`` blocks read like business logic, not
HTTP plumbing.

Mapping (status code → exception class):

  * 400 / 422            → :class:`ValidationError`
  * 404                  → :class:`TaskNotFoundError` (when
                            the URL mentioned ``/tasks/{id}``)
                            or :class:`NotFoundError` (any
                            other resource)
  * 413                  → :class:`PayloadTooLargeError`
  * 429                  → :class:`RateLimitError`
  * 500                  → :class:`InternalError`
  * 502 / 503 / 504      → :class:`DependencyFailureError`
  * anything else        → :class:`OrchestraError` (catch-all)

Every exception carries the parsed :class:`ProblemDetail` so
a partner can read the ``request_id`` and grep the server
logs directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ProblemDetail:
    """A parsed RFC 7807 problem body.

    Carries the standard fields (``type``, ``title``, ``status``,
    ``detail``, ``instance``) and the ``orchestra`` extension
    (request id, error list, etc.). A partner can read
    ``problem.instance`` to get the request id, or
    ``problem.orchestra.get("request_id")`` — both work.
    """

    type: str
    title: str
    status: int
    detail: str = ""
    instance: str = ""
    orchestra: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ProblemDetail":
        return cls(
            type=d.get("type", ""),
            title=d.get("title", ""),
            status=int(d.get("status", 0)),
            detail=d.get("detail", ""),
            instance=d.get("instance", ""),
            orchestra=d.get("orchestra") or {},
        )

    def request_id(self) -> str:
        """The M9 request id, used to correlate with server logs."""
        if self.instance:
            return self.instance
        if self.orchestra:
            return str(self.orchestra.get("request_id", ""))
        return ""


class OrchestraError(Exception):
    """Base class for every Orchestra-specific error.

    A partner who only wants "any error from the Orchestra
    API" can ``except OrchestraError`` and skip the
    subclassing.
    """

    def __init__(self, message: str, *, problem: ProblemDetail | None = None) -> None:
        super().__init__(message)
        self.problem = problem

    @property
    def status(self) -> int:
        return self.problem.status if self.problem else 0

    @property
    def type_uri(self) -> str:
        return self.problem.type if self.problem else ""

    @property
    def request_id(self) -> str:
        return self.problem.request_id() if self.problem else ""


class ValidationError(OrchestraError):
    """The request body or query failed validation (400 / 422)."""


class TaskNotFoundError(OrchestraError):
    """The /tasks/{task_run_id} does not exist (404)."""


class NotFoundError(OrchestraError):
    """A non-task resource was not found (404)."""


class PayloadTooLargeError(OrchestraError):
    """The request body exceeded the size cap (413)."""


class RateLimitError(OrchestraError):
    """The token bucket is empty; retry after ``Retry-After`` seconds (429)."""

    @property
    def retry_after_seconds(self) -> int:
        # The default 0 is what callers fall back to when
        # the server omitted the header; in practice the
        # M14 middleware always sets it.
        if self.problem is None:
            return 0
        orchestra = self.problem.orchestra or {}
        try:
            return int(orchestra.get("retry_after_seconds", 0))
        except (TypeError, ValueError):
            return 0


class InternalError(OrchestraError):
    """Unhandled server-side failure (500)."""


class DependencyFailureError(OrchestraError):
    """An upstream dependency (DB, registry) is unavailable (502/503/504)."""


_STATUS_TO_EXCEPTION: dict[int, type[OrchestraError]] = {
    400: ValidationError,
    401: ValidationError,
    403: ValidationError,
    404: NotFoundError,  # callers may swap to TaskNotFoundError when relevant
    405: ValidationError,
    409: ValidationError,
    413: PayloadTooLargeError,
    422: ValidationError,
    429: RateLimitError,
    500: InternalError,
    502: DependencyFailureError,
    503: DependencyFailureError,
    504: DependencyFailureError,
}


def exception_for_problem(problem: ProblemDetail) -> type[OrchestraError]:
    """Pick the right exception class for a parsed problem body."""
    return _STATUS_TO_EXCEPTION.get(problem.status, OrchestraError)
