# ADR-0012 — Break-glass two-person control with bounded time

- **Status:** Accepted (M24)
- **Date:** 2026-08-08
- **Drivers:** Frank (owner), M24 P0 spec, safety invariant #23
- **Closes:** `pilot-readiness.md §2 — Invariant #23 ⚠️ Partial → ✅ Enforced`

## Context

Safety invariant #23 — "Break-glass can't lower label or bypass
Zero-Egress" — is currently enforced only at the schema and
audit-trail layer. The actual emergency override is a single
human approval, which is the same surface as the regular
`requires_approval` path.

M5–M7 hardened the regular approval path, but a real incident
response requires:

1. **Two distinct approvers** for any operation that loosens a
   policy. One approver is the requester; the second is an
   independent reviewer.
2. **Bounded lifetime.** A Break-glass grant must auto-expire.
   It cannot stay "active" past its declared window.
3. **Tenant + task + purpose + effect + resource scope binding.**
   The grant is not "open access"; it is exactly one operation.
4. **Identity from auth context, never from form input.**
   `decided_by` must come from the verified bearer token /
   OIDC claim. A partner can NEVER claim to be someone else.
5. **Lowered-by-design floor.** A Break-glass can raise a
   capability ceiling, but it cannot lower a `SecurityLabel`
   or disable Egress PEP / Zero-Egress / tenant isolation.
6. **Early revocation.** Either approver, or any operator with
   the kill-switch role, can revoke before the window closes.
7. **Audit + SIEM at every transition.** requested, approved,
   active, expired, revoked — each emits a signed event and
   forwards to the SIEM connector.

## Decision

We add a new module `orchestra/enterprise/break_glass.py` that
implements a finite state machine on top of the existing
`approvals` table. The state machine is the only legal way to
produce a Break-glass grant; the engine's regular approval path
refuses any effect that names a Break-glass resource.

### State machine

```text
                      ┌──────────────────────┐
                      │     requested        │  (applicant + ticket)
                      └──────────┬───────────┘
                                 │ first approver signs
                                 ▼
                      ┌──────────────────────┐
                      │   first-approved     │  (1 of 2 identities)
                      └──────────┬───────────┘
                                 │ second approver (≠ first, ≠ applicant) signs
                                 ▼
                      ┌──────────────────────┐
                      │      active          │  (window = min(tenant_max, requested))
                      └──────────┬───────────┘
                  ┌──────────────┴───────────────┐
                  │                              │
   window expires │              approver revokes │
                  ▼                              ▼
         ┌──────────────────┐         ┌──────────────────┐
         │     expired      │         │     revoked      │
         └──────────────────┘         └──────────────────┘
```

### Database

A new table `break_glass_requests` is added to the event-store
schema migration. It is keyed by `request_id` and has both
`approver_1` and `approver_2` columns (or a `version` column
plus a separate `break_glass_approvals` table — see ADR-0013
for the version-stamped approval model).

### Default window

15 minutes, configurable per-tenant. The schema also pins a
hard maximum (default 4 hours) that no tenant policy can
exceed — this prevents a malicious tenant policy from leaving
a Break-glass grant alive for a week.

### Identity binding

The `BreakGlassService.request()` method takes the
authenticated identity from the request context
(`tenant_id`, `user_id`, `roles`). The `approve()` method
takes the second identity from the request context. The
service rejects:

- The same identity approving twice.
- The applicant also being an approver.
- Any identity whose tenant_id does not match the request's
  tenant_id (cross-tenant is denied at the service layer, not
  just at the API layer).

### Effect ceiling

The `BreakGlassRequest.effect` field is a structured
`Effect` (the same `orchestra.core.schema.Effect` the
compiler uses). The Break-glass service refuses any effect
that:

- Has `label_override` of lower classification than the
  resource's current label.
- Has `egress_view_name` of `egress.public` when the
  resource has `DataClassification.RESTRICTED`.
- Has `disable_egress_pep=True`.

These checks are the same as the compiler's info-flow
checks, re-applied at runtime — so a misconfigured tenant
policy cannot smuggle a "downgrade" through the break-glass
path.

### Expiry sweep

A `BreakGlassExpirySweeper` (run periodically, plus on each
`/admin/breakglass` list query) marks expired rows and
revokes any associated `NodeGrant` or `Lease`. The
expiry is best-effort but always lands in the audit trail:
if the sweeper cannot reach PostgreSQL, the request stays
in `first-approved` (cannot proceed to `active` until the
DB is back) — fail-closed.

## Consequences

- The existing single-approver path in `coordinator/engine.py`
  is no longer a legal way to grant a Break-glass. Any
  template that wants Break-glass must opt in to the new
  service.
- The CLI gains a `breakglass` subcommand group: `request`,
  `approve`, `list`, `revoke`, `show`.
- The API gains `/admin/breakglass` endpoints behind the
  same admin scope that today exposes `/admin/tenants/...`
  and `/admin/webhooks/...`.
- Invariant #23 flips from ⚠️ Partial to ✅ Enforced.
- A new `BreakGlassEvent` kind is added to `EventKind` so
  the audit timeline renders break-glass rows distinctly
  from regular approvals.

## What we are NOT doing

- We are not introducing a new "super-admin" role. Break-glass
  approvers come from the existing `break_glass_approver` role
  defined in the tenant's RBAC.
- We are not implementing automated break-glass (a policy
  engine cannot request a break-glass for itself).
- We are not implementing cross-region break-glass
  replication. The PG row IS the source of truth; if the DB
  is down, break-glass is unavailable. This is the correct
  default for a security control.
