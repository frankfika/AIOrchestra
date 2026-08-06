# ADR-0004 — Trust Compiler runs in-process (Python), not as a separate OPA service

- Status: Accepted
- Date: 2026-08-06
- Deciders: Orchestra core team
- Relates to: M1, SPEC §0.4 M1, AGENTS.md §2

## Context

The Trust Compiler (M1) verifies every Plan before the Coordinator runs it:
the parser / normalizer / type-checker / info-flow checker / effect-checker /
delegation-checker must all agree the Plan is sound. The original white paper
left the implementation language open. Two paths were credible:

  * **In-process Python** — compile the plan inside the Coordinator process,
    ship the same bytecode that's already in `orchestra.compiler.*`.
  * **External OPA / Rego service** — every Coordinator run would do a
    cross-process round-trip to a Rego policy engine.

## Decision

The Trust Compiler is in-process Python. Every `Coordinator.run()` call
synchronously runs the chain in the same process; the compiled plan is the
input to `_exec_node`. There is no OPA dependency.

## Consequences

  * **+** Zero operational overhead. No second service to deploy, monitor, or
    version. A 5-milestone project (M0–M7) can run on one machine.
  * **+** Test surface is a single Python process — the `tests/m1/` suite runs
    in < 1 second.
  * **+** Custom checkers (the M0 freeze's "Trust Compiler" isn't a single
    algorithm — it's a chain of four) are easy to express in Python; Rego
    would have meant learning Datalog.
  * **−** The dev path can't enforce policy across multiple Coordinator
    instances. A production swap to OPA over HTTP is documented in
    `orchestra/opa/` (the interface is already extracted); see ADR-0002.
  * **−** A policy change requires a Coordinator redeploy. A future
    regulatory pilot will need the OPA swap to make policy changes hot-load.

## Alternatives considered

  * **OPA / Rego via HTTP** — the production-ready path. Rejected for P0
    because the OPA binary is a separate process with its own
    versioning/rollout, and the dev path doesn't need that. The
    `orchestra.opa.interface.OPABackend` interface is the seam.
  * **Compile to WASM** — let a partner run the Trust Compiler in their own
    browser. Rejected because the white paper's audience is the dev-path
    SRE, not the partner dev. M5 Agent Card + M16 SDK is the partner-side
    path; the Trust Compiler is server-side.
