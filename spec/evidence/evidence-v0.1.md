# SPEC-003 — Execution Event & Receipt v0.1

> **Status:** Frozen at M0.
> **Owner:** Agent-1 (Spec Architect) + Agent-9 (Evidence Plane Developer).
> **Relates to:** Dev plan §0.2 (SPEC-003), white paper §3.7 (Evidence
> & Audit Plane), §6.2 (security invariants), ADR-0002 (P0 boundary).

## 1. What an Event is

An **Event** is the unit of truth the Event Store appends. It is
**immutable, content-addressed, and signed** when it becomes a
**Receipt**. Every node-level state transition produces at least
three events: `node.started`, one or more `io.*` events, and
`node.succeeded` / `node.failed`. The Coordinator emits the events
in a defined order so an auditor can replay a task from the log.

## 2. Event kinds (M0 frozen set)

The frozen set is in `orchestra.core.schema.EventKind`. P0 already
ships the lifecycle + data-flow + decision events. M0 extends the
set with:

| Event kind | When | M0 invariant coverage |
|---|---|---|
| `task.received` | upper layer submitted a contract | #9 (cross-domain evidence) |
| `plan.created` | Trust Compiler produced a Plan | — |
| `plan.signed` | Plan Signer signed the Plan | #24 (signature rotation) |
| `plan.amended` | a Plan Amendment was applied | #6 (dynamic nodes re-compile) |
| `node.started` | node execution began | #9, #22 (Intent/Outcome) |
| `node.awaiting-approval` | human approval gate opened | #7 (high-risk effect approval) |
| `node.approved` | human approved | #7 |
| `node.rejected` | human rejected | #7 |
| `node.succeeded` | node execution finished cleanly | #22 |
| `node.failed` | node execution failed | #9 (deficiency triggers anomaly) |
| `task.completed` | task run finished | #9 |
| `task.failed` | task run failed | #9 |
| `io.intent` | node declared what it would send | #22 (Intent/Outcome) |
| `io.sent` | node sent bytes | #1, #2 (PEP) |
| `io.received` | node received bytes | #9 |
| `external.outcome` | external system replied | #22 |
| `policy.decision` | PDP made a decision | #8 (no default-allow) |
| `routing.decision` | Router picked a Capability | #10 (Eligible Set not expanded) |
| `grant.issued` | Node Grant was signed | #4 (target binding) |
| `grant.expired` | a Node Grant reached expires_at | #4 |
| `receipt.signed` | a Receipt was signed | #9, #24 |
| `receipt.verified` | a Receipt was re-verified on read | #24 |
| `fallback.triggered` | a Fallback was used | #10 |
| `lease.issued` | a Lease was issued (M2) | #19 (RLS), #21 (fencing) |
| `lease.revoked` | a Lease was revoked | #14 (Kill Switch) |
| `fencing.rejected` | a stale Fencing Token was rejected | #21 |
| `artifact.committed` | a Zone Artifact was committed | #9, #26 (retention) |
| `artifact.deleted` | a Zone Artifact was deleted | #26 |
| `outbox.enqueued` | an Outbox event was enqueued | #9 |
| `outbox.dispatched` | an Outbox event was sent | #9 |
| `reconciler.observed` | Reconciler observed external state | #22 |
| `unknown.reached` | node entered Unknown | #14 (no blind retry) |

## 3. Event payload

```yaml
event:
  event_id:        event:<uuid>
  seq:             <monotonic per task_run_id>
  task_run_id:      <uuid>
  node_run_id:      <uuid> | null   # null for task-level events
  kind:             <EventKind>
  occurred_at:      <iso-utc>
  actor:            <str>           # "orchestra" | "user:<id>" | "adapter:<id>"
  payload:          { ... }          # event-kind-specific
  prev_event_id:    <event_id>      # chain (Merkle pre-image in M2)
```

`prev_event_id` is a **pointer to the previous event in the same
task_run_id**. M2 uses this as the Merkle pre-image; P0 keeps the
pointer for forward compatibility.

