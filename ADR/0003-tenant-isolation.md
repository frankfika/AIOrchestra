# ADR-0003: Multi-tenant isolation via per-row tenant_id + tenant-scoped storage

- **Status**: Accepted (M6)
- **Date**: 2026-08-06
- **Deciders**: Frank + the M6 multi-tenant engineer

## Context

The M0–M5 EventStore is a single-tenant design. Every audit
event, task run, node run, receipt, grant, and approval lives
in a single shared table; the row is not tagged with the
tenant that owns it. M6 is the first milestone that requires
multi-tenant production (ENT-001 in the dev plan).

Three options were considered:

1. **Schema-per-tenant**: each tenant gets its own tables
   (`task_runs_<tenant>`, `events_<tenant>`, …). Strong
   isolation, easy to drop a tenant by dropping their schema.
   Migrations are per-tenant and additive.
2. **Database-per-tenant**: each tenant gets its own database.
   Strongest isolation; the closest thing to a real production
   multi-tenant shape. Migrations are per-database; running
   `tenant:acme` and `tenant:beta` upgrades simultaneously is
   a coordination problem.
3. **Row-level security (RLS) via `tenant_id` column**: every
   table has a `tenant_id` column; the storage layer's WHERE
   clause always filters by the active tenant. Indexes are
   per-(tenant_id, …). Cross-tenant reads return nothing;
   cross-tenant writes raise.

The white paper / dev plan §M6 says "row-level security"
explicitly, so option 3 is the canonical answer. This ADR
records the specific implementation choice: row-level
`tenant_id` with a tenant-context propagation via Python's
`contextvars`.

## Decision

The M6 implementation is **option 3 with a soft migration**:

1. **Every audit table gets a `tenant_id` column**. The
   migration is idempotent: it runs on every API boot. New
   installations start with the column; old installations are
   back-filled with `'tenant:demo'`.
2. **The column stays `NULL`-able** so the M0–M5
   `EventStore` (which does not know about `tenant_id`) can
   keep writing. The M6 `IsolatingEventStore` always sets
   `tenant_id` explicitly; its WHERE clause filters out
   `NULL` rows so M6 callers see only their own data.
3. **The active tenant is propagated through a
   `contextvars.ContextVar`**. The M6 routes set the tenant
   from a header (production: OIDC claim); tests set it
   directly. The `IsolatingEventStore` reads the active
   tenant; there is no public API to query another tenant's
   data.
4. **The M6 `WHERE` clause is built from the active tenant
   server-side**, not from a query parameter. A caller cannot
   forge a different tenant id; the tenant id is
   authoritative.
5. **The M6 schema is the only schema M7+ uses**. The M0
   `EventStore` is deprecated and slated for removal in M7
   once every caller is migrated (the M7 "all callers
   upgraded" gate).

## Consequences

### Positive

- **Strongest possible isolation**: the WHERE clause
  literally cannot be forged from the client.
- **Backwards-compatible migration**: M0–M5 tests continue
  to pass without modification; the `tenant:demo`
  back-fill is automatic.
- **Indexes are per-tenant**: a tenant with a million events
  does not slow down a tenant with ten events.
- **No application-level filter bugs**: the storage layer is
  the only place that knows about `tenant_id`; forgetting a
  filter at the API layer does not leak data.

### Negative

- **`NULL` rows are writeable from the M0 path** until M7's
  "all callers upgraded" gate. The M0 `EventStore` is
  tenant-agnostic by design; this is migration debt, not a
  design flaw.
- **A truly catastrophic bug in the storage layer** (e.g.
  dropping the `tenant_id` predicate from a query) would
  leak across tenants. The test suite
  (`tests/m6/test_isolation.py`) catches regressions of this
  shape; the production swap will add a SQL-level RLS policy
  as a defense-in-depth measure.

### Neutral

- **Cross-tenant enumeration** is admin-only (`list_tenants_for_admin`
  requires `role == ADMIN`). Auditor and operator roles
  cannot see other tenants; the test suite enforces this.

## Alternatives revisited

- **Schema-per-tenant**: rejected for M6 because the M0–M5
  test suite does not need to change. Picked up in M6
  defense-in-depth: the M7 production swap will shard by
  tenant (e.g. separate PG schemas) **on top of** the
  row-level filter, not as a replacement.
- **Database-per-tenant**: rejected for M6 because the
  per-DB migrations are operationally heavy. Picked up
  in M6 / M7 defense-in-depth for the largest customers.

## References

- White paper §M6 (ENT-001): row-level security + cross-tenant
  attack tests + resource-leak detection + quota.
- Dev plan §M6 / 26-invariants #13 (external input
  untrusted + tenant context isolation) and #18 (external
  cache key includes full security context).
- 26-invariants.md.
