# Orchestra — Hybrid / Sovereign AI Orchestration Plane

> **Status: P0 (category proof) — runnable, audited, not production.**
> P0 demonstrates that an enterprise would let Orchestra combine a
> local model, a public model, an A2A agent, a human approval, and a
> Mock Procurement Sink on one Contract Review task, and that the
> route, the per-node grants, and the signed receipts are all
> auditable.
>
> See [`Orchestra_开发计划.md`](./Orchestra_开发计划.md) §0.1.1 for the
> Milestone Consistency Matrix. P0 implements 5 features (FND-001,
> LIT-001..005); M0–M7 are future work.

The full product definition is in
[`Orchestra_Hybrid_Sovereign_AI_Orchestration_Plane_产品白皮书.md`](./Orchestra_Hybrid_Sovereign_AI_Orchestration_Plane_产品白皮书.md).
Higher-priority rules win: when the white paper and the dev plan
disagree on **product principle or safety boundary**, the white paper
wins; on **schedule or acceptance detail**, the dev plan wins.
Disagreements go in `ADR/`.

## What's in P0

| Layer | Real code | Out of scope (marked `not-in-scope`, see `ADR/0002`) |
|---|---|---|
| FND-001 monorepo + CI + license + ADR template | ✅ | — |
| LIT-001 fixed Contract Review Template + Task/Capability/Event schema | ✅ | — |
| LIT-002 static Capability Registry + OPA-style Policy + Eligible Set + deterministic Router | ✅ | — |
| LIT-003 Local Model + OpenAI-compatible + in-repo A2A Reference Adapter | ✅ | — |
| LIT-004 minimal Coordinator + Node Grant + PostgreSQL Event Store + signed Receipt | ✅ | — |
| LIT-005 Benchmark Manifest + 3 baselines + Route/Audit Demo + Dify Task Tool entry | ✅ | — |
| Trust Compiler | — | `not-in-scope` (M1) |
| Binding Closure | — | `not-in-scope` (M1) |
| Fenced Runtime | — | `not-in-scope` (M2) |
| Enterprise Credential Broker | — | `not-in-scope` (M2 / M6) |
| Schema Projection + Egress PEP (beyond fixed demo) | — | `not-in-scope` (M3) |
| Merkle Backend | — | `not-in-scope` (M2) |

P0 is allowed: sequential nodes, limited fan-out, one pre-approved
Fallback, one approval point, Mock Procurement Sink only.
P0 must NOT call any of the above a "production" guarantee.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# One-time: create the orchestra role and database
psql -U postgres -c "create user orchestra with password 'orchestra' superuser;"
createdb -O orchestra orchestra

# Tests
pytest                              # all 23 tests, e2e auto-skip if PG is down
pytest tests/test_schema.py tests/test_registry.py tests/test_adapters.py   # smoke

# Run the demo
python -m uvicorn orchestra.api.app:create_app --factory --port 8000 --log-level info
```

Then in another shell:

```bash
# 1. Submit a contract
curl -s -X POST http://127.0.0.1:8000/tasks -H 'content-type: application/json' -d '{
  "contract_id": "ctr-001",
  "contract_text": "供应商：Acme\n采购方：Helios\n合同金额：RMB 8,600,000.00\n付款条款：Net 30\n生效日期：2026-01-15\n到期日期：2027-01-14\n管辖：香港\n终止条款：30日通知。",
  "vendor_id": "demo-vendor-001",
  "budget_usd": 2.0
}'

# 2. Approve the human review
curl -s -X POST http://127.0.0.1:8000/tasks/<task_run_id>/approve \
  -H 'content-type: application/json' \
  -d '{"decided_by": "frank", "rationale": "looks ok"}'

# 3. Inspect the audit timeline
curl -s http://127.0.0.1:8000/tasks/<task_run_id>/events  | jq '.count, [.events[].kind] | unique'

# 4. Verify the signed receipts
curl -s http://127.0.0.1:8000/tasks/<task_run_id>/receipts | jq '[.receipts[].verified] | all'

# 5. Run the 3-baseline benchmark
curl -s -X POST http://127.0.0.1:8000/benchmark/run | jq '.pareto_verdict, .baselines[].metrics'
```

A typical P0 run produces:

```text
events: 31
receipts: 3 (all verified)
baselines:
  all-local    fact=0.87 pub=0.00 cost=$0.0000 egress=0   B humans=0
  all-public   fact=0.00 pub=0.00 cost=$0.0020 egress=32  B humans=0
  hybrid       fact=0.87 pub=1.00 cost=$0.0030 egress=256 B humans=1
verdict:
  not_dominated: true
  hypothesis_quality_exposure: true
  hypothesis_quality_cost: true
```

Full demo walkthrough, including the negative test (restricted data
blocked from public Adapter), is in
[`docs/p0_demo_guide.md`](./docs/p0_demo_guide.md).

## Repository layout

```text
orchestra/                  # the P0 Python package
  core/                     # frozen schema, ids, hashing, errors
  registry/                 # manifest store + OPA-style policy + router
  coordinator/              # event store, node grant, receipt, engine
  adapters/                 # 3 reference adapters + 4 in-repo servers
  templates/                # fixed Contract Review Task Template
  api/                      # FastAPI surface
  benchmarks/               # 3-baseline runner + manifest
  dify/                     # Dify Task Tool reference entry
data/samples/               # synthetic contract corpus (no real data)
tests/                      # 23 tests; e2e auto-skip without PG
ADR/                        # 0001 monorepo, 0002 P0 boundary
docs/p0_demo_guide.md       # full walkthrough
```

## Why "verified and usable"

The P0 demo is **real** in the following sense, verified by automated
tests:

1. The 23-test suite passes against a real PostgreSQL 16 instance.
2. The four Adapter servers are real FastAPI/uvicorn processes, not
   mocks. The Coordinator talks to them over real HTTP.
3. The Event Store is a real PostgreSQL schema; the audit timeline
   is a real ordered event log; the Receipt is a real COSE-like
   HMAC envelope that the API re-verifies on every read.
4. The Router's choice is deterministic and reproducible: same
   inputs → same chosen Capability and same rationale.
5. The Policy denies `restricted → public-model` (invariant #1)
   unless the destination node is the dedicated `public_research`
   node (verified by `test_router_does_not_allow_internal_to_public_outside_public_research`).
6. The 3-baseline benchmark demonstrates the hybrid is **not
   Pareto-dominated** by either single-environment baseline, and
   both pre-registered hypotheses (quality-exposure and
   quality-cost) are satisfied.

It is **not production** in the following sense (per the dev plan
and `ADR/0002`):

- No Trust Compiler, Binding Closure, Fenced Runtime, Enterprise
  Credential Broker, real Schema Projection + Egress PEP, or
  Merkle Backend. These are explicitly `not-in-scope`; calling the
  P0 demo "production secure" is forbidden by the dev plan.
- The Policy is an in-process Rego-like engine, not a real OPA
  sidecar. The M1 swap is mechanical: the engine exposes the same
  decision shape.
- Node Grants are HMAC-SHA256 dev credentials bound to a single
  tenant. They are not OAuth/SPIFFE, not short-lived enough for
  cross-tenant rotation, and not revoked on `AuthorityEpoch`
  change.

## Contributing

See `AGENTS.md` for the project-level invariants every change must
respect, and the dev plan §0.5/§0.6/§0.7 for Definition of Ready,
Definition of Done, and the agent delivery format.

PR titles must include a Feature ID (`[LIT-003] …`).
