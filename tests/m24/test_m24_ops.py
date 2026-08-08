"""M24 W4 — Pilot operations primitives (M24-OPS-001).

Self-contained tests for the rotation + drill helpers. The
M24-OPS-001 surface does not need a live database for the
happy paths; the DB-backed paths are exercised by W2 + W3
test suites already.
"""
from __future__ import annotations

from orchestra.enterprise.connectors import InMemoryKMSKeyProvider
from orchestra.enterprise.ops import (
    rotate_kms_key,
    rotate_webhook_secret,
)
from orchestra.core.ids import new_id


# ---------------------------------------------------------------------------
# KMS rotation
# ---------------------------------------------------------------------------


def test_rotate_kms_key_creates_new_kid() -> None:
    provider = InMemoryKMSKeyProvider()
    old = provider.create_key()
    result = rotate_kms_key(provider, old_kid=old.kid)
    assert result.old_kid == old.kid
    assert result.new_kid != old.kid
    # The old key still exists and resolves (we do not auto-revoke
    # on rotation; the on-call revokes separately once the
    # rotation window elapses).
    assert provider.get_key(old.kid) is not None
    # The new key is active.
    assert provider.get_key(result.new_kid) is not None


def test_rotate_kms_key_unknown_kid_raises() -> None:
    provider = InMemoryKMSKeyProvider()
    try:
        rotate_kms_key(provider, old_kid="key:does-not-exist")
    except KeyError:
        return
    raise AssertionError("expected KeyError for unknown kid")


def test_rotate_kms_key_picks_latest_when_old_kid_omitted() -> None:
    provider = InMemoryKMSKeyProvider()
    a = provider.create_key()
    b = provider.create_key()
    result = rotate_kms_key(provider)
    # The rotation picks one of the existing keys (a or b)
    # and creates a new one. ``created_at`` ties happen within
    # a millisecond, so the assertion is order-agnostic: we
    # must rotate one of the two and not a third orphan.
    assert result.old_kid in {a.kid, b.kid}
    assert result.new_kid not in {a.kid, b.kid}


def test_rotate_kms_key_empty_provider_raises() -> None:
    provider = InMemoryKMSKeyProvider()
    try:
        rotate_kms_key(provider)
    except KeyError:
        return
    raise AssertionError("expected KeyError on empty provider")


# ---------------------------------------------------------------------------
# Webhook secret rotation
# ---------------------------------------------------------------------------


def test_rotate_webhook_secret_returns_new_and_digest() -> None:
    old = new_id()
    result = rotate_webhook_secret(partner="pilot-1", current_secret=old)
    assert result.partner == "pilot-1"
    assert result.new_secret != ""
    assert result.new_secret != old
    # The hash of the old secret is preserved for audit; the
    # plaintext itself is not.
    assert len(result.old_secret_sha256) == 64
    # Re-hashing the old secret should match the digest.
    import hashlib

    assert (
        result.old_secret_sha256
        == hashlib.sha256(old.encode("utf-8")).hexdigest()
    )


def test_rotate_webhook_secret_without_current() -> None:
    result = rotate_webhook_secret(partner="pilot-1")
    assert result.partner == "pilot-1"
    assert result.new_secret != ""
    # No current secret → empty digest.
    assert result.old_secret_sha256 == ""
