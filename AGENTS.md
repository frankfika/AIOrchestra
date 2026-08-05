# AGENTS.md — Project Memory for AI Agents

This file is consumed by AI coding agents (Mavis, Codex, Cursor, Aider, Gemini CLI, …)
working inside the Orchestra monorepo. It encodes the **project-level invariants** that
must be respected on every change. Higher-priority rules (orchestra/白皮书, /开发计划) win
on conflict; this file is the bridge between those documents and the agent's daily
decisions.

## 1. What this project is

**Orchestra** is a Hybrid / Sovereign AI Orchestration Plane. It is a *control plane*
between applications (Dify, Coze, AgenticHub, custom UIs) and execution resources
(local models, public models, A2A agents, MCP tools, human approvers). The full product
definition lives in `Orchestra_Hybrid_Sovereign_AI_Orchestration_Plane_产品白皮书.md`;
engineering schedule, milestones, and gates live in `Orchestra_开发计划.md`.

When those two documents disagree on **product principle or safety boundary**, the
white paper wins. When they disagree on **schedule, feature status, or acceptance
detail**, the development plan wins. Disagreements must be captured as an ADR
(`ADR/NNNN-*.md`); agents may not silently resolve them.

## 2. Milestone discipline (CRITICAL)

This repository currently targets **P0 — Category Proof**. P0 is a real, runnable demo
proving the product is worth building — **not** a production system. The Milestone
Consistency Matrix in `Orchestra_开发计划.md` §0.1.1 is the binding contract.

| P0 must implement (real code) | P0 explicitly NOT in scope (mark `not-in-scope`, do not fake) |
|---|---|
| Fixed Contract Review Task Template | Trust Compiler |
| Static Capability Manifest | Binding Closure |
| Single OPA-style Policy | Fenced Runtime |
| Eligible Set + deterministic Router | Enterprise Credential Broker |
| Local / OpenAI-compatible / in-repo A2A Adapters | Schema Projection + Egress PEP (beyond fixed demo) |
| Minimal Coordinator | Merkle Backend |
| Node Grant (local-signed dev credential) | Real cross-tenant / multi-region / zero-leak guarantees |
| PostgreSQL Event Store | |
| Audit Timeline | |
| Basic signed Receipt | |
| Benchmark Manifest + 3 baselines + Dify Task Tool entry | |

**Forbidden shortcuts in P0:**
- Calling the demo "production secure"
- Implementing `BindingClosure` under a different name to make the matrix look green
- Skipping an audit event because "the demo doesn't need it"
- Using a public model with the full contract text in any baseline

P0 is allowed: sequential nodes, limited fan-out, one pre-approved Fallback, one
approval point, and writing only to a Mock Procurement Sink.

## 3. Schemas and naming

The frozen P0 vocabulary is in `orchestra/core/schema.py`. Do not introduce
parallel types with the same name. Names like `SecurityLabel`, `Node Grant`, `Lease`,
`Receipt`, `Data View`, `Plan Amendment`, `Capability Manifest` are reserved and
must match the white paper's semantics. The full M0 freeze list is in
`Orchestra_开发计划.md` §0.1.2 — P0 implements the subset actually used.

## 4. Test discipline

- Every Feature has at least one positive test, one negative test, and one failure
  test, mapped to the 26-invariants matrix when relevant.
- Tests must be runnable in a clean environment (see `docs/p0_demo_guide.md`).
- An e2e test that depends on PostgreSQL is marked `@pytest.mark.e2e` and skipped
  when the DB is unavailable; smoke tests must not require PG.
- A passing test is **not** proof a feature is done — see §0.6 of the dev plan.

## 5. What agents must NOT do

- **Do not** claim an invariant is enforced when the implementation is a stub.
  Add `not-in-scope` to the README / docs and link the invariant number.
- **Do not** add adapters, capabilities, or storage backends that are not in the
  P0 matrix without an ADR.
- **Do not** commit secrets, real keys, or PII. The demo runs on synthetic / public
  / approved data only (see `Orchestra_开发计划.md` §P0 Gate).
- **Do not** push directly to `main` without a review. PR titles must include the
  Feature ID (e.g. `[LIT-003] …`).

## 6. Quick orientation

- Source: `orchestra/`
- Tests: `tests/`
- Sample data: `data/samples/`
- Decision records: `ADR/`
- Demo guide: `docs/p0_demo_guide.md`
- White paper: `Orchestra_Hybrid_Sovereign_AI_Orchestration_Plane_产品白皮书.md`
- Dev plan: `Orchestra_开发计划.md`
