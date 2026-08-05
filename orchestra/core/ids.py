"""ID generation and content addressing.

Identifiers in P0 are:
- ``new_id()``: random UUIDv4 (for runs, events, requests)
- ``content_addressed_id(kind, payload)``: SHA-256 over canonical JSON, used
  for Plan digests, Receipt references, and Manifest snapshots.

We use SHA-256 instead of a cryptographic hash family with longer outputs
because the P0 demo only needs collision resistance inside one tenant /
session — see ADR-0002 for what is and isn't production.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any


def new_id() -> str:
    """Random UUIDv4 string."""
    return str(uuid.uuid4())


def digest_bytes(data: bytes) -> str:
    """SHA-256 hex digest of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def digest_json(payload: Any) -> str:
    """SHA-256 hex digest over a canonical JSON encoding.

    ``payload`` must be JSON-serialisable. Pydantic models are accepted via
    ``model_dump(mode='json')`` — callers are expected to do that conversion
    themselves to keep this function dependency-free.
    """
    return digest_bytes(_canonical_json(payload))


def content_addressed_id(kind: str, payload: Any) -> str:
    """Stable, content-addressed identifier of the form ``kind:sha256[:12]``.

    ``kind`` is short and human-meaningful (e.g. ``plan``, ``manifest``,
    ``grant``). The first 12 hex chars of the digest are kept in the ID to
    make logs readable; the full digest is still recoverable from the payload.
    """
    d = digest_json(payload)
    return f"{kind}:{d[:12]}"
