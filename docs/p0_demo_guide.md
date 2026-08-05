# Orchestra P0 Demo Guide

> P0 is a **category proof**, not a production product. It demonstrates
> that an enterprise would let Orchestra combine a local model, a
> public model, an A2A agent, a human approval, and a Mock Procurement
> Sink on one Contract Review task — and that the route, the per-node
> grants, and the signed receipts are all auditable.
>
> See `AGENTS.md` §2 and `ADR/0002-p0-boundary-and-not-in-scope.md` for
> what P0 deliberately *doesn't* implement (Trust Compiler, Binding
> Closure, Fenced Runtime, Enterprise Credential Broker, Schema
> Projection + Egress PEP, Merkle Backend).

## 1. Prerequisites

- Python ≥ 3.11
- PostgreSQL ≥ 14 (P0 uses one database for the Event Store)
- A local checkout of this repository
- (No GPU is required: the "local model" is a deterministic
  contract-fact extractor served in-repo.)

## 2. One-time setup

```bash
# 2.1 Create a Python venv
python3 -m venv .venv
source .venv/bin/activate

# 2.2 Install the package + dev extras
pip install -e ".[dev]"

# 2.3 Create the orchestra role and database (Postgres)
# Adjust the path to pg_ctl for your installation
psql -U postgres -c "create user orchestra with password 'orchestra' superuser;"
createdb -O orchestra orchestra
# (The DSN is "postgresql://orchestra:orchestra@127.0.0.1:5432/orchestra";
#  override with the DATABASE_URL env var if your setup differs.)
```

## 3. Tests

```bash
# Smoke tests (no PG required)
pytest tests/test_schema.py tests/test_registry.py tests/test_adapters.py

# All tests (PG-backed; e2e tests auto-skip if PG is down)
pytest
```

The P0 demo is "real" only when:

