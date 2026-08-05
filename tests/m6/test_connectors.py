"""M6 ENT-004 — Enterprise connector contract tests.

The four connectors share a single dev implementation that satisfies
the production interface. The tests here prove the dev impl
behaves correctly; the production swap (Okta / Azure AD / AWS KMS
/ Splunk) is a config change.
"""
from __future__ import annotations

import pytest

from orchestra.enterprise.connectors import (
    DevHMACIdP,
    InMemoryKMSKeyProvider,
    InMemorySCIMDirectory,
    InMemorySIEMForwarder,
    SCIMUser,
    TokenInvalid,
)


# ---------------------------------------------------------------------------
# OIDC
# ---------------------------------------------------------------------------


def test_oidc_dev_idp_round_trip():
    idp = DevHMACIdP(issuer="acme-idp", audience="partner-acme", key=b"k" * 32)
    tok = idp.mint("user-1", ["read", "write"])
    claims = idp.verify(tok)
    assert claims["iss"] == "acme-idp"
    assert claims["sub"] == "user-1"
    assert claims["aud"] == "partner-acme"


def test_oidc_dev_idp_rejects_tampered_token():
    idp = DevHMACIdP(issuer="acme", audience="acme", key=b"k" * 32)
    tok = idp.mint("user-1", ["read"])
    # Flip a character in the payload segment.
    parts = tok.split(".")
    payload_bytes = bytearray(parts[0].encode("ascii"))
    payload_bytes[5] ^= 0x01
    tampered = "".join([bytes(payload_bytes).decode("ascii"), ".", parts[1]])
    with pytest.raises(TokenInvalid):
        idp.verify(tampered)


def test_oidc_dev_idp_wrong_key_rejects():
    idp1 = DevHMACIdP(issuer="a", audience="a", key=b"k" * 32)
    idp2 = DevHMACIdP(issuer="a", audience="a", key=b"x" * 32)
    tok = idp1.mint("u", [])
    with pytest.raises(TokenInvalid):
        idp2.verify(tok)


# ---------------------------------------------------------------------------
# SCIM
# ---------------------------------------------------------------------------


def test_scim_upsert_get_list_deactivate():
    scim = InMemorySCIMDirectory()
    u1 = SCIMUser(user_id="u1", email="u1@acme", display_name="U One", groups=["devs"])
    scim.upsert_user(u1)
    assert scim.get_user("u1") is not None
    assert scim.list_users() == [u1]
    scim.deactivate("u1")
    assert scim.get_user("u1").active is False


def test_scim_get_unknown_returns_none():
    scim = InMemorySCIMDirectory()
    assert scim.get_user("nobody") is None


def test_scim_upsert_overwrites():
    scim = InMemorySCIMDirectory()
    scim.upsert_user(SCIMUser(user_id="u1", email="old@acme", display_name="old"))
    scim.upsert_user(SCIMUser(user_id="u1", email="new@acme", display_name="new"))
    assert scim.get_user("u1").email == "new@acme"


# ---------------------------------------------------------------------------
# KMS
# ---------------------------------------------------------------------------


def test_kms_create_get_revoke():
    kms = InMemoryKMSKeyProvider()
    k1 = kms.create_key()
    assert k1.kid.startswith("key:")
    fetched = kms.get_key(k1.kid)
    assert fetched is not None
    assert fetched.kid == k1.kid
    kms.revoke(k1.kid)
    # Revoked keys are no longer returned.
    assert kms.get_key(k1.kid) is None


def test_kms_rotate_promotes_to_new_key():
    """Rotation produces a new key; the old key keeps the
    ``rotated_to`` pointer so verifiers can chase the chain."""
    kms = InMemoryKMSKeyProvider()
    k1 = kms.create_key()
    k2 = kms.rotate(k1.kid)
    assert k1.rotated_to == k2.kid
    # Both are usable (until k1 is revoked).
    assert kms.get_key(k1.kid) is not None
    assert kms.get_key(k2.kid) is not None


def test_kms_revoke_unknown_is_noop():
    """Revoking a key that doesn't exist does not raise; the
    M6 dev impl treats it as idempotent."""
    kms = InMemoryKMSKeyProvider()
    kms.revoke("nonexistent")  # should not raise


# ---------------------------------------------------------------------------
# SIEM
# ---------------------------------------------------------------------------


def test_siem_in_memory_records_events():
    siem = InMemorySIEMForwarder()
    siem.forward({"kind": "task.received", "task_run_id": "t-1"})
    siem.forward({"kind": "io.sent", "task_run_id": "t-1"})
    assert len(siem.events) == 2
    # Each event has a forwarded_at timestamp.
    assert all("forwarded_at" in e for e in siem.events)
    # The original event body is preserved.
    assert siem.events[0]["kind"] == "task.received"
    assert siem.events[1]["kind"] == "io.sent"
