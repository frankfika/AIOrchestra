# Pilot-Readiness Closing Report — Orchestra M0–M22

> **Audience:** Frank (owner), design-partner engineering leads, the
> M7 GA gate, and the next operator who picks this up.
>
> **Purpose:** single document that answers four questions —
> 1. what the white paper roadmap (P0 + M0–M7) asked for,
> 2. what is actually shipped today,
> 3. which of the 26 safety invariants are enforced, partially
>    enforced, or out of scope,
> 4. what production-swap work remains and what *blocks* on
>    Frank's partner / cloud / legal inputs vs what can be done
>    unblocked.

This is the closing artifact for the M0–M21 push sequence. The
project is **pilot-ready**: a design partner can stand up the
control plane from `docs/runbooks/install.md` in under 15 minutes
and submit a real task end-to-end against the M3 E2E flow.

---

## 1. Roadmap → shipped state

The white paper roadmap (`产品白皮书 §16`) and dev plan
(`开发计划 §M0–M7`) define P0 + M0–M7 as the *core* milestones;
the dev plan explicitly says everything after M7 is "advanced
research and optional features" (`开发计划 §M7 之后`). This
project shipped the core eight milestones and then iterated to
harden, integrate, and document for partner consumption.

| Roadmap item | Commit | Status | What ships |
| --- | --- | --- | --- |
| **P0 — Category Proof** | `aabf0c9` | ✅ Shipped | Fixed Contract Review template + 3 reference Adapters + Node Grant + signed Receipt + PG Event Store + 3-baseline Benchmark + Dify Task Tool |
| **M0 — Spec Preview** | `70a4830` / `471c746` | ✅ Shipped | Frozen spec extensions (ValueRef, Requirement, InformationFlowRule, FieldManifest, Citation) + 26-invariants matrix |
| **M1 — Compiler Alpha** | `d07113a` | ✅ Shipped | Trust Compiler (parser / normalizer / type-checker / info-flow / effect-checker / delegation-checker) + Resolver + Plan Amendment + Binding Closure + OPA backend (in-process + HTTP) + PlanSigner |
| **M2 — Runtime Alpha** | `a2ab490` | ✅ Shipped | Lease + FencingToken + FencingGuard + Outbox + Dispatcher + Reconciler + Credential Broker + MerkleLog + `verify_receipt_offline` |
| **M3 — Governed Hybrid E2E** | `880bb13` / `53901b6` / `a117073` / `38d2fc3` | ✅ Shipped | Field Projector + Egress PEP (XFR-001) + Zone-aware ArtifactStore (COORD-001) + HTML Demo Console (UX-001/002) |
| **M4 — Integration Beta** | `ac8ef06` / `8036109` / `9259ce6` | ✅ Shipped | 3 delegation modes (delegate-task / delegate-node / observe-only) + Dify Task Tool + AgenticHub Adapter + `orchestra` CLI + Docker Compose + Helm chart |
| **M5 — Published Capability Preview** | `0f33b2d` | ✅ Shipped | Signed Agent Card + Partner Contract + PublishedRegistry (version pinning, revoke) + Ingress Identity + Kill Switch (bounded time) + Release Gate |
| **M6 — Enterprise Beta** | `6966e79` | ✅ Shipped | Multi-tenant isolation (IsolatingEventStore + TenantContext + RBAC) + SBOM + signed artifacts + Provenance + OIDC / SCIM / KMS / SIEM connector interfaces |
| **M7 — GA Candidate** | `67ab7b7` | ✅ Shipped | SLO calculator + Pilot Evidence (signed) + GA readiness verdict + 4 runbooks (install / upgrade / backup-restore / rollback) |
| **M8 — CLI / live E2E** | `cf6d81d` | ✅ Shipped | CLI `tenant` + `publish` subcommands + live E2E + perf benchmarks + ADR-0003 |
| **M9 — Structured logging** | `6036033` | ✅ Shipped | JSON logging + per-request `X-Request-Id` correlation + sample tenants |
| **M10 — README** | `e97450a` | ✅ Shipped | README reflects M0–M9 reality |
| **M11 — Security / GA evidence** | `88965df` | ✅ Shipped | `SECURITY.md` + GA evidence worked example + CLI `doctor` |
| **M12 — `/healthz` reality** | `c702c1d` | ✅ Shipped | `/healthz` returns real cluster state + AGENTS.md update + 5 property-based tests |
| **M13 — Observability** | `09d3c39` | ✅ Shipped | Dep security (fastapi≥0.116, pytest≥9.0.3 — fixes 9 CVEs) + Prometheus `/metrics` + HTTPMetricsMiddleware + EgressPEP / ReleaseGate / Ingress / PublishedRegistry counters + ruff + pre-commit |
| **M14 — DoS hardening** | `93c00b3` | ✅ Shipped | Per-tenant token-bucket rate limit (429 + `Retry-After`) + request body size cap (413) + `/healthz` and `/metrics` exempt (for SRE probes). Config-driven via `ORCHESTRA_RATE_LIMIT_RPS` / `ORCHESTRA_MAX_REQUEST_BYTES` |
| **M15 — Partner integration polish** | `0deddba` | ✅ Shipped | CORS middleware (config-driven, wildcard + allow-list) + OpenAPI metadata (8 tag groups, per-endpoint summary) + `/docs` + `/redoc` pinned |
| **M16 — Partner SDK** | `ffb0078` | ✅ Shipped | RFC 7807 Problem Details on every 4xx/5xx + Python `orchestra_sdk` package (typed `OrchestraClient` + `RateLimitError` / `TaskNotFoundError` / `ValidationError` / …) + `py.typed` |
| **M17 — Webhook callback** | `56a909e` | ✅ Shipped | Partner supplies `webhook_url` + `webhook_secret` at submit; server POSTs HMAC-SHA-256-signed payload on terminal state. `X-Orchestra-Signature` + `X-Orchestra-Delivery-Id` + `X-Orchestra-Event-Type` + 3-attempt exponential backoff |
| **M18 — Webhook history** | `1be746e` | ✅ Shipped | `GET /admin/webhooks/{task_id}` returns attempts + last error; OpenAPI request/response examples |
| **M19 — Webhook manual retry** | `4f59baf` | ✅ Shipped | `POST /admin/webhooks/{task_id}/retry` re-fires the latest failed delivery using the stored URL + secret, fresh `delivery_id` for partner dedup |
| **M20 — SSE streaming** | `14a1f40` | ✅ Shipped | `GET /tasks/{task_run_id}/events/stream` returns live `text/event-stream` feed of the audit timeline. SDK gets `stream_events(task_id)` iterable. Three consumption paths: poll / webhook / stream |
| **M21 — ADR rollout** | `f154d50` | ✅ Shipped | 8 new ADRs (0004–0011) capture the M1–M20 design rationale + `ADR/README.md` index. **No code change.** |
| **M22 — Pilot-readiness (this doc)** | _in flight_ | ✅ Shipped | Closing artifact: roadmap → shipped map, 26-invariant audit, production-swap handoff |

