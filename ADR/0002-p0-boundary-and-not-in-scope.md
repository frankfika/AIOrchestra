# ADR-0002 — P0 boundary, what is real, and what is `not-in-scope`

- Status: Accepted
- Date: 2026-08-06
- Deciders: Orchestra core team
- Relates to: AGENTS.md §2, `Orchestra_开发计划.md` §0.1.1 and §P0 Gate

## Context

The development plan is explicit that P0 is **not** a production security
product. It must be a real, runnable demo of the *interaction*, not a thin
mock of a production system. The risk is that an implementation agent, under
time pressure, marks "Binding Closure" as "implemented" by writing a 30-line
file with the same name. That violates the plan's Milestone Consistency Matrix
and silently shifts the safety story.

## Decision

The following 6 components are **explicitly `not-in-scope` for P0**. Any
implementation in this repo that *appears* to provide them must be replaced
by a stub that raises `NotInScopeError` (see `orchestra/core/errors.py`) and
displays a clear message. The README, the demo console, and the
`docs/p0_demo_guide.md` must enumerate these.

| Component | P0 disposition |
|---|---|
| Trust Compiler | not-in-scope — plans are validated only by shape, not by info-flow / effect / delegation rules |
| Binding Closure | not-in-scope — the Eligible Set is consumed directly; no abstract-graph → concrete-binding closure is computed |
| Fenced Runtime | not-in-scope — Node Runs are short-lived, no lease / fencing / outbox |
| Enterprise Credential Broker | not-in-scope — Node Grant is a local-signed dev token, not a delegated OAuth chain |
| Schema Projection + Egress PEP | not-in-scope beyond a fixed demo projection in the public-model adapter |
| Merkle Backend | not-in-scope — Receipts are individually signed; no Merkle log, no inclusion proof |

What **is** real and must be real code:
- Fixed Contract Review Task Template
- Static Capability Manifest with declared SecurityLabel and Effect
- Single OPA-style policy (in-process, hot-swappable to real OPA later)
- Eligible Set and deterministic Router that consumes the manifest + policy
- Three Adapters: Local (real extractor), OpenAI-compatible (real protocol),
  A2A Reference (real in-repo agent)
- Minimal Interaction Coordinator with one approval point and one Fallback
- Node Grant (local-signed dev token, bound to task/node/capability/view/purpose/expiry)
- PostgreSQL Event Store with append-only events
- Audit Timeline (REST + WebSocket)
- Basic signed Receipt (COSE-style envelope, per-event signature)
- Benchmark Manifest + 3 baselines (all-local, all-public, hybrid)
- Dify Task Tool reference entry

## Consequences

Positive:
- Reviewers can immediately tell which guarantees the demo makes and which
  are deferred to M1+ — no false confidence.
- The demo focuses on interaction quality (route explanation, node grant
  visibility, audit timeline), which is what P0 needs to validate.

Negative / risk:
- A reader expecting a production product on day 1 will be disappointed.
  This is mitigated by the white paper and dev plan both saying P0 ≠ GA.

## Enforcement

- CI grep: `grep -RIn "BindingClosure\|Merkle\|Trust Compiler" orchestra/ tests/` must
  only match docstrings and the `not-in-scope` markers, not production code.
- The README's "P0 status" table must be regenerated whenever an `not-in-scope`
  marker is added or removed.
