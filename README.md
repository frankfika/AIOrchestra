# Orchestra — Hybrid / Sovereign AI Orchestration Plane

> **Status: M0–M7 complete (white paper, all stages). M8 / M9
> production hardening landed; ready for pilot onboarding.**
>
> P0 (category proof) → M0 (spec-preview) → M1 (compiler-alpha)
> → M2 (runtime-alpha) → M3 (hybrid-e2e) → M4 (integration-demo)
> → M5 (publishing-preview) → M6 (enterprise-beta) →
> M7 (ga-candidate). 241 tests, 18 intentionally skipped
> (clean-room install + M1+ invariants that need M1+ features).
> See [`Orchestra_开发计划.md`](./Orchestra_开发计划.md) for the
> per-milestone scope and the
> [`docs/walkthrough-publishing.md`](./docs/walkthrough-publishing.md)
> for the canonical pilot narrative.

The full product definition is in
[`Orchestra_Hybrid_Sovereign_AI_Orchestration_Plane_产品白皮书.md`](./Orchestra_Hybrid_Sovereign_AI_Orchestration_Plane_产品白皮书.md).
Higher-priority rules win: when the white paper and the dev plan
disagree on **product principle or safety boundary**, the white paper
wins; on **schedule or acceptance detail**, the dev plan wins.
Disagreements go in `ADR/`.

## What's in this repo

| Milestone | What it ships | Module |
| --- | --- | --- |
| **P0** | Fixed Contract Review Template + 3 reference Adapters + Node Grant + signed Receipt + PG Event Store + 3-baseline Benchmark + Dify Task Tool | `orchestra.coordinator`, `orchestra.adapters`, `orchestra.dify`, `orchestra.benchmarks` |
| **M0** | Frozen spec extensions (ValueRef, Requirement, InformationFlowRule, FieldManifest, Citation) + 26-invariants matrix | `orchestra.core`, `spec/` |
| **M1** | Trust Compiler (parser / normalizer / type-checker / info-flow / effect-checker / delegation-checker) + Resolver + Plan Amendment + Binding Closure + OPA backend (in-process + HTTP) + PlanSigner | `orchestra.compiler`, `orchestra.opa` |
| **M2** | Lease + FencingToken + FencingGuard + Outbox + Dispatcher + Reconciler + Credential Broker + MerkleLog + Offline Receipt verify | `orchestra.runtime`, `orchestra.evidence` |
| **M3** | Field Projector + Egress PEP (XFR-001) + Zone-aware ArtifactStore (COORD-001) + HTML Demo Console (UX-001/002) | `orchestra.xfr`, `orchestra.artifact`, `orchestra.ux` |
| **M4** | 3 delegation modes (delegate-task / delegate-node / observe-only) + Dify Task Tool + AgenticHub Adapter + orchestra CLI + Docker Compose + Helm chart | `orchestra.integrations`, `orchestra.agentichub`, `orchestra.cli`, `Dockerfile`, `docker-compose.yml`, `deploy/helm/` |
| **M5** | Signed Agent Card + Partner Contract + PublishedRegistry (version pinning, revoke) + Ingress Identity + Kill Switch (bounded time) + Release Gate | `orchestra.publishing` |
| **M6** | Multi-tenant isolation (IsolatingEventStore + TenantContext + RBAC) + SBOM + signed artifacts + Provenance + OIDC / SCIM / KMS / SIEM connector interfaces | `orchestra.enterprise` |
| **M7** | SLO calculator + Pilot Evidence (signed) + GA readiness verdict + 4 runbooks (install / upgrade / backup-restore / rollback) | `orchestra.ga`, `docs/runbooks/` |
| **M8** | CLI `tenant` + `publish` subcommands + live E2E + perf benchmarks + ADR-0003 | `orchestra.cli`, `docs/walkthrough-publishing.md` |
| **M9** | Structured JSON logging + per-request id correlation + sample tenant + Agent Card data | `orchestra.core.logging`, `data/samples/tenants.py` |

## Quick start

### Local dev

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# One-time: create the orchestra role and database
psql -U postgres -c "create user orchestra with password 'orchestra' superuser;"
psql -U postgres -c "create database orchestra owner orchestra;"

# Boot the demo
python3 -m uvicorn orchestra.api.app:create_app --factory --host 127.0.0.1 --port 8000

