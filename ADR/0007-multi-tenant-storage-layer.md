# ADR-0007 — Tenant isolation is enforced at the storage layer, not the API layer

- Status: Accepted
- Date: 2026-08-06
- Deciders: Orchestra core team
- Relates to: M6 ENT-002, ADR-0003, AGENTS.md §2

## Context

A multi-tenant store must answer "is this row visible to this tenant?"
The enforcement can live in:

  * **The API layer** — every handler adds a `WHERE tenant_id = :tid`
    filter before reading. Fast to implement, easy to forget, hard
    to audit. A single missed handler leaks the whole table.
  * **The storage layer** — the store's API takes a `TenantContext`
    and adds the filter itself, regardless of the caller. The
    storage is the only thing that talks to the database.

## Decision

The dev path enforces tenant isolation in the storage layer
(`orchestra.enterprise.isolation.IsolatingEventStore`). Every read and
write goes through a method that takes the active `TenantContext` and
adds the `WHERE tenant_id = ...` clause in the SQL itself, not in the
caller. The API layer's only job is to wire the `TenantContext` from
the request headers (or the test setup).

## Consequences

  * **+** A single audit surface. The store has exactly one place
    that talks to the DB; if isolation is wrong, that one place is
    wrong, and a SRE knows where to look.
  * **+** The cross-tenant access path is unreachable from
    application code. A handler that forgets to filter can't
    leak — the storage adds the filter itself.
  * **+** The dev path's test matrix is small: one test per
    storage method (read / write / cross-tenant denial). The
    API layer's test matrix doesn't have to re-prove isolation.
  * **−** The storage layer is more complex than a thin SQL
    wrapper. The `IsolatingEventStore` class is ~250 lines.
  * **−** A handler that wants to do an admin cross-tenant read
    must go through a separate admin path (the M8 admin endpoints
    use a `TenantRole.ADMIN` context). The dev path doesn't
    paper over the access boundary.

## Alternatives considered

  * **Row-level security in PostgreSQL** — the dev path uses `WHERE
    tenant_id = current_setting('app.tenant_id')` and the
    Coordinator sets the GUC per request. Production-strong;
    the dev path's EventStore uses `current_setting(...)` so a
    production swap can opt in.
  * **Schema-per-tenant** — heaviest isolation, hardest to
    operate. Rejected for the dev path; the M6 production
    swap is free to use it.
  * **API-layer filtering** — the naive choice. Rejected because
    it's the single biggest source of cross-tenant data
    leaks in real-world SaaS.
