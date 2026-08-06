"""M16 — Standard error response envelope (RFC 7807 Problem Details).

Every 4xx and 5xx response from the dev path carries the same
JSON shape. The shape is the IETF standard
``application/problem+json`` (RFC 7807) with one extension
field for Orchestra's own context.

Why a standard envelope:
  * The Python Partner SDK (orchestra_sdk) parses the shape
    once and turns every error into a typed exception.
    Partners that integrate with curl + jq also benefit:
    ``jq .detail`` works the same on every error.
  * The HTTP status code stays the source of truth (a
    429 with a problem body is still a 429), so existing
    load balancers and rate-limit-aware SDKs keep working.
  * SRE runbooks (docs/runbooks/) can document a single
    error schema instead of a different shape per endpoint.

The ``type`` URI is a stable, versioned reference a
partner's automated runbook can match against. The dev
path uses a URN-like ``urn:orchestra:problem:<slug>``
because that's resolvable from any namespace without
needing a public DNS entry.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# The standard media type for RFC 7807 problem responses.
PROBLEM_JSON = "application/problem+json"

# The dev-path problem-type catalog. The slug is the
# last component of the ``type`` URI; the title is the
# short human-readable label.
#
# Adding a new entry here is the SRE's way of saying
# "this is a new error class partners need to handle".
# The slug becomes the Python exception class name in
# the SDK (e.g. ``RateLimitError`` from ``rate_limited``).
PROBLEM_TYPES: dict[str, str] = {
    "validation_error": "Request body or query failed validation.",
    "task_not_found": "No task with the given task_run_id.",
    "rate_limited": "Token bucket exhausted; retry after the Retry-After interval.",
    "payload_too_large": "Request body exceeded the size cap.",
    "method_not_allowed": "HTTP method not allowed for this path.",
    "internal_error": "Unhandled server-side failure; check logs by request id.",
    "unauthorized": "Missing or invalid credentials.",
    "forbidden": "Caller is authenticated but not allowed.",
    "not_found": "Resource not found.",
    "conflict": "Resource state conflicts with the request.",
    "dependency_failure": "An upstream dependency (DB, registry) is unavailable.",
}


def problem_type_uri(slug: str) -> str:
    """Return the canonical ``type`` URI for a problem slug.

    The dev path uses ``urn:orchestra:problem:<slug>`` so the
    URI is unique without a DNS dependency. A partner's
    runbook matches against the slug, not the full URI.
    """
    return f"urn:orchestra:problem:{slug}"


@dataclass
class ProblemDetail:
    """The RFC 7807 problem details body.

    Required fields (``type``, ``title``, ``status``) carry
    the IETF-mandated minimum. ``detail`` is the human
    explanation; ``instance`` is the URI for this specific
    occurrence (the M9 request id is a fine value).

    The ``orchestra`` extension field carries Orchestra-
    specific context: the tenant, the request id, and any
    extra fields a handler wants to surface. Partners can
    read the standard fields and ignore the extension.
    """

    type: str
    title: str
    status: int
    detail: str = ""
    instance: str = ""
    orchestra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Render as the wire body. The ``type`` URI is
        resolved through :func:`problem_type_uri` if the
        caller passed a slug."""
        d = asdict(self)
        # If ``type`` is a slug (no ``:`` separator), resolve
        # it to the canonical URN. An already-resolved URN
        # is left alone — calling :func:`problem_type_uri`
        # twice on the same slug would double-wrap.
        if ":" not in d["type"]:
            d["type"] = problem_type_uri(d["type"])
        # The dataclass wraps ``orchestra`` in a dict; if
        # it's empty we drop it from the wire body to keep
        # the response small.
        if not d.get("orchestra"):
            d.pop("orchestra", None)
        return d


def problem_from_http_exception(
    exc: Exception,
    *,
    status: int,
    request_id: str = "",
    extra: dict[str, Any] | None = None,
) -> ProblemDetail:
    """Build a :class:`ProblemDetail` from a FastAPI ``HTTPException``
    or any ``Exception`` with a ``.status_code`` attribute.

    The mapping is conservative: the dev path maps the HTTP
    status to the closest catalog slug. A SRE who wants a
    richer mapping (e.g. distinguishing ``validation_error``
    from ``conflict`` at the source) extends the call site.
    """
    status_to_slug = {
        400: "validation_error",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        413: "payload_too_large",
        422: "validation_error",
        429: "rate_limited",
        500: "internal_error",
        502: "dependency_failure",
        503: "dependency_failure",
        504: "dependency_failure",
    }
    slug = status_to_slug.get(status, "internal_error")
    detail = ""
    if hasattr(exc, "detail"):
        # FastAPI's HTTPException carries a string detail; some
        # custom exceptions carry a structured one.
        raw = exc.detail
        detail = raw if isinstance(raw, str) else str(raw)
    orchestra: dict[str, Any] = {}
    if request_id:
        orchestra["request_id"] = request_id
    if extra:
        orchestra.update(extra)
    return ProblemDetail(
        type=problem_type_uri(slug),
        title=PROBLEM_TYPES.get(slug, "Error"),
        status=status,
        detail=detail or PROBLEM_TYPES.get(slug, ""),
        instance=request_id,
        orchestra=orchestra,
    )
