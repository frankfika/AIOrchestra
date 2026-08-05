# Walkthrough: Publishing + Multi-tenant

> **Audience:** pilot partners, IR / sales engineers, and Frank's
> investors. This walkthrough shows the canonical M5 + M6 flow
> in a single read: a tenant publishes a capability, a partner
> subscribes, the call goes through every layer (Egress PEP,
> Ingress, Release Gate), and the audit timeline is visible to
> ops.

## 1. The setup

A tenant named **ACME** wants to expose a summarisation
capability to one named partner, **Beta Insights**. ACME and
Beta have signed a contract; Beta will only receive structured
results, no free-text, and no internal labels.

The pieces Orchestra must connect:

| Layer | Module | What it does |
| --- | --- | --- |
| Multi-tenant storage | `orchestra.enterprise.isolation` | Every read and write is filtered by the active tenant. |
| Capability registry | `orchestra.registry` | The set of capabilities the tenant owns. |
| Agent Card | `orchestra.publishing.card` | Signed metadata for the published capability. |
| Published registry | `orchestra.publishing.registry` | Version pinning + revoke + lifecycle. |
| Ingress | `orchestra.publishing.ingress` | Verifies the partner's bearer token. |
| Release Gate | `orchestra.publishing.release_gate` | Validates the partner-facing result before it leaves the tenant. |
| Audit timeline | `orchestra.coordinator.event_store` | The receipt of what happened, end-to-end. |

## 2. The flow

### 2.1. ACME creates the tenant

```bash
orchestra tenant create tenant:acme --name "ACME Corp" --plan pilot
# created tenant: tenant:acme (plan=pilot)
```

Behind the scenes:

```python
ctx = TenantContext(tenant=Tenant("tenant:acme", "ACME Corp"), role=TenantRole.ADMIN)
store = IsolatingEventStore()
store.create_tenant("tenant:acme", "ACME Corp", plan="pilot")
```

The migration ran on first connect: `tenant_id` columns added to
all 6 audit tables, indexed. ACME's row is the only place
`tenant:acme` data lives.

### 2.2. ACME publishes the summarisation capability

```bash
orchestra publish create \
  --capability acme.summarize \
  --name "ACME Summarise" \
  --version 0.1.0 \
  --partner partner-beta \
  --contract contract-acme-beta-001 \
  --audiences partner-beta-api,partner \
  --data-views view:safe-summary
# published: acme.summarize v0.1.0 (status=published)
# card_id: card:ab12cd34
```

The Card is content-addressed, signed with the dev HMAC key
(production: M6 KMS), and stored in the PublishedRegistry.
M5's contract says: a new version doesn't auto-revoke the
old one — partners upgrade on their own schedule.

### 2.3. Beta fetches the Card and mints a token

```bash
curl http://acme.example/.well-known/agent.json
# Returns the Card JSON with audiences=["partner-beta-api","partner"],
# data_views=["view:safe-summary"], signature="<base64>".

# Beta mints a token via their IdP. The dev helper:
token=$(orchestra-cli ingress issue-token \
  --audience partner-beta-api \
  --scopes read:summary)
```

### 2.4. Beta calls the capability

```bash
curl -X POST https://acme.example/api/v1/orchestra/partner/summarize \
  -H "Authorization: Bearer $token" \
  -H "Content-Type: application/json" \
  -d '{"input": "the contract text"}'
```

The Ingress:

1. Looks up `acme.summarize` v0.1.0 in the PublishedRegistry.
2. Verifies the token's HMAC signature (production: OIDC
   discovery + JWKS).
3. Checks the token's `aud` is in the Card's audiences.
4. Checks the token's `scope` covers `read:summary`.
5. Looks up the FieldManifest for the view and projects the
   request through the Egress PEP. The M3 XFR-001 audit event
   records `projected_digest` — never the raw payload.

### 2.5. The Release Gate validates the response

The capability produces a structured result with a
`CitationManifest`. The Release Gate refuses:

- Free-text results.
- `error` / `stacktrace` / `internal_id` keys.
- Citations whose source is `restricted`.
- Citations whose audience is not in the Card's audience set.
- Claims with no sources (M5 default: zero unsourced claims).

If the result passes, it's released to Beta with the
`projected_digest` as the only audit fingerprint.

### 2.6. The audit timeline

```bash
orchestra audit $task_run_id
```

Ops sees, in order:

1. `task.received` — the partner call entered.
2. `plan.created` + `plan.signed` — Plan was created and signed.
3. `node.started` × N — the per-node lifecycle.
4. `grant.issued` — the Node Grant (M1).
5. `io.intent` + `io.sent` — the Egress PEP projection (M3).
   The `io.sent` row carries `projected_digest` and
   `dropped_fields`; the raw payload never appears.
6. `node.succeeded` — node completed.
7. `receipt.signed` — Receipt is signed (M2).

## 3. The fail paths

### 3.1. Kill Switch (PUB-003)

```python
ks = KillSwitch(max_effect_seconds=5.0)
ks.trip(reason="incident-42")
# Every subsequent Ingress.admit raises KillSwitchTripped
# within the bounded time window.
```

### 3.2. Revocation

```bash
orchestra publish revoke acme.summarize 0.1.0 --reason "contract ended"
# revoked: acme.summarize v0.1.0
```

Beta's next call is denied at the Ingress. The audit timeline
shows the revocation event with `revoked_at` and
`revoke_reason`.

### 3.3. Release Gate refusal

A Bug or a misconfiguration causes a result to leak
`error: "..."`. The Release Gate raises
`ReleaseDenied("forbidden key in release: 'error'")`; the
Coordinator surfaces the failure and the audit timeline
records the denial.

## 4. Why this matters

The flow proves the M5 + M6 white paper claims in one read:

- **External Subject / Service Actor / Audience / Contract**
  enter the decision (M5 acceptance bullet 2).
- Only **structured, lineaged** results reach the partner (M5
  acceptance bullet 3).
- **Agent Card, Token, version, and in-flight revocation**
  semantics are explicit (M5 acceptance bullet 4).
- **Kill Switch** has a bounded effect window, measured by the
  test suite (M5 acceptance bullet 5).
- **Multi-tenant isolation** is enforced at the storage layer
  (M6 acceptance bullet 1).
- **SBOM + signed artifacts + provenance** are produced by
  the M6 supply chain (M6 acceptance bullet 3).

For pilots: this is the runnable demo. For investors: this
is the auditable evidence.
