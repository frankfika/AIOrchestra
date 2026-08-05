"""M12 — Property-based tests for publish / release gate.

These tests do not use ``hypothesis`` (would add a dep). They
hand-roll a small fuzzer over the input space and pin the
invariants the surface must hold. The properties are:

  1. The Release Gate NEVER accepts a result whose manifest
     contains a restricted citation. This holds for every
     combination of audience, source label, and claim.
  2. The Release Gate NEVER accepts a result whose manifest
     audience is not in the Card's audience set. This holds
     for every combination of audience values.
  3. The Projector NEVER produces a payload with bytes > byte_budget.
  4. The Projector NEVER includes a field not in allowed_fields.
  5. The Ingress NEVER admits a token whose audience is not in
     the Card's audiences. This holds for any token / Card pair.
"""
from __future__ import annotations

import random
import string

import pytest

from orchestra.core.hashing import hmac_keygen
from orchestra.core.schema import (
    Citation,
    CitationManifest,
    CitationSourceRef,
    DataClassification,
    SecurityLabel,
    SourceTrust,
)
from orchestra.publishing.card import AgentCard
from orchestra.publishing.ingress import Ingress, IngressDenied
from orchestra.publishing.registry import PublishedRegistry
from orchestra.publishing.release_gate import ReleaseGate, ReleaseDenied
from orchestra.xfr.projector import EgressBudgetExceeded, FieldProjector


# ---------------------------------------------------------------------------
# Property 1: Release Gate never accepts restricted citations
# ---------------------------------------------------------------------------


def _rand_label(cls: DataClassification) -> SecurityLabel:
    return SecurityLabel(
        classification=cls,
        residency="local" if cls == DataClassification.RESTRICTED else "public",
        source_trust=SourceTrust.INTERNAL if cls == DataClassification.RESTRICTED else SourceTrust.PUBLIC,
    )


def test_property_release_gate_never_accepts_restricted_citation():
    """For any random combination of audience + classification
    pair, the gate refuses a restricted citation. 50 random
    cases."""
    rng = random.Random(0xC0FFEE)
    # Citation.audience is Literal["public","partner","internal","restricted"]
    audiences = ["public", "partner", "internal", "restricted"]
    for _ in range(50):
        aud = rng.choice(audiences)
        card = AgentCard(
            capability_id="prop", name="Prop", version="0.1.0",
            partner_id="p", partner_contract_id="c",
            audiences=["public", "partner", "internal"],
        )
        manifest = CitationManifest(
            task_run_id="trun-prop",
            citations=[Citation(
                claim="x",
                sources=[CitationSourceRef(
                    kind="node-output", ref="n",
                    label=_rand_label(DataClassification.RESTRICTED),
                )],
                audience=aud,
                release_class="attested",
            )],
        )
        gate = ReleaseGate(card=card)
        with pytest.raises(ReleaseDenied):
            gate.release({"claims": ["x"]}, manifest)


def test_property_release_gate_never_accepts_audience_outside_card():
    """For any audience not in the Card, the gate refuses.
    50 random cases."""
    rng = random.Random(0xBEEF)
    card_audiences = ["partner", "internal", "public"]
    other_audiences = ["restricted"]  # Citation.audience Literal
    for _ in range(50):
        card = AgentCard(
            capability_id="prop", name="Prop", version="0.1.0",
            partner_id="p", partner_contract_id="c",
            audiences=card_audiences,
        )
        # Pick the "other" audience (only `restricted` is outside
        # the Card's audience set here).
        manifest = CitationManifest(
            task_run_id="trun-prop",
            citations=[Citation(
                claim="x",
                sources=[CitationSourceRef(
                    kind="synthetic", ref="s",
                    label=_rand_label(DataClassification.PUBLIC),
                )],
                audience=rng.choice(other_audiences),
                release_class="attested",
            )],
        )
        gate = ReleaseGate(card=card)
        with pytest.raises(ReleaseDenied):
            gate.release({"claims": ["x"]}, manifest)