# In another shell: submit a contract
orchestra submit --contract ctr-001 --text "..." --vendor demo
```

The demo console is at <http://127.0.0.1:8000/>; the JSON API
at <http://127.0.0.1:8000/tasks>. The healthz endpoint
`/healthz` returns 200; the capabilities summary at
`/capabilities`.

### Docker

```bash
docker compose up -d
# orchestra on :8000, postgres on :5432
orchestra --base http://localhost:8000 capabilities
```

### Helm (production-shape)

```bash
helm install orchestra ./deploy/helm \
  --set image.tag=0.1.0 \
  --set postgres.host=postgres.example.internal \
  --set postgres.existingSecret=orchestra-postgres
```

See [`docs/runbooks/install.md`](./docs/runbooks/install.md) for
the canonical install path, and
[`docs/runbooks/upgrade.md`](./docs/runbooks/upgrade.md) for the
rolling-upgrade procedure.

## Verification

```bash
pytest tests/                  # 241 passed, 19 intentionally skipped
pytest tests/m8/test_perf.py -v -s  # perf printout
```

The perf test prints µs-per-call for the M3 Egress PEP, the
M5 Ingress token verify, the FieldProjector, and the
Release Gate. Dev-path numbers are sub-microsecond; the
production swap's KMS / OIDC verifier will be the bottleneck.

## Architecture

```
                          ┌──────────────────────────┐
                          │      app / CLI / UX      │
                          └────────────┬─────────────┘
                                       │
            ┌──────────────────────────┼──────────────────────────┐
            │                          │                          │
   ┌────────▼────────┐        ┌────────▼────────┐        ┌────────▼────────┐
   │  M5 Publishing  │        │  M8 Admin API   │        │  M3 Demo Console │
   │  Card+Registry  │        │  tenant/publish │        │  (3 role views)  │
   └────────┬────────┘        └────────┬────────┘        └────────┬────────┘
            │                          │                          │
            └──────────────┬───────────┴──────────────────────────┘
                           │
                ┌──────────▼──────────┐
                │     Coordinator     │  M1 compiler + M2 runtime
                │  Plan + Receipt     │
                └──────────┬──────────┘
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
 ┌──────▼──────┐   ┌───────▼──────┐   ┌───────▼──────┐
 │ M3 Egress   │   │ M5 Release  │   │ M3 Artifact │
 │  PEP       │   │    Gate     │   │    Store    │
 └──────┬─────┘   └───────┬──────┘   └───────┬──────┘
        │                  │                  │
        └──────────────────┼──────────────────┘
                           │
                ┌──────────▼──────────┐
                │   M6 Isolating      │  tenant_id row filter
                │   EventStore        │
                └──────────┬──────────┘
                           │
                ┌──────────▼──────────┐
                │   PostgreSQL 16     │
                └─────────────────────┘
```

## Runbooks

- [`docs/runbooks/install.md`](./docs/runbooks/install.md) — clean-room + Helm install
- [`docs/runbooks/upgrade.md`](./docs/runbooks/upgrade.md) — dev + Helm rolling
- [`docs/runbooks/backup-restore.md`](./docs/runbooks/backup-restore.md) — RPO 60s, RTO 15m
- [`docs/runbooks/rollback.md`](./docs/runbooks/rollback.md) — Kill Switch, plan amendment, credential rotation
- [`docs/walkthrough-publishing.md`](./docs/walkthrough-publishing.md) — pilot / investor narrative

## ADRs

- [`ADR/0001-monorepo-structure.md`](./ADR/0001-monorepo-structure.md) — why monorepo
- [`ADR/0002-p0-boundary-and-not-in-scope.md`](./ADR/0002-p0-boundary-and-not-in-scope.md) — the P0 matrix
- [`ADR/0003-tenant-isolation.md`](./ADR/0003-tenant-isolation.md) — why row-level tenant_id

## License

Apache-2.0. See [`LICENSE`](./LICENSE).

## What you can do next

1. **Pilot onboarding** — `from data.samples.tenants import *; client.post("/admin/tenants", json=ACME_TENANT)`.
2. **Run the GA calculator** — `from orchestra.ga import collect_pilot_evidence; collect_pilot_evidence(...)`.
3. **Push your `.github/workflows/ci.yml`** — your OAuth token has the `workflow` scope; my token does not.
4. **Swap the dev connectors** for production ones (Okta, AWS KMS, Splunk) — the M6 interface is the same.

The code-side deliverable is complete. The remaining items
are operational and require your inputs (real pilot data, real
credentials, real production tuning).