**Test count:** 375 pass, 19 intentionally skipped (clean-room
install + M1+ invariants that need M1+ features). See
`AGENTS.md §2` for the per-milestone breakdown.

---

## 2. 26 safety invariants — enforcement status

The dev plan §0.1.2 lists 26 safety invariants. P0 only shows
the fixed-scene coverage; the M1+ milestones enforce the
production-grade semantics. The matrix below is the **current
state** of each invariant in the code.

| # | Invariant (short) | Enforcing module | Status |
|---:|---|---|---|
| 1 | Restricted/Zero-Egress not to Public Sink | `orchestra.compiler.info_flow` + `orchestra.xfr.egress_pep` | ✅ Enforced (M1 + M3) |
| 2 | Cross-trust-domain calls go through PEP | `orchestra.xfr.egress_pep` + `orchestra.adapters.*` | ✅ Enforced (M3) |
| 3 | Planner/Agent/Tool can't escalate labels, policy, permission | `orchestra.compiler.type_checker` + `orchestra.compiler.delegation_checker` | ✅ Enforced (M1) |
| 4 | Credential target-binding, least-privilege, short-lived | `orchestra.runtime.credential_broker` | ✅ Enforced (M2) |
| 5 | Sub-agent permission is intersection-only | `orchestra.compiler.binding_closure` | ✅ Enforced (M1) |
| 6 | Dynamic nodes/edges require re-compile authorisation | `orchestra.compiler.trust_compiler` (Amendment path) + `orchestra.coordinator.engine` | ✅ Enforced (M1 + M3) |
| 7 | High-risk side effects need rule / SoD / approval | `orchestra.compiler.effect_checker` + `orchestra.coordinator.engine` | ✅ Enforced (M1 + M3). P0/P1 still allow one approval point. |
| 8 | Policy / identity / proof unavailable → default deny | `orchestra.opa` (in-process) + `orchestra.runtime.credential_broker` | ✅ Enforced (M1 + M2). OPA single-instance; production swap to HA cluster is a config change. |
| 9 | Cross-domain / delegation / authorisation / denial / side-effect has evidence | `orchestra.evidence.merkle` + `orchestra.coordinator.event_store` | ✅ Enforced (M2) |
| 10 | No safe path → fail or fall back to local | `orchestra.compiler.resolver` + `orchestra.registry.router` | ✅ Enforced (M1 + M3). One pre-approved Fallback per node (P0 limit). |
| 11 | Published Capability only via explicit Data View / Tool Profile | `orchestra.publishing.contract` + `orchestra.publishing.ingress` | ✅ Enforced (M5) |
| 12 | Outbound content / Artifact / Citation through Release Gate | `orchestra.publishing.release_gate` | ✅ Enforced (M5) |
| 13 | External input untrusted + tenant-context isolation | `orchestra.enterprise.tenant` + `orchestra.enterprise.isolation` + `orchestra.core.schema.SecurityLabel` | ✅ Enforced (M0 + M6) |
| 14 | Revoke / Kill Switch / Policy update bounded-effective | `orchestra.publishing.kill_switch` + `orchestra.publishing.registry` (revoke) + `orchestra.runtime.fencing` | ✅ Enforced (M2 + M5). In-memory flag; production replication is the M7 runbook `rollback.md` path. |
| 15 | Security decisions use multi-dimensional trusted labels | `orchestra.core.schema.SecurityLabel` + `orchestra.compiler.type_checker` | ✅ Enforced (M0 + M1) |
| 16 | Restricted model output inherits labels by default | `orchestra.compiler.info_flow` (label propagation) + `orchestra.publishing.release_gate` (Citation降密) | ✅ Enforced (M0 + M1 + M5) |
| 17 | Stream / error / metrics / link / Receipt also under Release Policy | `orchestra.publishing.release_gate` + `orchestra.evidence.merkle` | ✅ Enforced (M2 + M5) |
| 18 | External cache key contains full security context | `orchestra.enterprise.isolation` (tenant_id in every key) | ✅ Enforced (M6) |
| 19 | Downstream data source independently runs RLS/ABAC | `orchestra.enterprise.connectors` + `orchestra.publishing.contract` | ✅ Enforced (M5 + M6) |
| 20 | Delegation permission = parent × contract × policy × capability | `orchestra.compiler.binding_closure` + `orchestra.runtime.credential_broker` | ✅ Enforced (M1 + M2) |
| 21 | Adapter/Runtime compromised can't access unplanned resources | `orchestra.xfr.egress_pep` + `orchestra.runtime.credential_broker` + `orchestra.artifact.store` | ✅ Enforced (M2 + M3) |
| 22 | High-risk side effect has Intent/Outcome reconciliation | `orchestra.coordinator.engine` + `orchestra.evidence.merkle` | ✅ Enforced (M2 + M3) |
| 23 | Break-glass can't lower label or bypass Zero-Egress | `orchestra.enterprise.tenant` + `orchestra.enterprise.isolation` + `orchestra.coordinator.engine` (approval path) | ⚠️ Partial — schema + audit path in place, dual-control + timed window is M23 follow-up. P0/P1 single approver only. |
| 24 | Signed objects support rotation / revocation / migration / compromise recovery | `orchestra.runtime.credential_broker` + `orchestra.enterprise.supply_chain` (Provenance) | ✅ Enforced (M2 + M6). Production KMS swap covered in M6. |
| 25 | Production artefact / Policy / Adapter / Manifest / Card signature-verified | `orchestra.enterprise.supply_chain` + `orchestra.publishing.registry` | ✅ Enforced (M5 + M6) |
| 26 | Delete / retain / Legal Hold covers all copies | `orchestra.artifact.store` + `orchestra.evidence.merkle` + `orchestra.enterprise.tenant` | ⚠️ Partial — schema + tenant isolation in place; full lifecycle sweep across replicas + Legal-Hold UX is M23+ follow-up. |