## 4. Receipt

A **Receipt** is an Event that has been **signed** by the Receipt
key. M0 supports a COSE-like envelope; M1+ moves to real COSE_Sign1.

```yaml
receipt:
  receipt_id:        <uuid>
  task_run_id:       <uuid>
  node_run_id:       <uuid>
  node_id:           <str>
  envelope:                    # COSE_Sign1-like
    protected:
      alg: HS256                # M0 default; M1+ allows RS256, ES256, EdDSA
      kid: <kid>
      type: node-receipt
    payload:                    # canonical JSON body
      plan_digest:        plan:<sha256[:12]>
      capability_id:      <str>
      manifest_id:        manifest:<sha256[:12]>
      data_view_digest:   <sha256[:12]>
      inputs_digest:      <sha256[:12]>
      outputs_digest:     <sha256[:12]>
      started_at:         <iso-utc>
      ended_at:           <iso-utc>
      status:             succeeded | failed
    signature:        <base64url HMAC>
  created_at:        <iso-utc>
```

The `payload` is a deterministic projection of the node's
inputs, outputs, and the Plan it executed. Two Receipts of the
same node with the same inputs and outputs are byte-identical.

## 5. Verification

A Receipt is verified by:

1. Recomputing the canonical-JSON `payload`.
2. Computing `HMAC(protected || payload, key)`.
3. Comparing to `signature`.
4. Recomputing the `plan_digest` from the Plan and confirming the
   Plan is in the current Manifest store at that `manifest_id`.
5. Confirming the Node Grant for the node has not expired.

P0's `coordinator._verify_receipts` does steps 1-3. M2 adds 4-5
and the Merkle consistency proof.

## 6. The 26-invariants map

| Invariant | Evidence event(s) |
|---|---|
| #1 Restricted→Public blocked | `policy.decision` (deny) + `routing.decision` (no public chosen) |
| #2 Cross-domain via PEP | `io.sent` (only via declared endpoint) + `external.outcome` |
| #3 Planner doesn't escalate | `plan.created` carries the `plan_digest` of the input |
| #4 Target-bound credentials | `grant.issued` (with manifest_id, view, expires_at) |
| #5 Sub-agent permission narrowing | `node.started` carries the grant's effective audience |
| #6 Dynamic nodes re-compile | `plan.amended` |
| #7 High-risk effect needs approval | `node.awaiting-approval` + `node.approved` |
| #8 No default-allow | `policy.decision` (always emits) |
| #9 Cross-domain evidence | every event has `actor` + `prev_event_id` |
| #10 No security path → fail-local | `fallback.triggered` + `node.failed` |
| #11 Published Capability only via Data View | `node.started` (manifest_id) |
| #12 Release Gate | M5: `release.class` + `citation.verified` |
| #13 Tenant isolation | `task.received` carries tenant_id (M5+) |
| #14 Revocation / Kill Switch | `lease.revoked` + `grant.expired` |
| #15 Multi-dim labels | `node.started` payload includes the full SecurityLabel |
| #16 Label propagation | `node.started` payload includes the InformationFlowRule outcome |
| #17 Streams/Errors under Release | `io.sent` + `external.outcome` |
| #18 Cache key includes security context | M5: `cache.cleared` event |
| #19 Data source RLS | `lease.issued` carries the audience + view |
| #20 Delegation as parent intersection | `grant.issued` (kid chain) |
| #21 Adapter can't escape plan | `io.sent` endpoint must be the plan's endpoint |
| #22 Intent/Outcome | `io.intent` + `io.sent` + `external.outcome` pair |
| #23 Break-glass | M5+: `grant.break_glass` event |
| #24 Signature rotation | `receipt.verified` carries the key version |
| #25 Artifact verification | M5: `artifact.committed` carries `manifest_id` |
| #26 Retention/Legal Hold | M5+: `artifact.deleted` event |

Every invariant has at least one event kind that proves it. The M0
test suite (SEC-002) generates **at least one positive test, one
negative test, and one failure test per invariant**.
