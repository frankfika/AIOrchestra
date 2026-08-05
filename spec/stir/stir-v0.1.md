# SPEC-001 — STIR (Sovereign Task Intermediate Representation) v0.1

> **Status:** Frozen at M0.
> **Owner:** Agent-1 (Spec Architect).
> **Relates to:** White paper §3 (product definition), Dev plan §0.2 (SPEC-001),
> ADR-0002 (P0 boundary).

## 1. What STIR is

STIR is the **typed intermediate representation** Orchestra compiles
every Task into before it can be Resolved, Bound, or Executed. It is
*not* a general-purpose IR for any planner — it is the smallest
representation that lets the Trust Compiler (M1) answer three
questions deterministically:

1. **What data does each node read, and what label does it have?**
2. **What effects does each node declare, and what approval is
   required?**
3. **Which Capabilities are candidates for which nodes, and what
   is the binding closure?**

STIR is the contract between the **upper layer** (Dify, AgenticHub,
custom UIs, the in-repo `TaskTemplate`) and the **lower layer**
(Router, Resolver, Coordinator). The frozen Pydantic models in
`orchestra.core.schema` are the canonical implementation; this
document is the human-readable spec.

## 2. Top-level shape

```yaml
# STIR Plan (informal; see orchestra.core.schema.ExecutionPlan for the
# canonical Pydantic model)
plan:
  plan_id:        plan:<sha256[:12]>
  contract_id:    <uuid>
  template_id:    contract-review          # or any other frozen template id
  template_version: 0.1.0
  nodes:          [ PlanNode, ... ]
  edges:          [ PlanEdge, ... ]
  routing:        [ RoutingDecision, ... ]
  field_manifests: [ FieldManifest, ... ]   # M3 input (XFR-001)
  citation_manifests: [ CitationManifest, ... ]   # M5 input (REL-001)
  created_at:     <iso-utc>
  signed_by:      <kid>
  signature:      <cose-sig>
```

The Plan is **content-addressed** (its `plan_id` is a function of
its body), signed by the Plan Signer (M1 BND-001), and pinned into
every downstream Receipt.

## 3. Node

A `PlanNode` is the resolved form of a `NodeSpec` from the
`TaskTemplate`. The PlanNode is what the Router binds a Capability to,
and what the Coordinator executes.