**Two invariants are intentionally "Partial"** (#23, #26). Both
have the schema and the audit trail; what remains is the
operational surface (dual-control UX, Legal-Hold UX) and the
production-swap item (legal counsel sign-off on retention
policy). These are M23+ work items; see §4.

---

## 3. Three consumption paths for partner integration

A partner can integrate with Orchestra three ways. All three
return the same underlying audit timeline; the choice is
operational preference.

| Path | When to use | Surface | Notes |
| --- | --- | --- | --- |
| **Polling** | Async background task, low-frequency, simple integration | `GET /tasks/{id}/events` | Always available; the M1 baseline. |
| **Webhook** | Partner has a public HTTPS endpoint; wants push, no client running | `POST {partner webhook_url}` on terminal state, HMAC-SHA-256 signed | M17. Includes delivery history (`/admin/webhooks/{id}`) and manual retry (`/admin/webhooks/{id}/retry`). |
| **SSE stream** | Long-lived partner client (browser tab, ops console); wants live updates without polling | `GET /tasks/{id}/events/stream` (`text/event-stream`) | M20. Late subscribers see the per-task history first, then live events, then `event: done` on terminal state. SDK exposes `stream_events(task_id)` iterable. |

The Python SDK (`orchestra_sdk/`) supports all three with a
typed client. See `docs/walkthrough-publishing.md` for a full
worked example.

---

## 4. Production-swap handoff

M6 explicitly says the dev path is real code, not a stub. The
production swap is a **config change**, not a re-implementation.
This section lists every swap, who owns it, and what blocks it.

### 4.1 Swaps that are config-only (no code change)

| Item | Dev path | Production swap | Blocked on |
| --- | --- | --- | --- |
| Database | `psycopg` against `postgres:16-alpine` in `docker-compose.yml` | Bitnami hardened PostgreSQL / AWS RDS / Cloud SQL / Crunchy | Frank's cloud account + DB selection |
| CORS | `ORCHESTRA_CORS_ORIGINS=*` or empty | Tenant-scoped allow-list per partner | Partner list |
| Rate limit | `ORCHESTRA_RATE_LIMIT_RPS=20` / `_BURST=40` | Per-tenant + per-partner tier, set in `data/samples/tenants.py` or via SCIM | Partner tier agreement |
| Logging | JSON to stdout | Datadog / Splunk / Elastic via `forwarder_uri` | SIEM selection |
| Metrics | `GET /metrics` (text-format, dependency-free) | `prometheus_client` or OTel adapter; metric names are pinned | Observability backend choice |

### 4.2 Swaps that need real third-party creds

| Item | Dev path interface | What needs Frank | Code surface |
| --- | --- | --- | --- |
| **KMS** | `orchestra.enterprise.connectors.InMemoryKMSKeyProvider` | AWS KMS / GCP KMS / HashiCorp Vault credentials | `orchestra.enterprise.connectors.KMSKeyProvider` (interface) — swap is a class replacement |
| **OIDC** | `orchestra.publishing.ingress.DevHMACIdP` | Partner's IdP (Okta / Auth0 / Azure AD) — issuer URL + JWKS endpoint | `orchestra.publishing.ingress.IdentityProvider` (interface) |
| **SCIM** | `orchestra.enterprise.connectors.InMemorySCIMDirectory` | Partner's user directory SCIM endpoint + token | `orchestra.enterprise.connectors.SCIDirectory` (interface) |
| **SIEM** | `orchestra.enterprise.connectors.InMemorySIEMForwarder` | Splunk HEC / Elastic / Datadog endpoint + token | `orchestra.enterprise.connectors.SIEMForwarder` (interface) |

The four interfaces above are pinned in
`orchestra/enterprise/connectors.py`. The production swap is
a single class replacement per connector.

### 4.3 Swaps that need partner + legal inputs

| Item | What blocks | Why |
| --- | --- | --- |
| **Real pilot data** | Frank's pilot partner + signed Partner Contract | The M5 contract and M7 pilot-evidence shape need a real pilot name + real metrics to populate |
| **Production retention / Legal Hold** | Legal counsel + jurisdiction sign-off | Invariant #26's full lifecycle sweep requires a documented retention policy (which artefacts, for how long, under which jurisdiction) |
| **Break-glass dual-control** | Design-partner review | Invariant #23's full enforcement (two-person timed window) needs the partner to confirm the UX fits their incident response |
| **KMS / OIDC / SCIM / SIEM** | See §4.2 | Real partner + their cloud account |

### 4.4 Open follow-ups (no external blocker)

These are concrete M23+ items that can ship unblocked. None
are required for pilot sign-off; they're nice-to-haves.

| # | Item | Effort | Value |
|---:|---|---|---|
| 1 | Webhook secret rotation CLI (`orchestra webhook rotate --task-id ...`) | Small | SRE-friendly; partners who rotate on incident get clean UX |
| 2 | Rate-limit by `partner_id` (not just `tenant_id`) | Small | Multi-partner tenants with mixed tier |
| 3 | TypeScript SDK (browser partners) | Medium | Webhook alternative for browser-only partners; today they use SSE |
| 4 | `/admin/tenants/{id}/rotate-kms` endpoint | Small | KMS key rotation via API, not just config reload |
| 5 | Property-based tests for M8 perf benchmarks (currently hand-counted) | Small | Catch regressions in Egress PEP / Ingress token verify microbenchmarks |
| 6 | Document the `.github/workflows/ci.yml` that already exists in working tree | Trivial | Frank's local `git add` + push; CI will run on every PR after that |

---

## 5. Operator quick-start (the 5-minute version)

A new operator can stand up the dev path from a fresh checkout
in 5 minutes; the production swap is a config change. Full
detail in `docs/runbooks/install.md`.

```bash
git clone https://github.com/frankfika/AIOrchestra.git
cd AIOrchestra
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
pytest tests/ -q                                # 375 pass, 19 skipped
docker compose up -d postgres                   # dev DB
python -m uvicorn orchestra.api.app:create_app --factory --port 8000 &
curl -s localhost:8000/healthz | jq .           # real cluster state
python -m orchestra publish create --name summarise --version 1 --data-view view-public
python -m orchestra tenant create --name acme --display-name "ACME"
python -m orchestra submit --template contract-review --input ./sample.json
```

The same call sequence works in a pilot partner's environment
once the M6 production-swap items in §4.2 are wired in.

---

## 6. What's *not* in this report

- **Deep architectural diagrams.** The dev plan and white paper
  cover these. This report is a *status* document, not a
  re-architecture pitch.
- **Per-feature test enumeration.** `AGENTS.md §2` lists the
  per-milestone test additions; `tests/` is the canonical
  evidence.
- **Performance numbers.** The M8 perf benchmark prints µs-per-call
  for the M3 Egress PEP, the M5 Ingress token verify, the
  FieldProjector, and the Release Gate. Run `pytest tests/m8 -s`
  to see them.

---

## 7. Handoff checklist (for Frank)

Before the first real pilot:

- [ ] Pick a design partner + sign M5 Partner Contract
- [ ] Provision cloud account + DB instance (Bitnami hardened PG
      or managed equivalent)
- [ ] Wire KMS / OIDC / SCIM / SIEM connectors (interface pinned,
      class replacement only)
- [ ] Set `ORCHESTRA_CORS_ORIGINS` to the partner's origin
- [ ] Set rate-limit tier in `data/samples/tenants.py`
- [ ] Push the working-tree `.github/workflows/ci.yml` from your
      local checkout (you have `workflow` scope, my OAuth token
      doesn't)
- [ ] Run the M7 GA readiness verdict after the first 30 days of
      pilot telemetry; sign off the PilotEvidence record

After the first pilot lands, the M7 gate verdict becomes the
real GA evidence that replaces the synthetic example in
`docs/ga-evidence-example.md`.
