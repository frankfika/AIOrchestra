"""M5 — Agent Card + Partner Contract + Ingress + Kill Switch + Release Gate."""
from __future__ import annotations

import time

import pytest

from orchestra.core.hashing import hmac_keygen
from orchestra.core.ids import new_id
from orchestra.core.schema import (
    Citation,
    CitationManifest,
    CitationSourceRef,
    DataClassification,
    SecurityLabel,
    SourceTrust,
)
from orchestra.publishing import (
    AgentCard,
    AudienceSpec,
    KillSwitch,
    PartnerContract,
    PublishedRegistry,
    ReleaseGate,
)
from orchestra.publishing.card import CardStatus, sign_card, verify_card
from orchestra.publishing.ingress import BearerToken, Ingress, IngressDenied
from orchestra.publishing.kill_switch import KillSwitchTripped
from orchestra.publishing.registry import PublishError
from orchestra.publishing.release_gate import ReleaseDenied


def _key():
    return hmac_keygen()


def _draft_card(**overrides) -> AgentCard:
    base = dict(
        capability_id="pub.summarize",
        name="Summarize",
        version="0.1.0",
        partner_id="partner-acme",
        partner_contract_id="contract-001",
        audiences=["partner-acme-api"],
        data_views=["public-summary"],
    )
    base.update(overrides)
    return AgentCard(**base)


# ---------------------------------------------------------------------------
# Agent Card (PUB-001)
# ---------------------------------------------------------------------------


def test_draft_card_signature_verifies_but_state_rejects():
    """A signed DRAFT Card must NOT pass verify (only PUBLISHED
    Cards are valid). The signature itself is fine; the state
    guard is what rejects it."""
    card = _draft_card()
    key = _key()
    signed = sign_card(card, key=key, kid="signer-1")
    assert signed.signature is not None
    assert signed.signer_kid == "signer-1"
    # State guard: DRAFT is not published, so verify returns False.
    assert not verify_card(signed, key=key)
    # And the registry is the only thing that can flip state to PUBLISHED.


def test_publish_flips_state_and_keeps_signature():
    """Publishing through the Registry flips state to PUBLISHED
    while preserving the signature (the partner can still verify
    the body that was signed at draft time)."""
    registry = PublishedRegistry(default_key=_key(), default_kid="signer-1")
    signed = registry.publish(_draft_card())
    assert signed.status == CardStatus.PUBLISHED
    assert verify_card(signed, key=registry._by_version[("pub.summarize", "0.1.0")].key)
    # And the registry exposes the card.
    assert registry.get("pub.summarize").card_id == signed.card_id


def test_publishing_same_version_twice_raises():
    """A capability cannot be re-published under the same version.
    A second publish is a versioning bug."""
    registry = PublishedRegistry(default_key=_key(), default_kid="signer-1")
    registry.publish(_draft_card())
    with pytest.raises(PublishError):
        registry.publish(_draft_card())


def test_version_pinning_does_not_revoke_old_version():
    """PUB-003: when a tenant ships v0.2.0, the v0.1.0 card stays
    published. Partners keep calling the version they pinned to."""
    registry = PublishedRegistry(default_key=_key(), default_kid="signer-1")
    registry.publish(_draft_card(version="0.1.0"))
    registry.publish_new_version(_draft_card(version="0.2.0"))
    # Both versions are reachable.
    assert registry.get("pub.summarize", version="0.1.0").version == "0.1.0"
    assert registry.get("pub.summarize", version="0.2.0").version == "0.2.0"
    # "latest" pointer advances to v0.2.0.
    assert registry.latest("pub.summarize").version == "0.2.0"
    # v0.1.0 is still PUBLISHED, not DEPRECATED.
    assert registry.get("pub.summarize", version="0.1.0").status == CardStatus.PUBLISHED


# ---------------------------------------------------------------------------
# Revocation + Kill Switch (PUB-003)
# ---------------------------------------------------------------------------


def test_revoke_blocks_future_admits():
    """Revoking a Card makes the next admit() deny."""
    registry = PublishedRegistry(default_key=_key(), default_kid="signer-1")
    registry.publish(_draft_card(version="0.1.0"))
    registry.revoke("pub.summarize", "0.1.0", reason="contract ended")
    # The Card's state is REVOKED.
    assert registry.get("pub.summarize", version="0.1.0").status == CardStatus.REVOKED
    # is_current() reflects it.
    assert not registry.is_current("pub.summarize", version="0.1.0")


def test_revoke_promotes_older_published_version_to_latest():
    """If we revoke the latest version, an older published version
    becomes the 'latest' pointer so partners can keep calling."""
    registry = PublishedRegistry(default_key=_key(), default_kid="signer-1")
    registry.publish(_draft_card(version="0.1.0"))
    registry.publish_new_version(_draft_card(version="0.2.0"))
    registry.revoke("pub.summarize", "0.2.0")
    assert registry.latest("pub.summarize").version == "0.1.0"


