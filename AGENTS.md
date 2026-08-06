# AGENTS.md — Project Memory for AI Agents

This file is consumed by AI coding agents (Mavis, Codex, Cursor,
Aider, Gemini CLI, …) working inside the Orchestra monorepo. It
encodes the **project-level invariants** that must be respected
on every change. Higher-priority rules (orchestra/白皮书, /开发计划)
win on conflict; this file is the bridge between those documents
and the agent's daily decisions.

> **Status: M0–M13 shipped.** The original P0 matrix in §2 is
> preserved for traceability; the actual production surface is
> M0 + M1 + M2 + M3 + M4 + M5 + M6 + M7 + M8 + M9 + M10 + M11 + M12 + M13.
> The "P0 not-in-scope" column is the **historical** P0 boundary;
> the dev plan moved those items to M1+ in later milestones.

## 1. What this project is

**Orchestra** is a Hybrid / Sovereign AI Orchestration Plane. It
is a *control plane* between applications (Dify, Coze, AgenticHub,
custom UIs) and execution resources (local models, public models,
A2A agents, MCP tools, human approvers). The full product
definition is in
`Orchestra_Hybrid_Sovereign_AI_Orchestration_Plane_产品白皮书.md`;
the engineering schedule, milestones, and gates live in
`Orchestra_开发计划.md`. When those two documents disagree on
**product principle or safety boundary**, the white paper wins;
on **schedule or acceptance detail**, the dev plan wins.
Disagreements must be captured as an ADR (`ADR/NNNN-*.md`); agents
may not silently resolve them.

## 2. Milestone history (P0 + M0–M11)

| Milestone | Commit prefix | What it shipped | Key modules |
| --- | --- | --- | --- |
| **P0** | `aabf0c9` … `bdadb78` | Fixed Contract Review Template + 3 reference Adapters + Node Grant + signed Receipt + PG Event Store + 3-baseline Benchmark + Dify Task Tool | `orchestra.coordinator`, `orchestra.adapters`, `orchestra.dify`, `orchestra.benchmarks` |
| **M0** | `70a4830` `471c746` | Frozen spec extensions (ValueRef, Requirement, InformationFlowRule, FieldManifest, Citation) + 26-invariants matrix | `orchestra.core`, `spec/` |
| **M1** | `d07113a` | Trust Compiler (parser / normalizer / type-checker / info-flow / effect-checker / delegation-checker) + Resolver + Plan Amendment + Binding Closure + OPA backend (in-process + HTTP) + PlanSigner | `orchestra.compiler`, `orchestra.opa` |
| **M2** | `a2ab490` | Lease + FencingToken + FencingGuard + Outbox + Dispatcher + Reconciler + Credential Broker + MerkleLog + Offline Receipt verify | `orchestra.runtime`, `orchestra.evidence` |
| **M3** | `880bb13` `53901b6` `a117073` `38d2fc3` | Field Projector + Egress PEP (XFR-001) + Zone-aware ArtifactStore (COORD-001) + HTML Demo Console (UX-001/002) | `orchestra.xfr`, `orchestra.artifact`, `orchestra.ux` |
| **M4** | `ac8ef06` `8036109` `9259ce6` | 3 delegation modes (delegate-task / delegate-node / observe-only) + Dify Task Tool + AgenticHub Adapter + orchestra CLI + Docker Compose + Helm chart | `orchestra.integrations`, `orchestra.agentichub`, `orchestra.cli`, `Dockerfile`, `docker-compose.yml`, `deploy/helm/` |
| **M5** | `0f33b2d` | Signed Agent Card + Partner Contract + PublishedRegistry (version pinning, revoke) + Ingress Identity + Kill Switch (bounded time) + Release Gate | `orchestra.publishing` |
| **M6** | `6966e79` | Multi-tenant isolation (IsolatingEventStore + TenantContext + RBAC) + SBOM + signed artifacts + Provenance + OIDC / SCIM / KMS / SIEM connector interfaces | `orchestra.enterprise` |
| **M7** | `67ab7b7` | SLO calculator + Pilot Evidence (signed) + GA readiness verdict + 4 runbooks (install / upgrade / backup-restore / rollback) | `orchestra.ga`, `docs/runbooks/` |
| **M8** | `cf6d81d` | CLI `tenant` + `publish` subcommands + live E2E + perf benchmarks + ADR-0003 | `orchestra.cli`, `docs/walkthrough-publishing.md` |
| **M9** | `6036033` | Structured JSON logging + per-request id correlation + sample tenant + Agent Card data | `orchestra.core.logging`, `data/samples/tenants.py` |
| **M10** | `e97450a` | README update (M0–M9 reality) | `README.md` |
| **M11** | `88965df` | SECURITY.md + GA evidence example + CLI `doctor` | `SECURITY.md`, `docs/ga-evidence-example.md` |
| **M12** | in flight | Fix /healthz (real cluster state) + AGENTS.md update + property-based tests | — |
| **M13** | `09d3c39` | Dep security upgrade (fastapi>=0.116, pytest>=9.0.3, fixed 9 CVEs) + Prometheus metrics (`/metrics` text-format + HTTPMetricsMiddleware + EgressPEP/ReleaseGate/Ingress/PublishedRegistry counters) + ruff + pre-commit hooks | `orchestra.observability`, `.pre-commit-config.yaml` |
| **M14** | (in flight) | DoS hardening: per-tenant token-bucket rate limit (429 + Retry-After) + request body size cap (413) + exempt `/healthz`/`/metrics` for SRE probes. Config-driven via `ORCHESTRA_RATE_LIMIT_RPS` / `ORCHESTRA_MAX_REQUEST_BYTES`. | `orchestra.runtime.rate_limit`, `orchestra.observability.rate_limit_mw` |

