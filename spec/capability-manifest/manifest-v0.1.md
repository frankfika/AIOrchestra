# SPEC-002 — Capability Manifest v0.1

> **Status:** Frozen at M0.
> **Owner:** Agent-1 (Spec Architect).
> **Relates to:** Dev plan §0.2 (SPEC-002), ADR-0002 (P0 boundary),
> white paper §3.2 (Capability), §3.4 (Information flow control).

## 1. What a Capability Manifest is

A Capability Manifest is the **declaration** a publisher (an in-repo
Adapter, a public OpenAI-compatible endpoint, an external A2A Agent,
a human approver, a sink) writes into Orchestra's static registry.
The Trust Compiler reads the Manifest to know what the Capability
will accept, what it will produce, what side effects it may have,
and how to verify its identity.

The Manifest is **content-addressed**: the `manifest_id` is the
SHA-256 of the canonical-JSON body, truncated to 12 hex chars, with a
`manifest:` prefix. Pinning a Plan to a `manifest_id` makes the
Plan immune to silent Manifest updates.

## 2. Schema (informal; see orchestra.core.schema.CapabilityManifest)

```yaml
capability_manifest:
  capability_id:   <str>           # publisher-chosen, unique
  name:            <str>
  kind:            local-model | public-model | a2a-agent
                   | tool | human | sink
  version:         0.1.0          # semver; P0 is "0.1.0" forever
  description:     <str>

  endpoint:        <str>           # how the Coordinator reaches it
                                    # (URL, in-process symbol, or queue)
  integration_level: enforce | recommend | observe

  # M0 additions (subset of full spec):
  runtime_requirements:
    region:        local
    gpu:           0
    memory_mb:     512
    timeout_ms:    30000
    network:       private-only | public-egress-allowed
    tier:          P0

  accepts_labels:  [SecurityLabel, ...]
  produces_labels: [SecurityLabel, ...]

  declared_effects:  [Effect, ...]
  # declare every Effect the Adapter may produce, even if the
  # calling node doesn't ask for it. The Trust Compiler uses this
  # to compute the "least upper bound" of effects.

  declared_inputs:   [FieldManifest, ...]
  declared_outputs:  [FieldManifest, ...]

  # M5: published capabilities add these (out of scope for P0)
  audience:          internal | partner | public
  agent_card:        { ... }    # for A2A Agents
  data_views:        [DataView, ...]

  protocol:
    name:            openai-chat | a2a-jsonrpc | mcp | http-json
    version:         2024-08-01

  identity:          { kid: <kid>, alg: HS256 | RS256 | ES256 | EdDSA }
  observability:     { otel: true, log_pii: false }

  supports_idempotency:    bool
  supports_cancel:         bool
  supports_status_query:   bool
  cost_estimate_usd:       float
  p50_latency_ms:          int
  p95_latency_ms:          int

  tags: { <key>: <value>, ... }
```

The Pydantic model in `orchestra.core.schema.CapabilityManifest` is
the **M0 frozen shape** — a strict subset of the above. M1+ extends
it with the optional fields marked "M0 additions" / "M5".

## 3. Integration level

```yaml
integration_level: enforce   # default for P0
```

| Level | Meaning |
|---|---|
| `enforce` | The Coordinator treats this Manifest as the source of truth. Adapter behaviour must match the declared fields. |
| `recommend` | The Coordinator shows the Manifest as advice to the human approver; the human can override. (Not used in P0.) |
| `observe` | The Manifest is metadata only. The Adapter may behave differently from its declaration. The audit timeline records the divergence. (Not used in P0.) |

The P0 demo uses `enforce` everywhere. The M3 UX-001 surface
exposes the level so a human auditor can see which Capabilities are
"hard" vs "soft".

## 4. Identity

Every Manifest declares the `kid` (key id) and `alg` (signature
algorithm) the Coordinator uses to verify the Capability's
identity. P0 uses a single tenant key (`HS256`); M1+ plugs in real
SPIFFE / SVIDs and asymmetric algorithms.

The M2 EVD-001 (Merkle backend) verifies that the `manifest_id`
in every Plan matches the Manifest body the Publisher registered.
A mismatch is **invariant #24** (signed objects support rotation /
revocation / migration / key-compromise recovery).

## 5. Manifest versioning and snapshot

The Trust Compiler pins every Plan to a `manifest_id`. When the
Publisher rotates a Manifest (e.g. new `runtime_requirements`):

- A new `manifest_id` is computed.
- New Plans bind the new `manifest_id`; in-flight Plans keep the old
  one.
- The M2 Reconciler retires in-flight Plans whose `manifest_id` is
  older than the rolling window.

The Capability Manifest store (M3) keeps every snapshot, not just
the latest. P0 keeps the latest only.

## 6. M0 acceptance

- `tests/m0/test_manifest.py` round-trips every Manifest through
  Pydantic + JSON Schema.
- `tests/m0/test_manifest_snapshot.py` confirms the content-
  addressed `manifest_id` is stable across process restarts.
- `tests/m0/test_manifest_invariants.py` confirms invariant #24
  (signature rotation) and #25 (artifact verification) hold for a
  Manifest rotated while a Plan is in flight.