def test_kill_switch_takes_effect_within_bounded_time():
    """PUB-003 invariant: the Kill Switch takes effect in
    ``max_effect_seconds`` (default 5s). The test asserts the
    bounded time on the in-memory switch — the production version
    in M6 must replicate this guarantee across the control plane.
    """
    registry = PublishedRegistry(default_key=_key(), default_kid="signer-1")
    registry.publish(_draft_card())
    switch = KillSwitch(max_effect_seconds=5.0)

    # Switch is open; admit() works.
    card = switch.admit(registry, "pub.summarize")
    assert card.status == CardStatus.PUBLISHED

    # Trip and immediately call admit(). The bounded-time guarantee
    # is that the trip is observed by the very next call.
    switch.trip(reason="incident-42")
    with pytest.raises(KillSwitchTripped):
        switch.admit(registry, "pub.summarize")
    # The test fails if a production swap introduces latency
    # between trip() and admit()'s check. (We sleep a tiny amount
    # to allow a clock-resolution gap; the bound is 5s.)
    elapsed = time.monotonic() - switch.tripped_at
    assert elapsed < switch.max_effect_seconds, f"Kill Switch took {elapsed:.3f}s > bound"


def test_kill_switch_reset_restores_admits():
    """A reset() clears the switch; the next admit() works again."""
    registry = PublishedRegistry(default_key=_key(), default_kid="signer-1")
    registry.publish(_draft_card())
    switch = KillSwitch()
    switch.trip()
    switch.reset()
    card = switch.admit(registry, "pub.summarize")
    assert card.status == CardStatus.PUBLISHED


# ---------------------------------------------------------------------------
# Ingress Identity (PUB-002)
# ---------------------------------------------------------------------------


def _contract(partner_id: str = "partner-acme") -> PartnerContract:
    return PartnerContract(
        tenant_id="tenant-demo",
        partner_id=partner_id,
        partner_name="ACME",
        capability_ids=["pub.summarize"],
        audiences=[AudienceSpec(audience_id="partner-acme-api", required_scopes=["read:summary"])],
        data_views=["public-summary"],
    )


def test_ingress_admit_with_valid_token_succeeds():
    """The happy path: a partner with a valid token + matching
    audience + scope is admitted."""
    registry = PublishedRegistry(default_key=_key(), default_kid="signer-1")
    card = _draft_card(audiences=["partner-acme-api"], contract_snapshot=_contract().model_dump(mode="json"))
    registry.publish(card)
    ingress = Ingress(registry, token_key=_key())
    token = ingress.issue_token(
        issuer="acme-idp",
        subject="acme-user-1",
        audience="partner-acme-api",
        scopes=["read:summary"],
    )
    admitted, bt = ingress.admit(capability_id="pub.summarize", version="0.1.0", token=token)
    assert admitted.card_id == card.card_id
    assert bt.subject == "acme-user-1"


def test_ingress_denies_wrong_audience():
    """A token with the right signature but a different ``aud``
    claim is rejected. M5 is strict: the audience IS the
    authorisation."""
    registry = PublishedRegistry(default_key=_key(), default_kid="signer-1")
    registry.publish(_draft_card(audiences=["partner-acme-api"], contract_snapshot=_contract().model_dump(mode="json")))
    ingress = Ingress(registry, token_key=_key())
    token = ingress.issue_token(
        issuer="acme-idp", subject="x", audience="someone-else", scopes=["read:summary"],
    )
    with pytest.raises(IngressDenied) as ei:
        ingress.admit(capability_id="pub.summarize", version="0.1.0", token=token)
    assert "audience" in str(ei.value)


def test_ingress_denies_missing_scope():
    """A token without the required scope is rejected."""
    registry = PublishedRegistry(default_key=_key(), default_kid="signer-1")
    registry.publish(_draft_card(audiences=["partner-acme-api"], contract_snapshot=_contract().model_dump(mode="json")))
    ingress = Ingress(registry, token_key=_key())
    token = ingress.issue_token(
        issuer="acme-idp", subject="x", audience="partner-acme-api", scopes=["read:other"],
    )
    with pytest.raises(IngressDenied) as ei:
        ingress.admit(capability_id="pub.summarize", version="0.1.0", token=token)
    assert "scope" in str(ei.value)


def test_ingress_denies_revoked_card():
    """A revoked Card is rejected even if the token is valid."""
    registry = PublishedRegistry(default_key=_key(), default_kid="signer-1")
    registry.publish(_draft_card(audiences=["partner-acme-api"], contract_snapshot=_contract().model_dump(mode="json")))
    registry.revoke("pub.summarize", "0.1.0")
    ingress = Ingress(registry, token_key=_key())
    token = ingress.issue_token(
        issuer="acme-idp", subject="x", audience="partner-acme-api", scopes=["read:summary"],
    )
    with pytest.raises(IngressDenied):
        ingress.admit(capability_id="pub.summarize", version="0.1.0", token=token)