248 tests pass; 19 intentionally skipped (clean-room install +
M1+ invariants that need M1+ features). M13 adds 32 tests
(metrics primitives + instrumentation + endpoint) for a total
of 280 active. M14 adds 22 tests (token bucket, middleware,
request size limit) for a total of 302 active.

### Historical P0 boundary (preserved for traceability)

The M0 + M1 + M2 + M3 + M4 + M5 + M6 + M7 milestones implemented
the features that P0 marked `not-in-scope`. The P0 matrix below
is the historical record; treat the **per-milestone** column
above as authoritative for what exists today.

| P0 not-in-scope (historical) | Implemented in |
| --- | --- |
| Trust Compiler | **M1** — `orchestra.compiler.trust_compiler` |
| Binding Closure | **M1** — `orchestra.compiler.binding_closure` |
| Fenced Runtime | **M2** — `orchestra.runtime.fencing` |
| Enterprise Credential Broker | **M2 / M6** — `orchestra.runtime.credential_broker`, `orchestra.enterprise.isolation` |
| Schema Projection + Egress PEP (beyond fixed demo) | **M3** — `orchestra.xfr.{projector, egress_pep}` |
| Merkle Backend | **M2** — `orchestra.evidence.merkle` |
| Real cross-tenant / multi-region / zero-leak | **M6** — multi-tenant isolation (production swap is M6/M7) |
| Production-grade free-text scrubbing | **out of scope** — M5 Release Gate only does structured release |

## 3. Schemas and naming

The frozen P0 + M0 vocabulary is in `orchestra/core/schema.py`. Do
not introduce parallel types with the same name. Names like
`SecurityLabel`, `Node Grant`, `Lease`, `Receipt`, `Data View`,
`Plan Amendment`, `Capability Manifest`, `FieldManifest`,
`AgentCard`, `Citation`, `Partner Contract`, `Tenant` are
reserved and must match the white paper's semantics. The full
M0 freeze list is in `Orchestra_开发计划.md` §0.1.2 — P0
implements the subset actually used.

## 4. Test discipline

- Every Feature has at least one positive test, one negative
  test, and one failure test, mapped to the 26-invariants
  matrix when relevant.
