# SEC-002 — 26 Security Invariants & Threat Model v0.1

> **Status:** Frozen at M0.
> **Owner:** Agent-2 (Security Model Engineer).
> **Relates to:** Dev plan §0.8 (26-invariants matrix), white paper §6.

## 1. The 26 invariants (formal)

Each invariant is a **proposition** the Trust Compiler + Event
Store + UI must demonstrate holds for every Plan and every
execution. M0 freezes the wording; M1+ implements the checker.

| # | Invariant | M0 responsibility |
|---:|---|---|
| 1 | Restricted / Zero-Egress data must not reach a Public Sink. | PDP rule `no-restricted-to-public`; Router denies; Coordinator refuses to call. |
| 2 | Cross-trust-domain calls must pass the PEP. | Adapter enforces endpoint whitelist; Egress PEP checks `manifest_id`. |
| 3 | Planner / Agent / Tool must not escalate labels, policy, or authority. | Trust Compiler (M1) compares Plan's effective authority against input. |
| 4 | Credentials are target-bound, least-privilege, short-lived. | Node Grant binds task/node/capability/view/purpose/expiry. |
| 5 | Sub-Agent authority is the intersection of its parent's. | Delegation token chain (M2) verifies audience ≤ parent's audience. |
| 6 | Dynamic nodes / edges must re-compile and re-authorize. | Plan Amendment event; Trust Compiler re-runs on every amendment. |
| 7 | High-risk side-effects need segregation-of-duties + approval. | Template's `requires_approval` flag on every WRITE/DELETE/PAYMENT/PUBLISH. |
| 8 | No default-allow: strategy / identity / proof failures → deny. | PolicyEngine default-deny on empty bundle. |
| 9 | Cross-domain, delegation, authorization, denial, and side-effects all leave evidence. | Every event has actor + prev_event_id; signed Receipt. |
| 10 | No secure path → fail-local, request human, or fail. | Router's pre-approved Fallback; Coordinator surfaces `fallback.triggered`. |
| 11 | Published Capability may only use explicit Data View + Tool Profile. | Adapter contract's `enforce` integration level (P0); M5+ adds `data_views` allowlist. |
| 12 | External Content / Artifact / Citation must pass the Release Gate. | CitationManifest (M5) carries `release_class`; REL-001 enforces. |
| 13 | External input is untrusted; tenant context isolated. | Tenant boundary in Node Grant (M5+); Planner input is treated as UNTRUSTED. |
| 14 | Revocation / Kill Switch / Policy updates have bounded effect. | Authority Epoch (M1) + Lease revocation (M2). |
| 15 | Security decisions use multi-dimensional trusted labels. | SecurityLabel carries all 5 dimensions; PDP consumes the full tuple. |
| 16 | Restricted model output inherits Restricted. | InformationFlowRule's `derived_trust` (SEC-001 §5). |
| 17 | Streams, errors, metrology, links, Receipts are under Release Policy. | Egress PEP applies to error and trace events. |
| 18 | External cache key includes the full security context. | Cache key = hash(tenant_id, capability_id, manifest_id, view, purpose, region, effect). |
| 19 | Downstream data sources independently enforce RLS / ABAC. | Connector contract (M5) declares the source's own access control. |
| 20 | Delegation authority is the intersection of parent, contract, policy, capability. | Binding Closure (M1) computes the intersection. |
| 21 | A compromised Adapter / Runtime cannot access plan-out-of-band resources. | Egress PEP + Node Grant scope; Artifact Manager boundary. |
| 22 | High-risk side-effects have Intent / Outcome reconciliation. | `io.intent` + `io.sent` + `external.outcome` paired events. |
| 23 | Break-glass must not downgrade labels or bypass Zero-Egress. | Policy bundle's `break_glass` rule (M5+) is dual-control. |
| 24 | Signed objects support rotation / revocation / migration / key-compromise recovery. | manifest_id pinning; Receipts carry `kid`; verification checks the current kid. |
| 25 | Production artifacts / Policy / Adapter / Manifest / Card are verified on install. | SBOM + signature check on `pip install` / image pull. |
| 26 | Delete / retain / Legal Hold cover every copy. | Artifact Manager + cache + backup cross-check; Legal Hold event. |

## 2. Threat model (STRIDE)

| Threat | Counter-measure (M0) |
|---|---|
| **S**poofing of a Capability (a malicious Adapter claims to be `public.openai-compat`) | manifest_id + signed Receipt; `kid` chain. |
| **T**ampering of the Plan after the Trust Compiler signed it | Plan Signature is content-addressed; Plan Amendment re-signs. |
| **R**epudiation of a human approval | `node.approved` event carries `decided_by` + `rationale`; Approval record persisted. |
| **I**nformation disclosure via a side-channel Adapter | Egress PEP (`manifest_id`, view, field allowlist). |
| **D**enial of service via long-running Adapters | Node-level `timeout_ms`; Unknown state; Fencing Token (M2). |
| **E**levation of privilege via Planner escalation | Trust Compiler CMP-002 checks the join rule (SEC-001 §6). |

## 3. Counter-example corpus (M0 deliverable)

`tests/m0/test_26_invariants.py` is the **counter-example corpus**
the Security Model Engineer ships. For each invariant I it contains:

- `test_i_positive_*`: a Plan that satisfies I.
- `test_i_negative_*`: a Plan that violates I; the Trust
  Compiler must report a deny with the invariant number and a
  human-readable reason.
- `test_i_failure_*`: an environment that prevents the check
  (e.g. the PDP is down); the system must fail-closed.

P0 already covers invariants #1, #3, #7, #8, #10, #15, #20, #22
with positive tests; the M0 corpus extends coverage to all 26.

## 4. Test report format

`tests/m0/test_26_invariants.py` outputs a JSON test report with
one entry per invariant:

```json
{
  "invariant": 1,
  "name": "restricted-never-reaches-public",
  "positive": [{"test": "...", "result": "pass"}],
  "negative": [{"test": "...", "result": "pass"}],
  "failure":  [{"test": "...", "result": "pass"}]
}
```

The CI gate fails the M0 build if any invariant has fewer than
one positive + one negative + one failure passing.

## 5. M0 acceptance

- All 26 invariants have at least one passing positive, one
  passing negative, and one passing failure test.
- The threat model table is reviewed and signed off.
- The Trust Compiler (M1) is the runtime enforcement; M0 only
  has the declarative spec and the failing-Python positive/negative
  tests as the *test corpus* M1 must satisfy.