def test_property_release_gate_accepts_well_formed_for_any_audience():
    """For any audience IN the Card and a public citation, the
    gate accepts. 50 random cases."""
    rng = random.Random(0xCAFE)
    card_audiences = ["partner", "internal", "public"]
    for _ in range(50):
        aud = rng.choice(card_audiences)
        card = AgentCard(
            capability_id="prop", name="Prop", version="0.1.0",
            partner_id="p", partner_contract_id="c",
            audiences=card_audiences,
        )
        manifest = CitationManifest(
            task_run_id="trun-prop",
            citations=[Citation(
                claim="x",
                sources=[CitationSourceRef(
                    kind="synthetic", ref="s",
                    label=_rand_label(DataClassification.PUBLIC),
                )],
                audience=aud,
                release_class="attested",
            )],
        )
        gate = ReleaseGate(card=card)
        # No raise.
        gate.release({"claims": ["x"]}, manifest)


# ---------------------------------------------------------------------------
# Property 3: Projector never produces > byte_budget
# ---------------------------------------------------------------------------


def test_property_projector_never_exceeds_byte_budget():
    """For any random payload whose projected size is bigger than
    the budget, the projector raises. 30 random cases."""
    rng = random.Random(0xDEAD)
    for _ in range(30):
        # Generate a payload with random allowed fields + extra
        # fields. The allowed fields are taken from a fixed set;
        # the extras are always dropped.
        allowed = ["a", "b", "c"]
        payload = {f: rng.choice(string.ascii_letters) * 50 for f in allowed}
        # Random extras — every extra is dropped.
        for _ in range(rng.randint(0, 5)):
            extra_key = rng.choice(string.ascii_letters) + str(rng.randint(0, 1000))
            payload[extra_key] = "x" * 50
        manifest_budget = rng.randint(50, 200)
        from orchestra.core.schema import FieldManifest
        manifest = FieldManifest(
            name="prop",
            source_view="view:prop",
            allowed_fields=allowed,
            byte_budget=manifest_budget,
        )
        projector = FieldProjector()
        # The projected payload is at most the size of the
        # allowed fields serialised, plus overhead. If the
        # allowed fields' values * 50 overflow the budget, the
        # projector raises; if they fit, it doesn't.
        try:
            result = projector.project(manifest, payload)
            # The result bytes are within the budget.
            assert result.projected_bytes <= manifest_budget
            # And the projected payload only contains allowed fields.
            for k in result.projected:
                assert k in allowed
        except EgressBudgetExceeded:
            # The exception is correct: the projected payload
            # exceeded the budget. No assertion on the projected
            # bytes here — the exception IS the signal.
            pass


# ---------------------------------------------------------------------------
# Property 5: Ingress never admits with wrong audience
# ---------------------------------------------------------------------------


def test_property_ingress_never_admits_wrong_audience():
    """For any audience not in the Card, the Ingress refuses.
    30 random cases."""
    rng = random.Random(0xA11CE)
    key = hmac_keygen()
    registry = PublishedRegistry(default_key=key, default_kid="key-prop")
    card_audiences = ["partner", "internal", "public"]
    for _ in range(30):
        card = AgentCard(
            capability_id="prop", name="Prop", version="0.1.0",
            partner_id="p", partner_contract_id="c",
            audiences=card_audiences,
        )
        registry.publish(card, key=key, kid="key-prop")
        ingress = Ingress(registry, token_key=key)
        # Mint a token with a *different* audience.
        bad_aud = rng.choice(["x", "y", "z", "internal-only"])
        token = ingress.issue_token(
            issuer="p", subject="u",
            audience=bad_aud, scopes=[],
        )
        with pytest.raises(IngressDenied):
            ingress.admit(capability_id="prop", version="0.1.0", token=token)
        # Reset the registry for the next iteration by re-adding
        # the card. (Each iteration publishes a fresh card; the
        # dedup-by-version is satisfied because version is 0.1.0
        # every time. So we manually clear the dedup table.)
        registry._by_version.clear()  # noqa: SLF001
        registry._latest.clear()  # noqa: SLF001
