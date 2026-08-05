# SEC-001 — SecurityLabel & Propagation Algebra v0.1

> **Status:** Frozen at M0.
> **Owner:** Agent-2 (Security Model Engineer).
> **Relates to:** Dev plan §0.2 (SEC-001), white paper §6.1 (threat
> model), §6.2 (security invariants), `orchestra.core.schema.SecurityLabel`.

## 1. The label

A `SecurityLabel` is a 5-tuple:

```
L = (c, r, t, ret, owner)
    │  │  │   │     └─ tenant / actor that owns the data
    │  │  │   └────── retention in days
    │  │  └────────── source trust (synthetic | public | partner | internal | restricted)
    │  └───────────── residency (ISO-3166 alpha-2 | "local" | "public")
    └──────────────── classification (public < partner < internal < restricted)
```

The Pydantic model is in `orchestra.core.schema.SecurityLabel`. The
M0 deliverable freezes the order of `c` (most restrictive = highest
number):

```
PUBLIC     = 0
PARTNER    = 1
INTERNAL   = 2
RESTRICTED = 3
```

## 2. Partial order

The label space is a **lattice** with the partial order:

```
L₁ ≤ L₂    iff
  c₁ ≤ c₂
  AND (r₁ == "local" OR r₂ == "local" OR r₁ == r₂)
  AND t₁ ≤ t₂
  AND ret₁ ≤ ret₂
  AND owner₁ == owner₂
```

Read: data labelled L₁ may flow to a context labelled L₂ iff
the classification, residency, trust, retention, and owner are
**compatible** (the destination is at least as sensitive as the
data).

## 3. Join (most restrictive)

For a node with inputs I₁, …, Iₖ:

```
L_join = (max(c₁, …, cₖ),
          ∩ residency,
          min(t₁, …, tₖ),
          min(ret₁, …, retₖ),
          owner if all equal else "<mixed>")
```

`∩ residency` is the intersection; if the inputs have
incompatible residencies (e.g. `cn` and `us`), the join's
residency is `local` (the only common lower bound).

## 4. Meet (least restrictive)

```
L_meet = (min(c₁, …, cₖ),
          ∪ residency,
          max(t₁, …, tₖ),
          max(ret₁, …, retₖ),
          owner if all equal else "<mixed>")
```

`∪ residency` is the union: if any input is `local`, the meet is
`local`; otherwise the meet is the union of specific residencies.

## 5. Trust as a flow gate

The `source_trust` field is **not propagated upward** in the same
way as classification. A model output that was generated from
`restricted` data **inherits** the `restricted` classification and
`local` residency; its `source_trust` becomes `restricted` (the
most conservative) — this is **invariant #16**.

```
derived_trust(L_join) =
  RESTRICTED  if any input is restricted
  INTERNAL    if any input is internal
  PARTNER     if any input is partner
  PUBLIC      if all inputs are public
  SYNTHETIC   if no inputs (cold start)
```

## 6. Information-flow property

For every node N, the Trust Compiler (M1) must prove:

```
classification(L(N.output)) ≥ classification(L(any input))
                                          │
                                          └─ in the same trust domain
```

That is the formal statement of invariant #1 (Restricted→Public
blocked) and #16 (Restricted model output inherits Restricted).
The M1 deliverable CMP-002 implements the prover.

## 7. Default deny

When the join is `None` (e.g. contradictory residencies), the
default is **deny** (invariant #8). The PDP surfaces the deny
reason and the invariant tag.

## 8. Properties to test

The M0 acceptance suite must prove:

1. **Monotonicity:** for any two labels L₁ ≤ L₂, JOIN(L₁, L₂) = L₂.
2. **Idempotence:** JOIN(L, L) = L; MEET(L, L) = L.
3. **Commutativity:** JOIN(L₁, L₂) = JOIN(L₂, L₁).
4. **Default-deny on empty:** JOIN(∅) = undefined; PDP returns deny.
5. **Trust inheritance:** if any input is `restricted`, output
   is `restricted` regardless of join.

These are the 5 canonical SecurityLabel tests in
`tests/m0/test_security_label_algebra.py`.
