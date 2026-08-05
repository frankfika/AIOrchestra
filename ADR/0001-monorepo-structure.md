# ADR-0001 — Monorepo structure for Orchestra P0

- Status: Accepted
- Date: 2026-08-06
- Deciders: Orchestra core team
- Relates to: FND-001, AGENTS.md §1–2

## Context

P0 needs a single, runnable artefact that proves the product is worth building. The
plan (`Orchestra_开发计划.md` §0.4 P0a–P0c) defines three delivery batches: repo
scaffold + template, registry/router/adapters/coordinator, benchmark + demo. We
need a layout that makes those three batches land in clearly separate directories
so parallel agents do not collide, and so a reviewer can audit which batch is
responsible for which file.

## Decision

Use a single Python monorepo (no Java/Go/TS split for P0) with this layout:

```
orchestra/
  core/         Pydantic schemas, ids, hashing, time
  registry/     Capability Manifest, policy, eligible set, router
  coordinator/  Node Grant, event store (PG), receipt, engine
  adapters/     Local / OpenAI-compatible / A2A reference
  templates/    Fixed Contract Review Task Template
  api/          FastAPI HTTP + WebSocket surface
  benchmarks/   3 baselines + manifest
  dify/         Dify Task Tool reference entry
tests/          pytest, mirrors orchestra/
ADR/            decision records
data/samples/   synthetic contract corpus
docs/           p0 demo guide, api reference
```

Why Python: the P0 schema, router, and coordinator are small (≲ 1k LOC) and
benefit from pydantic + async I/O. M1+ may split out a Go/Java compiler; that is
a future ADR.

Why no `services/` split: every component in P0 is a module inside one
process. The Event Store is the only network dependency, and it is PostgreSQL.

## Consequences

Positive:
- One `pip install -e .` makes the demo runnable.
- Tests run in one process; CI is one matrix entry.
- Schema changes (SPEC-001) are visible across all components in one PR.

Negative / risk:
- Python's GIL limits parallel compute, but P0 is I/O bound (HTTP adapters + PG).
- A future split to polyglot will need migration ADRs.

## Alternatives considered

- Multi-repo: rejected — too much cross-repo ceremony for a P0 demo.
- Polyglot from day one (Go compiler + Python coordinator): rejected — YAGNI
  for P0; revisit after M1 Gate.