def test_ingress_denies_bad_signature():
    """A token signed with the wrong key is rejected (HMAC verify)."""
    registry = PublishedRegistry(default_key=_key(), default_kid="signer-1")
    registry.publish(_draft_card(audiences=["partner-acme-api"], contract_snapshot=_contract().model_dump(mode="json")))
    ingress = Ingress(registry, token_key=_key())
    # Mint a token with a *different* signing key.
    other_ingress = Ingress(registry, token_key=hmac_keygen())
    token = other_ingress.issue_token(
        issuer="acme-idp", subject="x", audience="partner-acme-api", scopes=["read:summary"],
    )
    with pytest.raises(IngressDenied) as ei:
        ingress.admit(capability_id="pub.summarize", version="0.1.0", token=token)
    assert "signature" in str(ei.value)


# ---------------------------------------------------------------------------
# Release Gate (REL-001)
# ---------------------------------------------------------------------------


def _safe_label() -> SecurityLabel:
    return SecurityLabel(
        classification=DataClassification.PUBLIC,
        residency="public",
        source_trust=SourceTrust.PUBLIC,
    )


def _restricted_label() -> SecurityLabel:
    return SecurityLabel(
        classification=DataClassification.RESTRICTED,
        residency="local",
        source_trust=SourceTrust.INTERNAL,
    )


def _manifest(*citations) -> CitationManifest:
    return CitationManifest(
        task_run_id=f"trun-{new_id()[:8]}",
        citations=list(citations),
    )


def _citation(audience: str, sources=None) -> Citation:
    return Citation(
        claim="test claim",
        sources=sources or [CitationSourceRef(kind="synthetic", ref="synth-1", label=_safe_label())],
        audience=audience,
        release_class="attested",
    )


def test_release_gate_accepts_well_formed_result():
    card = _draft_card(audiences=["partner-acme-api", "partner"])
    gate = ReleaseGate(card=card)
    manifest = _manifest(
        _citation("partner"),
        _citation("partner"),
    )
    result = {"claims": ["a", "b"]}
    assert gate.release(result, manifest) is result


def test_release_gate_denies_free_text():
    """Free-text results (no claims list) are not a structured
    release. The Gate refuses them so partners cannot receive
    arbitrary prose that might leak internal state."""
    card = _draft_card(audiences=["partner-acme-api", "partner"])
    gate = ReleaseGate(card=card)
    with pytest.raises(ReleaseDenied) as ei:
        gate.release({"text": "free-form text dump"}, _manifest(_citation("partner")))
    assert "claims" in str(ei.value)


def test_release_gate_denies_forbidden_keys():
    """``error`` / ``stacktrace`` / ``internal_id`` are explicitly
    forbidden in any released payload. A naive release that dumps
    an exception trace leaks the internal IdP / DB host."""
    card = _draft_card(audiences=["partner-acme-api", "partner"])
    gate = ReleaseGate(card=card)
    for key in ("error", "trace", "stacktrace", "internal_id"):
        with pytest.raises(ReleaseDenied) as ei:
            gate.release({"claims": ["a"], key: "leak"}, _manifest(_citation("partner")))
        assert "forbidden" in str(ei.value).lower()


def test_release_gate_denies_restricted_citation():
    """A citation that carries a restricted label is NEVER
    releasable, regardless of the Card's audience."""
    card = _draft_card(audiences=["partner-acme-api", "partner"])
    gate = ReleaseGate(card=card)
    manifest = _manifest(Citation(
        claim="x",
        sources=[CitationSourceRef(kind="node-output", ref="n-1", label=_restricted_label())],
        audience="partner",
        release_class="attested",
    ))
    with pytest.raises(ReleaseDenied) as ei:
        gate.release({"claims": ["x"]}, manifest)
    assert "restricted" in str(ei.value)


def test_release_gate_denies_unsourced_claims_past_budget():
    """A claim with no sources is unsourced. M5 default budget is
    zero: every claim must be cited."""
    card = _draft_card(audiences=["partner-acme-api", "partner"])
    gate = ReleaseGate(card=card)
    manifest = _manifest(Citation(claim="x", sources=[], audience="partner", release_class="attested"))
    with pytest.raises(ReleaseDenied) as ei:
        gate.release({"claims": ["x"]}, manifest)
    assert "unsourced" in str(ei.value)


def test_release_gate_denies_citation_audience_not_in_card():
    """A citation whose audience is not in the Card's audience set
    is refused. Otherwise a partner could subscribe to a Card and
    receive claims targeted at a different audience."""
    card = _draft_card(audiences=["partner-acme-api", "partner"])
    gate = ReleaseGate(card=card)
    manifest = _manifest(_citation("internal"))
    with pytest.raises(ReleaseDenied) as ei:
        gate.release({"claims": ["x"]}, manifest)
    assert "audience" in str(ei.value)
