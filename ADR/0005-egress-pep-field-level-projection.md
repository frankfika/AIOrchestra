# ADR-0005 — Egress PEP projects fields, not whole payloads

- Status: Accepted
- Date: 2026-08-06
- Deciders: Orchestra core team
- Relates to: M3 XFR-001, SPEC §0.4 M3, AGENTS.md §2

## Context

When a public-capability Adapter (OpenAI / Anthropic / A2A) is invoked, the
Coordinator has to decide what to send. Two paths:

  * **Whole-payload redaction** — send the full input to the Adapter, then
    scrub the response before the audit log records it. PII / secrets
    have already left the tenant.
  * **Field-level projection** — the `FieldManifest` is the source of
    truth for which fields may leave the tenant; the Egress PEP replaces
    the Adapter's input with the projected payload. The audit timeline
    records an `io.sent` event with the projected *digest*, never the
    raw payload.

## Decision

The dev path projects fields before they leave the tenant
(`orchestra.xfr.egress_pep.EgressPEP.project`). The FieldManifest is the
binding contract; the PEP refuses a call that doesn't have a manifest, refuses
a payload that exceeds the byte budget, and writes only the digest of what
left to the audit timeline.

## Consequences

  * **+** PII / secrets never leave the tenant boundary. The audit
    timeline proves *exactly* what was sent, but the timeline itself
    doesn't contain the raw payload.
  * **+** The M1 Trust Compiler can verify the FieldManifest at Plan time
    ("this Adapter is allowed to read these fields"); the PEP just
    enforces it at call time. Two layers, one contract.
  * **+** The M1 info-flow checker proves the data flow is sound:
    "this field is read from this Manifest's `allowed_fields` and never
    copied to a field the Manifest doesn't list."
  * **−** Adapters that expect a wide contract (e.g. an LLM that wants
    the full prompt history) need a Manifest that lists every field
    they read. This is a partner onboarding cost; the
    `make_egress_manifest_lookup` helper in `orchestra/registry/bootstrap.py`
    ships with a default Manifest per reference Adapter.
  * **−** A partner who wants a different field shape must ship a new
    Manifest version. The `version` field on the Manifest makes the
    change auditable.

## Alternatives considered

  * **Tokenisation at the boundary** — replace PII with reversible tokens
    in the egress payload, de-tokenise on the way back. Rejected
    because the dev path doesn't have a tokenisation store, and
    a partner's compliance team is happier with a deny-by-default
    FieldManifest than with a tokenisation pipeline that has to be
    kept in sync with the field schema.
  * **Confidential-computing envelope** — wrap the Adapter in a TEE
    that proves the bytes never left. Rejected for the dev path because
    it requires partner-side infrastructure the pilot doesn't have.
