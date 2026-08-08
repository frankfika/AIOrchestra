# ADR-0014 — Retention and Legal Hold lifecycle (deletion is idempotent, holds are absolute)

- **Status:** Accepted (M24)
- **Date:** 2026-08-08
- **Drivers:** Frank (owner), M24 P0 spec, safety invariant #26
- **Closes:** `pilot-readiness.md §2 — Invariant #26 ⚠️ Partial → ✅ Enforced`

## Context

Safety invariant #26 — "Delete / retain / Legal Hold covers
all copies" — is currently enforced only at the artifact store
layer. A delete operation on `ArtifactStore` removes the row
and the blob reference, but:

1. There is no global lifecycle policy. Each module decides
   its own retention independently.
2. There is no Legal Hold primitive. If a regulator says
   "preserve everything for case X", an operator must
   remember to disable the relevant cleanup jobs. There is
   no system-level gate.
3. The Receipt, Event, and Webhook payload stores do not
   participate in retention. An artifact can be deleted but
   its audit row remains forever (or vice versa).
4. Deletion is not idempotent. A partial failure leaves the
   system in a state where retry is dangerous (re-running
   the delete may double-trigger side effects, or worse, hit
   "already gone" errors and falsely report success).
5. There is no deletion evidence. The audit trail says
   "deleted" but does not record *what* was deleted, *by
   whom*, or *which copies remained*.
6. Cross-tenant operations are not uniformly denied. A
   bug in the artifact store could allow a tenant admin to
   read another tenant's deletion log.

M24 P0 requires a unified lifecycle model that covers every
copy of every artefact, with Legal Hold as an absolute gate
and deletion as an idempotent lifecycle task.

## Decision

We add a new module `orchestra/enterprise/lifecycle.py` that
implements a `LifecycleManager` with three policy primitives:

```text
LifecyclePolicy       — what to keep, for how long, under which trigger
LegalHold             — an absolute, tenant-scoped freeze on deletion
DeletionJob           — an idempotent, retryable unit of deletion work
```

The `LifecycleManager` is the *only* legal way to delete an
artifact, receipt, event payload, or webhook delivery. Direct
`DELETE` statements on these tables are blocked by the
manager's wrapper (or, where direct SQL is unavoidable, the
manager's `assert_no_active_hold()` check is the gate).

### Schema

```sql
CREATE TABLE lifecycle_policies (
    policy_id         TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    resource_kind     TEXT NOT NULL,    -- artifact|receipt|event|webhook|cache|backup
    retention_seconds BIGINT NOT NULL,
    auto_delete       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, resource_kind)
);

CREATE TABLE legal_holds (
    hold_id        TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    case_id        TEXT NOT NULL,
    reason         TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by     TEXT NOT NULL,
    released_at    TIMESTAMPTZ,
    released_by    TEXT,
    release_reason TEXT,
    UNIQUE (tenant_id, case_id)
);

CREATE TABLE legal_hold_resources (
    hold_id        TEXT NOT NULL REFERENCES legal_holds(hold_id) ON DELETE CASCADE,
    resource_kind  TEXT NOT NULL,
    resource_id    TEXT NOT NULL,
    PRIMARY KEY (hold_id, resource_kind, resource_id)
);

CREATE TABLE deletion_jobs (
    job_id         TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    resource_kind  TEXT NOT NULL,
    resource_id    TEXT NOT NULL,
    state          TEXT NOT NULL,    -- pending|running|deleted|partial|failed
    attempt        INT NOT NULL DEFAULT 0,
    max_attempts   INT NOT NULL DEFAULT 3,
    requested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    requested_by   TEXT NOT NULL,
    completed_at   TIMESTAMPTZ,
    last_error     TEXT,
    deletion_evidence JSONB,         -- {deletion_id, deleted_copies, kept_copies, digest}
    UNIQUE (tenant_id, resource_kind, resource_id)
);
```

### Legal Hold semantics