- Tests must be runnable in a clean environment (see
  `docs/runbooks/install.md`).
- An e2e test that depends on PostgreSQL is marked
  `@pytest.mark.e2e` and skipped when the DB is unavailable so
  a developer without Postgres still sees a green smoke run.
- A passing test is **not** proof a feature is done — see
  §0.6 of the dev plan.
- The M8 perf test prints µs-per-call for the M3 Egress PEP, the
  M5 Ingress token verify, the FieldProjector, and the
  Release Gate. Run with `-s` to see the printout.

## 5. What agents must NOT do

- **Do not** claim an invariant is enforced when the
  implementation is a stub. Add `not-in-scope` to the README
  / docs and link the invariant number.
- **Do not** add adapters, capabilities, or storage backends
  that are not in the milestone matrix without an ADR.
- **Do not** commit secrets, real keys, or PII. The dev path
  uses `hmac_keygen()` and a per-process random signing key.
- **Do not** push directly to `main` without a review. PR
  titles must include the Milestone / Feature ID (e.g.
  `[M12] /healthz returns real cluster state`).
- **Do not** add a `git add .` or `git add -A` — list the
  specific files. The OAuth App token does not have
  `workflow` scope; `.github/workflows/*.yml` must stay
  untracked and be pushed by the operator (Frank) with
  workflow scope.

## 6. Quick orientation

| What you want | Where it lives |
| --- | --- |
| Schema (SecurityLabel, Plan, Receipt, etc.) | `orchestra/core/schema.py` |
| Coordinator engine | `orchestra/coordinator/engine.py` |
| Egress PEP + Field Projector | `orchestra/xfr/` |
| Artifact store | `orchestra/artifact/store.py` |
| Publishing (Cards, Registry, Ingress, Kill Switch, Release Gate) | `orchestra/publishing/` |
| Multi-tenant isolation | `orchestra/enterprise/isolation.py` |
| GA readiness + SLO calculator | `orchestra/ga/` |
| Observability (Prometheus metrics + HTTP middleware) | `orchestra/observability/` |
| Rate limiter (per-tenant token bucket) | `orchestra/runtime/rate_limit.py` |
| CLI | `orchestra/cli.py` |
| Demo Console (HTML) | `orchestra/ux/` |
| Helm chart | `deploy/helm/` |
| Runbooks | `docs/runbooks/` |
| Pilot walkthrough | `docs/walkthrough-publishing.md` |
| GA evidence example | `docs/ga-evidence-example.md` |
| Security policy | `SECURITY.md` |
| ADRs | `ADR/` |
| Sample tenant / Agent Card | `data/samples/tenants.py` |
| Tests | `tests/m{0..14}/` |
| Verification command | `pytest tests/` (302 pass, 19 skipped) |

## 7. Production swap checklist (M6 → production)

The dev path is real code, not a stub. The production swap is
a config change, not a re-implementation:

- **KMS**: replace `InMemoryKMSKeyProvider` with AWS KMS / GCP
  KMS / HashiCorp Vault. The interface is in
  `orchestra/enterprise/connectors.py`.
- **OIDC**: replace `DevHMACIdP` with the partner's IdP. The
  token format and claims are pinned by `BearerToken.from_dict`.
- **SCIM**: replace `InMemorySCIMDirectory` with the partner's
  user directory.
- **SIEM**: replace `InMemorySIEMForwarder` with Splunk HEC /
  Elastic / Datadog.
- **PostgreSQL**: pin a hardened image (Bitnami's hardened
  PostgreSQL, or a managed service).
- **Pinned deps**: `fastapi>=0.116` (pulls starlette >= 1.0.1,
  fixes PYSEC-2026-161/248/249/1941/1943/2280/2281) and
  `pytest>=9.0.3` (fixes PYSEC-2026-1845). See `SECURITY.md`.
- **Kill Switch + revoke**: the production path needs the
  in-memory flag replicated to the data plane via lease
  revocation. M7 runbook `rollback.md` covers the path.
