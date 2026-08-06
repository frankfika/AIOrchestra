# ADR-0009 — Error envelope is RFC 7807 Problem Details (application/problem+json)

- Status: Accepted
- Date: 2026-08-06
- Deciders: Orchestra core team
- Relates to: M16, SPEC §0.4 M16, AGENTS.md §2

## Context

A partner who integrates with the dev path must distinguish
"validation error" from "rate-limited" from "internal error" to
show the user a useful message and back off appropriately. The
shape of the error response is a contract.

  * **Per-endpoint ad-hoc** — every handler returns its own
    dict, the partner writes a switch. Easy to write, hard
    to consume.
  * **RFC 7807 Problem Details** — every error is the same
    shape (`type` / `title` / `status` / `detail` / `instance`)
    with the `application/problem+json` media type. Standard
    for HTTP APIs.
  * **JSON:API errors** — a different standard; popular but
    not as widely adopted for non-CRUD APIs.

## Decision

The dev path uses RFC 7807 Problem Details
(`orchestra.api.errors`). Every 4xx and 5xx response carries
the same shape, with the `type` URI as a stable URN
(`urn:orchestra:problem:<slug>`) and an `orchestra` extension
field for the M9 request id and any per-handler context.
Three FastAPI exception handlers cover the standard paths
(HTTPException, RequestValidationError, the catch-all
Exception).

## Consequences

  * **+** A partner's SDK parses the shape once. The
    `orchestra_sdk.errors.exception_for_problem` map
    turns a status code into a typed exception class.
  * **+** The error envelope is documented in `OpenAPI`
    examples (M18) so a partner SDK generator picks it up
    from `/docs`.
  * **+** An operator who grep's server logs by request id
    finds the corresponding `instance` field in the body.
  * **+** The 500 catch-all never leaks the traceback; the
    partner gets a stable `urn:orchestra:problem:internal_error`
    and the operator greps the log line.
  * **−** A 4xx with a problem body is still a 4xx. The
    standard practice is to use both; we do. The trade-off
    is that a partner who only checks the status code
    misses the body (but our SDK reads it).

## Alternatives considered

  * **Per-endpoint ad-hoc** — naive. Rejected because
    a partner's SDK ends up with 30+ error classes and
    an `if/else` ladder.
  * **JSON:API errors** — fine, but the partner ecosystem
    is more familiar with Problem Details (which is what
    Stripe / GitHub / Microsoft use).
  * **GraphQL-style error envelope** — GraphQL has
    `errors[]` with a `path` field; the dev path's REST
    surface doesn't have a parallel concept. Rejected
    because the dev path is REST, not GraphQL.
