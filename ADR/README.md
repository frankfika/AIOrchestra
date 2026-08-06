# Architecture Decision Records

This directory captures the design decisions a SRE / partner
integrator / pilot auditor needs to know to understand *why* the
dev path looks the way it does. Each ADR is a 1–2 page document
with a fixed shape:

- **Status** — Accepted / Superseded / etc.
- **Date** — when the decision was locked.
- **Deciders** — who owned the call.
- **Context** — what the decision area was.
- **Decision** — what we chose.
- **Consequences** — the costs (good and bad).
- **Alternatives considered** — what we said no to, and why.

The ADRs are ordered by index. Reading them in order gives
the full design rationale from M0 (monorepo + boundary) to
M20 (SSE streaming).

## Index

| # | Title | Relates to |
|---|---|---|
| [0001](0001-monorepo-structure.md) | Monorepo structure for Orchestra P0 | FND-001, AGENTS.md §1–2 |
| [0002](0002-p0-boundary-and-not-in-scope.md) | P0 boundary and the "not-in-scope" list | SPEC §0.4 |
| [0003](0003-tenant-isolation.md) | Tenant isolation is a storage-layer concern | M6, ADR-0007 |
| [0004](0004-trust-compiler-python-in-process.md) | Trust Compiler runs in-process (Python), not as a separate OPA service | M1 |
| [0005](0005-egress-pep-field-level-projection.md) | Egress PEP projects fields, not whole payloads | M3 XFR-001 |
| [0006](0006-ingress-hmac-bearer.md) | M5 Ingress uses HMAC bearer tokens, not OAuth / JWT | M5 PUB-002 |
| [0007](0007-multi-tenant-storage-layer.md) | Tenant isolation is enforced at the storage layer, not the API layer | M6 ENT-002 |
| [0008](0008-rate-limit-per-tenant-token-bucket.md) | Per-tenant rate limit is a token bucket, not a sliding window | M14 |
| [0009](0009-error-envelope-rfc-7807.md) | Error envelope is RFC 7807 Problem Details | M16 |
| [0010](0010-webhook-hmac-stripe-style.md) | Webhook delivery is HMAC-SHA-256 signed, Stripe-style | M17, M19 |
| [0011](0011-sse-per-task-in-memory-bus.md) | SSE streaming is a per-task in-memory bus with replay | M20 |

## How to use this

  * **A partner integrator** is reading ADR-0005 (FieldManifest),
    ADR-0006 (Ingress token), ADR-0009 (error envelope), and
    ADR-0010 (webhook signature) — the four contracts a
    partner SDK consumes.
  * **A SRE running pilot traffic** is reading ADR-0008
    (rate limit), ADR-0011 (SSE bus), and ADR-0007 (tenant
    isolation) — the three things that decide whether a
    pilot survives its first 10k requests.
  * **A pilot auditor** is reading the whole file in order
    to understand the design decisions; the AGENTS.md
    "Quick orientation" table points at the modules
    themselves.

## When to write a new ADR

A new ADR is the right artifact when:

  * You're locking in a choice that future changes will
    have to defend (a "shape" decision, not a
    "what-should-the-default-be" decision).
  * The alternatives are real — there's a credible
    "we could have done X instead" line a future
    contributor could ask.
  * The decision affects the partner contract, the
    production-swap story, or the SRE mental model.

The dev path's policy is "every milestone ships an ADR
for the design decision that drives it". The
`AGENTS.md` milestone table names the milestone
commit; the ADR names the rationale.