1. All 23 tests pass.
2. The 7 PDP rules deny `restricted → public-model` (invariant #1).
3. The Coordinator refuses to issue a Node Grant without a Manifest.
4. Each node emits a `node.started` → `io.intent` → `io.sent` → `io.received`
   → `receipt.signed` event chain, and the Receipt verifies with HMAC.

## 4. Running the demo

### 4.1 Boot the API + the four reference Adapter servers

```bash
python -m uvicorn orchestra.api.app:create_app --factory \
    --host 127.0.0.1 --port 8000 --log-level info
```

This single process:

- Connects to PostgreSQL and creates the `task_runs`, `node_runs`,
  `events`, `receipts`, `grants`, `approvals` tables on first run.
- Starts the four reference Adapter servers on free ports:
  - `local.contract-extractor` (deterministic fact extractor)
  - `public.openai-compat` (OpenAI Chat Completions mock)
  - `a2a.reference-agent` (in-repo A2A-style agent)
  - `sink.mock-procurement` (Mock Procurement Sink)
- Loads the static Capability Manifests and the single OPA-style
  Policy bundle.

### 4.2 Submit a contract for review

```bash
curl -s -X POST http://127.0.0.1:8000/tasks \
  -H 'content-type: application/json' \
  -d '{
    "contract_id": "ctr-001",
    "contract_text": "供应商：Acme Cloud Logistics Co., Ltd.\n采购方：Helios\n合同金额：RMB 8,600,000.00\n付款条款：Net 30\n生效日期：2026-01-15\n到期日期：2027-01-14\n管辖：香港\n终止条款：30日违约通知。",
    "vendor_id": "demo-vendor-001",
    "budget_usd": 2.0
  }'
# → {"task_run_id": "...", "state": "created", "plan_id": null, ...}
```

The Coordinator runs in the background. It plans all six nodes up
front, then:

1. Calls the Local extractor (RESTRICTED → INTERNAL).
2. Routes `public_research` to the A2A agent (the Router picks the
   eligible public capability with the highest score; the policy
   denies any restricted-to-public path that isn't the
   `public_research` node).
3. Deterministic merge produces the review summary.
4. Pauses at the `human_approval` gate and waits for `/approve`.

### 4.3 Approve the human review

```bash
curl -s -X POST http://127.0.0.1:8000/tasks/<task_run_id>/approve \
  -H 'content-type: application/json' \
  -d '{"decided_by": "frank", "rationale": "vendor looks fine"}'
```

The Coordinator unblocks, calls the Mock Procurement Sink, builds a
signed Receipt, and marks the task `succeeded`.

### 4.4 Inspect the audit timeline, grants, and receipts

```bash
# 31 events, all signed: task.received, plan.created, plan.signed,
# node.started, grant.issued, io.intent, io.sent, io.received,
# receipt.signed, node.succeeded, node.awaiting-approval,
# node.approved, task.completed
curl -s http://127.0.0.1:8000/tasks/<task_run_id>/events | jq '.count'

# Three Node Grants (one per non-approval, non-merge, non-ingest node),
# each binding: task, node, capability, data view, purpose, expiry.
curl -s http://127.0.0.1:8000/tasks/<task_run_id>/grants | jq '.grants[].capability_id'

# Three signed Receipts (HMAC over COSE-like envelope). All verify
# because the demo Coordinator knows the receipt key; the audit
# timeline is genuinely tamper-evident for one tenant.
curl -s http://127.0.0.1:8000/tasks/<task_run_id>/receipts | jq '.receipts[].verified'
```

### 4.5 Run the 3-baseline benchmark

```bash
curl -s -X POST http://127.0.0.1:8000/benchmark/run | jq .
```

A typical P0 run produces:

```text
all-local    fact=0.87 pub=0.00 cost=$0.0000 egress=0  B humans=0
all-public   fact=0.00 pub=0.00 cost=$0.0020 egress=32 B humans=0
hybrid       fact=0.87 pub=1.00 cost=$0.0030 egress=256 B humans=1

verdict:
  not_dominated: true
  hypothesis_quality_exposure: true
  hypothesis_quality_cost: true
```

The hybrid is **not** Pareto-dominated by either single-environment
baseline: it is the only one with both local contract facts (0.87) and
public research completeness (1.00). Both pre-registered hypotheses
(quality-exposure and quality-cost) are satisfied.

### 4.6 Dify Task Tool reference entry

`orchestra.dify.task_tool.DifyTaskTool` is the Python shape of the
Dify Task Tool. A Dify plugin author can translate the
`submit_contract` / `approve` / `get_state` methods into a Task Tool
spec; the wire protocol is just the HTTP calls already exercised
above.

```python
from orchestra.dify.task_tool import DifyTaskTool
tool = DifyTaskTool(base_url="http://127.0.0.1:8000")
res = await tool.submit_contract(
    contract_id="ctr-001",
    contract_text="...",
    vendor_id="demo-vendor-001",
)
# res.audit_url  = "http://127.0.0.1:8000/tasks/<id>/events"
# res.route_url  = "http://127.0.0.1:8000/tasks/<id>/grants"
```

## 5. What the demo deliberately does not do

These are the P0 `not-in-scope` items. They appear in code as
`NotInScopeError` and in this guide as the truth.

- **Trust Compiler.** The Plan is accepted as a Plan; no info-flow,
  effect, or delegation analysis is run. M1.
- **Binding Closure.** The Router picks one Capability per Node from
  the static Eligible Set; no abstract-graph → concrete-binding
  closure is computed. M1.
- **Fenced Runtime.** Node Runs are short-lived. There are no Lease,
  Fencing Tokens, Outbox, or Reconciler. M2.
- **Enterprise Credential Broker.** Node Grants are locally-signed
  dev credentials (HMAC-SHA256 over a COSE-like envelope). They are
  not OAuth Token Exchange, not SPIFFE/SPIRE-bound, and not
  multi-tenant. M2 / M6.
- **Schema Projection + Egress PEP (beyond the fixed demo).** The
  public-model Adapter enforces a hard-coded message schema; the
  Coordinator does not run a generic field-by-field projection over
  arbitrary outputs. M3.
- **Merkle Backend.** Receipts are individually signed; no Merkle
  log, no inclusion proof. M2.

If a reader sees any of these described as "implemented" anywhere in
the code, file an issue — that is a Milestone Consistency Matrix
violation per the dev plan §0.1.1.

## 6. Reproducing the demo from a clean checkout

```bash
# Drop the PG tables, then re-boot.
psql -U orchestra -d orchestra -c "
  drop table if exists events, receipts, grants, approvals, node_runs, task_runs cascade;
"

# Re-run the demo and the benchmark. The numbers are deterministic
# because the Local extractor is deterministic, the public mock is
# deterministic, and the Router score function is deterministic.
pytest -q
python -m uvicorn orchestra.api.app:create_app --factory --port 8000 --log-level warning &
# submit one contract, approve it, fetch /events, /receipts, /grants
```