| Field | Type | Notes |
|---|---|---|
| `node_id` | str | Stable; must match a NodeSpec in the template. |
| `capability_id` | str | The Capability the Router chose. |
| `manifest_id` | str | Content-addressed ID of the bound Manifest snapshot. |
| `purpose` | Purpose | Cannot be changed by downstream nodes (invariant #5, #20). |
| `input_views` | list[DataView] | The data this node is allowed to read. |
| `input_refs` | list[ValueRef] | M0: typed handles to upstream outputs (STIR addition). |
| `expected_outputs` | list[str] | Logical names; mapped to DataView fields. |
| `output_label_rule` | InformationFlowRule | M0: how to compute the output label. |
| `requirements` | list[Requirement] | Non-functional constraints (region, GPU, …). |
| `timeout_ms` | int | Hard timeout; exceeding → Unknown. |
| `fallback_capability_id` | str \| null | One pre-approved Fallback. |
| `requires_approval` | bool | True for any node declaring WRITE/DELETE/PAYMENT/PUBLISH (invariant #7). |
| `status` | enum | pending / running / succeeded / failed / awaiting-approval. |

## 4. Edge

A `PlanEdge` is a directed "this node's output feeds that node's
input" link. Edges are **resolved at Plan time** from the template;
the Trust Compiler checks that the dataflow does not violate
information-flow rules.

| Field | Type | Notes |
|---|---|---|
| `from_node` | str | Upstream node id. |
| `to_node` | str | Downstream node id. |
| `when` | str | Predicate name; the canonical predicate is "always". |

## 5. ValueRef

A `ValueRef` is the **typed pointer** the Coordinator uses to fetch an
upstream node's output. It is part of the Plan (statically resolvable
by the Trust Compiler) and part of the `AdapterRequest.inputs` at
runtime.

```yaml
value_ref:
  ref_id:        ref:<uuid>
  producer_node_id: extract_facts_local
  producer_output:  facts
  view_name:      facts.internal
  type_hint:      json
  label:          { classification: internal, residency: local }
```

The runtime value does NOT travel in the Plan — it lives in the Zone
Artifact Store (M0 §0.2 Agent-3 deliverable). The Adapter fetches it
under the authority of the Node Grant.

## 6. Effect

An `Effect` declares a side-effect category a node may produce. The
M1 Trust Compiler enforces that any node declaring
`WRITE`/`DELETE`/`PAYMENT`/`PUBLISH` is preceded by an approval node
(an edge whose `to_node` is a node with `requires_approval=True`).

| `kind` | Meaning | Approval required? |
|---|---|---|
| `read` | Side-effect-free observation. | no |
| `write` | Mutation of an internal target. | **yes** |
| `delete` | Destructive mutation. | **yes** |
| `payment` | Any monetary movement. | **yes** |
| `publish` | Output that leaves the tenant to an external audience. | **yes** |
| `notify` | User-visible notification. | no |

`Effect.target` carries an optional logical name (e.g. `mock-procurement`)
so the audit timeline can show "node `write_sink` declared WRITE on
`target=mock-procurement`".

## 7. Requirement

A `Requirement` is a typed key/value/op tuple attached to a node. The
Trust Compiler checks `node.requirements` against
`manifest.runtime_requirements` and against the current environment.

| `kind` | Example | Notes |
|---|---|---|
| `region` | `{"op":"in","value":["local","us-east"]}` | residency constraint. |
| `gpu` | `{"op":"ge","value":1}` | accelerator count. |
| `memory-mb` | `{"op":"ge","value":4096}` | memory floor. |
| `timeout-ms` | `{"op":"le","value":10000}` | hard timeout. |
| `network` | `{"op":"eq","value":"public-egress-allowed"}` | Egress PEP gate. |
| `tool` | `{"op":"in","value":["mcp:web-search","mcp:sql"]}` | tool availability. |
| `language` | `{"op":"eq","value":"python3.12"}` | runtime language. |
| `tier` | `{"op":"in","value":["P0","M1"]}` | capability tier. |

## 8. InformationFlowRule

Each node declares how it computes the label of its outputs from
the labels of its inputs. The default is `JOIN` (the most
restrictive label wins). The M1 Trust Compiler uses these rules to
prove invariant #1 (Restricted never reaches a Public sink) and
#16 (Restricted model output inherits Restricted) statically.

```yaml
information_flow_rule:
  rule_id:        ifr:<uuid>
  join:           join                # join | meet | passthrough | explicit
  input_refs:     []                  # empty = all inputs
  output_name:    facts               # optional: limit to one output
  explicit_output_label:             # required when join=explicit
    classification: internal
    residency:      local
```

## 9. Information flow: formal propagation

For a node N with inputs I = {I₁, …, Iₖ} each carrying a label
L(Iⱼ) = (cⱼ, rⱼ, tⱼ, retⱼ, ownerⱼ), the output label L(O) of an
output O is computed as:

* **JOIN (default):** L(O) = ⊓ⱼ L(Iⱼ). The classification is the
  maximum (most restrictive); the residency is the intersection;
  the retention is the minimum; the source trust is the minimum.
* **MEET:** L(O) = ⊔ⱼ L(Iⱼ). The classification is the minimum
  (most permissive); used for redaction nodes that strip labels.
* **PASSTHROUGH:** L(O) = L(I_first_nonempty). Used by routing
  nodes that don't transform the payload.
* **EXPLICIT:** L(O) is the rule's `explicit_output_label` (used
  when the node's contract says "regardless of inputs, my output
  is X").

The Trust Compiler checks (M1 CMP-002) that for every PlanNode:

```
classification(L(O)) ≥ classification(L(any input from same trust domain))
```

i.e. an output must be at least as classified as the most
restrictive input.

## 10. DataView (from P0, frozen)

The `DataView` is the named projection a node reads. P0 already
ships two shapes (`reference`, `fields`); M0 adds the constraint
that every `DataView` referenced by a node's `input_views` must
be the same view the producer node's `expected_outputs` declared.

The Trust Compiler (M1) refuses a Plan that violates this constraint.

## 11. Versioning

The frozen STIR version is `0.1.0`. Breaking changes require:

1. A new top-level `template_version`.
2. An entry in `spec/stir/CHANGELOG.md` with a migration recipe.
3. A new `golden_cases/*.json` test that fails on `0.1.0` and passes
   on the new version.

## 12. Where the canonical implementation lives

The Python Pydantic models in
`orchestra.core.schema.{TaskTemplate, NodeSpec, EdgeSpec, PlanNode,
PlanEdge, ExecutionPlan, ValueRef, Requirement, InformationFlowRule}`
**are** STIR. JSON-Schema exports are produced by
`orchestra.core.schema.export_json_schemas` and dumped into
`spec/stir/json-schema/` by the M0 CI gate (see `tests/test_m0_spec.py`).

## 13. M0 acceptance

A PR that modifies STIR is accepted only if:

- All 25 existing tests still pass.
- `tests/m0/test_stir.py` round-trips every type through JSON Schema.
- `tests/m0/test_stir_invariants.py` confirms the formal join rule
  for 4 representative inputs.
- The CHANGELOG entry is present.
