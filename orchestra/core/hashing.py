"""P0 signing primitives.

P0 uses **HMAC-SHA256** for two reasons:
1. The P0 Node Grant is a "locally-signed dev credential" (see ADR-0002),
   not a delegated OAuth chain. HMAC over a canonical JSON envelope is the
   simplest construct that gives a verifiable signature.
2. P0 receipts are basic signed events. We use a COSE-*like* envelope —
   ``protected`` header + ``payload`` + ``signature`` — so a future M5 swap
   to real COSE_Sign1 is mechanical, not architectural.

The same key is reused for Plan signing in P0 (single-tenant demo).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from typing import Any

from orchestra.core.ids import digest_json


def hmac_keygen() -> bytes:
    """Generate a 32-byte HMAC key (for tests / dev)."""
    return os.urandom(32)


def hmac_sign(key: bytes, payload: Any) -> str:
    """Sign a JSON-serialisable payload. Returns base64url(signature)."""
    msg = digest_json(payload).encode("ascii")
    sig = hmac.new(key, msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")


def hmac_verify(key: bytes, payload: Any, signature_b64url: str) -> bool:
    """Verify a signature produced by :func:`hmac_sign`."""
    expected = hmac_sign(key, payload)
    return hmac.compare_digest(expected, signature_b64url)


def cose_like_envelope(
    *,
    protected: dict[str, Any],
    payload: Any,
    key: bytes,
    kid: str,
) -> dict[str, Any]:
    """Build a COSE_Sign1-like envelope.

    The shape is intentionally close to RFC 9052 §4.2 so that an M5 swap to
    real COSE is a drop-in replacement. ``protected`` is the algorithm
    descriptor (``{"alg": "HS256", "kid": "<kid>"}``); ``payload`` is the
    canonicalised JSON body; ``signature`` is base64url(HMAC).
    """
    prot = {"alg": "HS256", "kid": kid, **protected}
    body = {
        "protected": prot,
        "payload": payload,
    }
    sig = hmac_sign(key, body)
    return {**body, "signature": sig}


def verify_cose_like(envelope: dict[str, Any], key: bytes) -> bool:
    """Verify a COSE-like envelope. Returns True iff the signature matches."""
    sig = envelope.get("signature")
    if not isinstance(sig, str):
        return False
    body = {"protected": envelope["protected"], "payload": envelope["payload"]}
    return hmac_verify(key, body, sig)