- A `LegalHold` is created by an authenticated user with the
  `legal_hold_creator` role, and the creation emits a
  `hold.created` event to the audit trail and to SIEM.
- Every `DELETE` (manual or automatic) checks
  `legal_hold_resources` for an active hold matching the
  resource. The check is the *first* statement in the
  deletion transaction — if the hold exists, the transaction
  rolls back with `LifecycleBlocked` and the deletion job
  transitions to `state='held'` (not `failed`).
- A `LegalHold` is released by the `legal_hold_releaser` role
  (optionally with two-person control via ADR-0012's
  Break-glass — the design partner will pick). The release
  emits a `hold.released` event.
- Cross-tenant operations on `legal_holds` are denied at
  the row level (the `tenant_id` filter is in every query).

### Deletion job semantics

- A `DeletionJob` is created by `LifecycleManager.delete(...)`.
  The `UNIQUE (tenant_id, resource_kind, resource_id)` index
  makes the call idempotent — re-running `delete()` on the
  same resource returns the existing job instead of creating
  a second one.
- A worker (the `LifecycleSweeper`, run periodically or on
  demand) advances the job from `pending` to `running`,
  calls the resource-specific delete adapter, and updates
  the state:
  - `deleted`: every known copy is gone.
  - `partial`: some copies are gone, some failed. The
    `deletion_evidence` JSON records exactly which kept
    copies remain (so the next retry can re-attempt them).
  - `failed`: a non-retryable error. The job stays in the
    store so an operator can intervene.
- A retry is allowed up to `max_attempts`. After the limit,
  the job transitions to `failed` and an alert is emitted.
- The deletion *never* silently succeeds. A successful
  `DELETE` from the database is recorded as
  `deletion_evidence.copies_deleted=N+1` plus the digest
  of the deleted payload (for the audit trail, not for
  reconstruction).

### Coverage

The `LifecycleManager.delete` API covers:

- `Artifact` (the artifact store + the blob reference)
- `Receipt` (the receipts table + the event-store row that
  references it)
- `Event` (the events.payload JSONB column — the row stays
  but the payload is replaced with `{"redacted": true}`)
- `Webhook delivery` (the deliveries table)
- Cache entries (delegated to a future module — M24 ships
  the manager; the cache adapter is a follow-up if a real
  cache is in use)
- Backups (out of scope for the dev path — but the policy
  framework applies once backups are wired in)

### Cross-tenant safety

Every read/write to the lifecycle tables is scoped to a
`tenant_id` derived from the request context. A query that
omits the `tenant_id` filter is rejected by a code-review
check (the test suite includes a `test_lifecycle.py` that
asserts every public method accepts and forwards a
`tenant_id` argument).

## Consequences

- The existing `ArtifactStore.delete` is deprecated in favor
  of `LifecycleManager.delete`. The old method is kept as a
  thin wrapper that delegates to the manager.
- The CLI gains a `retention` subcommand group: `policy set`,
  `policy show`, `hold create`, `hold list`, `hold release`,
  `deletion show`, `deletion retry`.
- The API gains `/admin/holds`, `/admin/retention/policy`,
  and `/admin/deletion-jobs` endpoints.
- A new `LegalHoldEvent` and `DeletionJobEvent` kind is
  added to `EventKind`.
- Invariant #26 flips from ⚠️ Partial to ✅ Enforced.

## What we are NOT doing

- We are not implementing automated backup deletion. The
  policy framework supports it; the actual backup integration
  is a follow-up once the partner chooses a backup target.
- We are not implementing a "soft delete" window. A
  `DeletionJob` is a hard-delete job; a separate `Quarantine`
  primitive is a possible M25+ if the design partner
  requests it.
- We are not implementing free-text scrubbing of redacted
  payloads. The `{"redacted": true}` marker is the only
  payload after a deletion; no partial scrubbing.
- We are not implementing Legal Hold inheritance across
  derived artefacts (e.g. a citation of a receipt of a
  deleted artifact). The hold covers the explicitly listed
  resources; a future M25+ can add transitive coverage.
