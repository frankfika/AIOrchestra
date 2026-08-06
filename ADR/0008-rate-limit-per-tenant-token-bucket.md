# ADR-0008 — Per-tenant rate limit is a token bucket, not a sliding window

- Status: Accepted
- Date: 2026-08-06
- Deciders: Orchestra core team
- Relates to: M14, SPEC §0.4 M14, AGENTS.md §2

## Context

A SRE needs to bound how much load a single tenant can place on the
dev path. The two credible algorithms:

  * **Sliding window** — count requests in the last N seconds; deny
    if the count exceeds the budget. Smooth; easy to reason about.
  * **Token bucket** — refill the bucket at a rate; deny if the bucket
    is empty. Allows bursts up to the bucket size; the steady-state
    rate is the long-run budget.

## Decision

The dev path uses a token bucket (`orchestra.runtime.rate_limit.TokenBucket`).
The bucket is keyed by `X-Tenant-Id` (falling back to client IP when
the header is missing) and is created lazily on first request. The
dev default is 1000 RPS / 1000 burst (permissive for local testing);
a pilot dials it down via `ORCHESTRA_RATE_LIMIT_RPS` /
`ORCHESTRA_RATE_LIMIT_BURST`.

## Consequences

  * **+** A burst-tolerant budget matches real partner traffic
    (a partner who submits a single contract at 09:00 sharp
    shouldn't be penalised for a momentary burst). A token
    bucket's `capacity` is the burst size.
  * **+** A token bucket is one float + one lock. The hot path
    is `O(1)` and the dev path's M8 perf benchmark records
    sub-microsecond per check.
  * **+** The bucket emits `orchestra_rate_limit_remaining` to
    Grafana; a SRE can graph tenant saturation in real time.
  * **−** A misconfigured `refill_rate <= 0` denies everything.
    The dev impl fails loud (returns `False` with a 1-hour
    `retry_after`) instead of silently dropping traffic;
    the SRE's dashboard will spike and they'll notice.
  * **−** A "first request, never-seen-before tenant" creates
    a fresh bucket with full capacity. A partner who can
    rotate their tenant id would get a free burst every
    time. Pilot is single-tenant, so this is a known gap;
    the M6 OIDC swap is the seam.

## Alternatives considered

  * **Sliding window** — smoother but more state per
    request (a counter, a timestamp). The M8 perf benchmark
    showed 2.5x the cost of the token bucket. Rejected.
  * **Redis-backed counter** — production-ready. The dev
    impl is in-process; the production swap uses
    `INCR` + `EXPIRE` in Redis. Same algorithm shape;
    different transport.
  * **GCRA / Tock-Tok** — a more sophisticated shaping
    algorithm. Rejected for the dev path; the production
    swap can adopt it without changing the call site
    (`RateLimiter.check`).
