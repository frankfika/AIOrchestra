# ADR-0010 — Webhook delivery is HMAC-SHA-256 signed, Stripe-style

- Status: Accepted
- Date: 2026-08-06
- Deciders: Orchestra core team
- Relates to: M17, M19, AGENTS.md §2

## Context

A partner who asks for webhook delivery (M17) needs a way to
verify the body they receive is the body the server sent. Three
options:

  * **Mutual TLS** — strongest, but requires partner-side PKI
    the pilot doesn't have.
  * **JWT in a header** — over-engineered for a
    server-to-partner push.
  * **HMAC-SHA-256 of the body, in a `sha256=<hex>` header** —
    the convention Stripe / GitHub / Slack use. A partner
    who's integrated with any of them already has a verifier.

## Decision

The dev path signs every webhook body with HMAC-SHA-256
(`orchestra.webhooks.dispatcher.sign_body`). The signature is
in the `X-Orchestra-Signature` header as `sha256=<hex>`. The
body is the raw POST bytes (not the JSON-stringified version);
the JSON is serialized with `sort_keys=True` so the signature
is stable against server-side key reordering. Two more
headers round out the contract: `X-Orchestra-Delivery-Id`
(idempotency key for the partner's dedup logic) and
`X-Orchestra-Event-Type` (`task.succeeded` /
`task.failed` / `task.cancelled`).

## Consequences

  * **+** A partner's existing webhook verifier code
    transfers with one variable rename.
  * **+** The body is signed, not just the headers. A
    middleman who modifies the body breaks the signature.
  * **+** The signature is deterministic — a partner can
    re-verify offline without re-fetching the body.
  * **+** The M19 manual-retry path reuses the same
    `X-Orchestra-Signature` scheme with a fresh
    `delivery_id`, so the partner's verifier doesn't
    need to change.
  * **−** The dev path retries on 5xx but not on 4xx
    (a 4xx is a partner bug, retrying just amplifies
    it). The partner is expected to fix their endpoint
    and use the M19 retry endpoint to re-fire.
  * **−** The retry budget is 3 with exponential
    backoff. A flaky partner who hits 3 failures
    needs the M19 endpoint to re-fire. Production
    swap to a real queue (Redis / SQS) lifts this.

## Alternatives considered

  * **Mutual TLS** — production-strong; partner-side PKI
    cost. Rejected for the dev path; the M6 OIDC swap
    can layer mTLS on top of the bearer token if a
    pilot demands it.
  * **JWT in a header** — over-engineered. The body is
    small and signed; the partner doesn't need a full
    JWT to verify.
  * **No signature** — naive. A middleman can
    impersonate the server. Rejected.
