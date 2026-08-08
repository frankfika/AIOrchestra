# ADR-0013 — Persistent approval workflow (PostgreSQL is the source of truth)

- **Status:** Accepted (M24)
- **Date:** 2026-08-08
- **Drivers:** Frank (owner), M24 P0 spec, safety invariant #7 + #23
- **Closes:** `pilot-readiness.md §4.4 — Open follow-ups #1`

## Context

The `Coordinator` engine today resolves human approvals via an
in-memory `asyncio.Event`:

```python
self._approval_events: dict[tuple[str, str], tuple[asyncio.Event, dict[str, Any]]] = {}
```

This works for the demo and for tests, but it has three
production-blocking properties:

1. **Crash-loss.** A process restart between the engine pausing
   for approval and a human clicking "approve" silently drops
   the pending approval. The task is stuck forever; the audit
   timeline says "waiting for human" with no resurrection path.
2. **Single-instance only.** A second instance of the API
   never sees the approval the first instance is waiting on.
   The horizontal-scaling story is broken.
3. **No atomic state transition.** Two operators hitting
   "approve" at the same instant can both win; the engine
   records two approvals for the same gate. This is benign
   today because the second arrival is a no-op, but it
   prevents a future design from requiring "exactly one
   approval" (e.g. for legal hold lift).

M24 P0 requires:

- PostgreSQL as the authoritative store for approval state.
- Service restart re-loads pending approvals from PG and
  re-attaches them to the live event loop.
- Two instances approving the same gate concurrently
  produce exactly one state transition (atomic compare-and-set
  on a `version` column).
- Regular single-approver business approvals and Break-glass
  two-approver approvals share the same framework but enforce
  different policies.

## Decision

We make `approvals` a first-class state machine in the
EventStore, with a `version` column for atomic
compare-and-set. The engine keeps an in-process cache for
fast wake-up, but it always reads the authoritative state
from PG before acting.

### Schema change

```sql
CREATE TABLE approvals (
    approval_id    TEXT PRIMARY KEY,
    task_run_id    TEXT NOT NULL,
    node_id        TEXT NOT NULL,
    tenant_id      TEXT NOT NULL,         -- M24: cross-tenant deny
    version        BIGINT NOT NULL DEFAULT 0,  -- M24: atomic CAS
    state          TEXT NOT NULL,         -- pending|approved|rejected|expired
    required_approvers INT NOT NULL DEFAULT 1, -- M24: 1 for business, 2 for break-glass
    requested_at   TIMESTAMPTZ NOT NULL,
    requested_by   TEXT NOT NULL,         -- M24: identity from auth context
    ticket         TEXT,                  -- M24: ticket / case id for break-glass
    decided_at     TIMESTAMPTZ,
    decision_payload JSONB,
    -- M24: append-only approver log so two-person control has a real audit trail
    UNIQUE (task_run_id, node_id)
);

CREATE TABLE approval_decisions (
    decision_id    TEXT PRIMARY KEY,
    approval_id    TEXT NOT NULL REFERENCES approvals(approval_id) ON DELETE CASCADE,
    decision_seq   INT NOT NULL,          -- 1, 2, ... (per approver)
    decided_by     TEXT NOT NULL,
    decided_at     TIMESTAMPTZ NOT NULL,
    decision       TEXT NOT NULL,         -- approve|reject
    rationale      TEXT,
    identity_tenant TEXT NOT NULL,        -- M24: must match approval.tenant_id
    UNIQUE (approval_id, decision_seq)
);
```

### Atomic compare-and-set

```python
def record_decision(self, approval_id, decision, decided_by,
                    identity_tenant) -> DecisionResult:
    """Atomically append a decision and update the approval state.
    Returns the new state, or None if the approval is already terminal
    (concurrent loser).
    """
    ...
```

The SQL:

```sql
UPDATE approvals
SET state = CASE
        WHEN required_approvers = 1 AND $decision = 'approve' THEN 'approved'
        WHEN required_approvers = 2 AND $decision = 'approve'
             AND (SELECT count(*) FROM approval_decisions
                  WHERE approval_id = $approval_id) = 1
             THEN 'approved'
        WHEN $decision = 'reject' THEN 'rejected'
        ELSE state
    END,
    decided_at = now(),
    decision_payload = $payload,
    version = version + 1
WHERE approval_id = $approval_id
  AND state = 'pending'
  AND tenant_id = $identity_tenant
RETURNING state, version;
```

A `RETURNING` with zero rows means either (a) the approval is
already terminal, (b) the identity is cross-tenant, or (c) the
second approver tried to be the first. The caller distinguishes
the three by looking at the row separately.

### Engine integration

The engine's `_approval_events` becomes a *cache* not the
source of truth:

1. When a node needs approval, the engine calls
   `store.create_approval(...)` (PG) and stores the returned
   `approval_id`.
2. The engine waits on the in-process `asyncio.Event` (fast
   path for tests / single-instance).
3. When `decide_approval(...)` is called from the API:
   - Write the decision to PG via `record_decision(...)`.
   - If PG says the approval is now terminal, set the
     `asyncio.Event`.
   - If PG says the approval is still pending (second
     approver for break-glass not yet arrived), the engine
     keeps waiting.
4. On engine startup, `_reload_pending_approvals()` re-creates
   the in-process events for any `state='pending'` rows.

### Concurrency guarantee

Two instances approving the same `approval_id` race in PG. The
`UPDATE ... WHERE state='pending' RETURNING` guarantees at most
one writer transitions the row. The loser gets zero rows back
and the API returns 409 Conflict — the second approver is told
"already decided".

### Restart guarantee

The engine's `_reload_pending_approvals` runs on every
`Coordinator.__init__`. It re-issues the asyncio.Event for
each `state='pending'` row so the engine can re-await the
decision. The audit timeline is preserved (the row is
already in `events`).

## Consequences

- The engine's existing `approval_handler` interface is
  preserved. Tests that inject custom handlers continue to
  work, but the default handler now consults PG.
- The `coordinator.engine` file grows by ~150 lines (the
  reload + persistence hooks). The interface change is
  internal.
- A new `tests/m24/test_persistent_approval.py` exercises
  the restart and concurrency paths.
- The CLI `orchestra approve <task_id>` works against a
  server that has been restarted between the submission
  and the human decision.

## What we are NOT doing

- We are not implementing long-poll. The engine still uses
  `asyncio.Event` for wake-up. A future M25+ can layer SSE
  on top of the `approvals` table.
- We are not replacing `asyncio.Event` with PG `LISTEN/NOTIFY`
  for cross-instance wake-up. That would change the
  architecture too much for a single milestone. The
  compare-and-set is the production guarantee; the wake-up
  is a latency optimization.
- We are not changing the existing `requires_approval`
  template semantics. The two-person rule is opt-in.
