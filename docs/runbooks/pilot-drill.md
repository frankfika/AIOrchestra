# Pilot Safety Operations Drill (M24-OPS-001)

This drill exercises the M24 safety path end-to-end so the
on-call can confirm a green Pilot cluster before opening it
to design partners. It MUST be run on:

* every kickoff before the first design partner connects;
* every maintenance window;
* after any incident that touched the ``break_glass``,
  ``approvals``, or ``lifecycle`` tables.

The drill is non-destructive. It writes throw-away rows to
``break_glass_requests`` and ``legal_holds`` under the tenant
``tenant:drill``. The drill tenant is created lazily and can
be dropped afterwards if the operator wants a clean schema.

## What it exercises

1. **Break-glass two-person control** (ADR-0012) — a request
   is created and the active set is swept.
2. **Legal Hold creation** (ADR-0014) — a hold is created on
   a non-existent resource id (drill data, not real data).
3. **Hold-blocks-delete** — deleting the held resource MUST
   be refused with ``LifecycleBlocked``.
4. **Hold release** — the hold is released, after which the
   resource is no longer held.

The full report is JSON; the exit code is non-zero on any
step failure. The on-call is expected to gate any production
change on a green drill within the last 24 hours.

## How to run

### CLI (offline, no API required)

```bash
orchestra pilot-drill --tenant tenant:drill
```

The CLI uses synthetic in-memory stores so it does not
require a live PostgreSQL connection. The exit code is 0 on
``passed=true`` and 1 otherwise. The full JSON report is
printed to stdout; redirect to a file for the audit log:

```bash
orchestra pilot-drill --tenant tenant:drill \
    | tee /var/log/orchestra/pilot-drill-$(date -u +%Y%m%dT%H%M%SZ).json
```

### API (live cluster)

```bash
curl -sS -X POST "http://api.internal/admin/pilot-drill" \
    -H "X-Admin-Token: $ORCHESTRA_ADMIN_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"tenant_id": "tenant:drill"}' | jq .
```

(The live ``/admin/pilot-drill`` endpoint is a planned W4
follow-up; until it ships, the CLI path is authoritative.)

## Rotation drills

The drill also serves as the rotation check for two
operational secrets:

```bash
# Rotate the active KMS signing key (prints new kid).
orchestra kms rotate

# Generate a fresh webhook HMAC secret for a partner
# (the new secret is printed exactly once — copy it now).
orchestra webhook-secret rotate --partner pilot-1
```

The KMS rotation keeps the old key valid until the on-call
explicitly revokes it after the rotation window elapses.
The webhook secret rotation is fail-closed: the new
plaintext is returned once and the old plaintext is hashed
(SHA-256) for the audit log before being discarded.

## What to do on a red drill

* ``break_glass.request`` failed → DB down or the
  ``break_glass`` table is missing. Re-run
  ``orchestra doctor`` and check the DB connection.
* ``legal_hold.blocks_delete`` failed (the delete did NOT
  raise) → the LifecycleManager was wired without the
  hold gate. This is a code regression — page the on-call
  team lead immediately and revert the offending commit.
* ``legal_hold.create`` failed but the request above
  succeeded → the in-memory ``LifecycleManager`` was
  constructed without the right ``EventStore``. Check
  the W3 commit boundary.

## Audit log shape

```json
{
  "tenant_id": "tenant:drill",
  "passed": true,
  "summary": "5/5 steps ok",
  "steps": [
    {"name": "break_glass.request", "ok": true, "detail": {"request_id": "bg:..."}},
    {"name": "break_glass.sweep", "ok": true, "detail": {}},
    {"name": "legal_hold.create", "ok": true, "detail": {"hold_id": "hold:...", "case_id": "DRILL"}},
    {"name": "legal_hold.blocks_delete", "ok": true, "detail": {}},
    {"name": "legal_hold.release", "ok": true, "detail": {}}
  ]
}
```

The report is written to the audit log via the standard
``EventKind`` events (``bg.requested``, ``bg.sweep``,
``hold.created``, ``deletion.blocked``, ``hold.released``)
so a SRE can correlate the drill to the timeline.
