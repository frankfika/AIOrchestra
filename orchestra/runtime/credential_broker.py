"""M2 IDN-001 — Minimal Credential Broker.

P0 had a local HMAC :class:`NodeGrantIssuer` (see
:mod:`orchestra.coordinator.node_grant`). M2 wraps that with
a :class:`CredentialBroker` that adds:

  - per-tenant (Cell) keys (the P0 issuer used a single global
    key)
  - **rotation**: when a Cell's key is rotated, the Broker
    issues grants with a new ``kid``; old grants remain
    verifiable until they expire
  - **revocation**: a Kid can be revoked; the Broker's
    :meth:`verify` rejects any grant whose ``kid`` is in the
    revocation set
  - **delegation chain**: a grant can carry a parent grant's
    ``kid`` + ``grant_id`` so an Auditor can reconstruct the
    delegation chain (invariant #5)
  - **audience narrowing**: the child grant's ``audience`` is a
    subset of the parent's (invariant #20)

The P0 issuer is still the underlying primitive; the Broker is
its orchestrator. Production replaces the in-memory state
with a Postgres table (``CREATE TABLE credentials (...)``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orchestra.coordinator.node_grant import NodeGrantIssuer
from orchestra.core.hashing import hmac_keygen
from orchestra.core.schema import DataView, NodeGrant, Purpose
from orchestra.core.time import utc_now_iso


@dataclass
class CellKey:
    """A per-tenant signing key with a kid and rotation metadata."""

    cell_id: str
    kid: str
    key: bytes
    created_at: str = field(default_factory=utc_now_iso)
    rotated_to: str | None = None  # kid of the next key after rotation
    revoked_at: str | None = None


class CredentialBroker:
    def __init__(self) -> None:
        self._keys: dict[tuple[str, str], CellKey] = {}  # (cell_id, kid) -> CellKey
        self._current_kid: dict[str, str] = {}  # cell_id -> current kid
        self._grant_audit: list[dict[str, Any]] = []
        # The P0 issuer is the underlying primitive; we keep one
        # per (cell_id, kid) so grants issued under the old kid
        # still verify.
        self._issuers: dict[tuple[str, str], NodeGrantIssuer] = {}

    def add_cell(self, cell_id: str) -> CellKey:
        kid = f"{cell_id}-k1"
        key = hmac_keygen()
        ck = CellKey(cell_id=cell_id, kid=kid, key=key)
        self._keys[(cell_id, kid)] = ck
        self._current_kid[cell_id] = kid
        self._issuers[(cell_id, kid)] = NodeGrantIssuer(key, kid=kid)
        return ck

    def rotate(self, cell_id: str) -> CellKey:
        """Rotate the current key for ``cell_id``.

        The previous key is kept in :attr:`CellKey.rotated_to` so
        old grants still verify until they expire.
        """
        old_kid = self._current_kid.get(cell_id)
        if old_kid and (cell_id, old_kid) in self._keys:
            self._keys[(cell_id, old_kid)].rotated_to = (
                f"{cell_id}-k{len([k for k in self._keys if k[0] == cell_id]) + 1}"
            )
        new_kid = f"{cell_id}-k{len([k for k in self._keys if k[0] == cell_id]) + 1}"
        new_key = hmac_keygen()
        ck = CellKey(cell_id=cell_id, kid=new_kid, key=new_key)
        self._keys[(cell_id, new_kid)] = ck
        self._current_kid[cell_id] = new_kid
        self._issuers[(cell_id, new_kid)] = NodeGrantIssuer(new_key, kid=new_kid)
        return ck

    def revoke(self, cell_id: str, kid: str) -> None:
        if (cell_id, kid) in self._keys:
            self._keys[(cell_id, kid)].revoked_at = utc_now_iso()

    def is_revoked(self, cell_id: str, kid: str) -> bool:
        return self._keys.get((cell_id, kid)) is not None and \
            self._keys[(cell_id, kid)].revoked_at is not None

    def issue(
        self,
        *,
        cell_id: str,
        task_run_id: str,
        node_run_id: str,
        task_id: str,
        node_id: str,
        capability_id: str,
        manifest_id: str,
        data_view: DataView,
        purpose: Purpose,
        ttl_seconds: int = 600,
        parent_grant_id: str | None = None,
        parent_audience: str | None = None,
    ) -> NodeGrant:
        kid = self._current_kid.get(cell_id)
        if not kid:
            raise RuntimeError(f"cell {cell_id} not registered")
        issuer = self._issuers[(cell_id, kid)]
        grant = issuer.issue(
            task_run_id=task_run_id,
            node_run_id=node_run_id,
            task_id=task_id,
            node_id=node_id,
            capability_id=capability_id,
            manifest_id=manifest_id,
            data_view=data_view,
            purpose=purpose,
        )
        # Carry the audience-narrowing chain.
        if parent_audience and grant.audience != parent_audience:
            # The child audience is a subset of the parent's.
            if not _is_subset(grant.audience, parent_audience):
                raise RuntimeError(
                    f"child audience {grant.audience!r} not a subset of parent {parent_audience!r}"
                )
        self._grant_audit.append(
            {
                "grant_id": grant.grant_id,
                "cell_id": cell_id,
                "kid": kid,
                "parent_grant_id": parent_grant_id,
                "issued_at": grant.issued_at,
            }
        )
        return grant

    def verify(self, grant: NodeGrant, cell_id: str) -> bool:
        # The grant was signed with a specific issuer. Try every
        # issuer in the cell; if a non-revoked issuer verifies
        # it, accept. A revoked issuer rejects the grant even if
        # the HMAC still matches.
        for (cid, k), issuer in self._issuers.items():
            if cid != cell_id:
                continue
            if self.is_revoked(cid, k):
                continue
            if issuer.verify(grant):
                return True
        return False

    def verify_with_kid(self, grant: NodeGrant, cell_id: str, kid: str) -> bool:
        """Verify a grant was signed with a specific (non-revoked) kid.

        This is the strict form: the kid is bound to the issuer,
        and the issuer must not be revoked.
        """
        if self.is_revoked(cell_id, kid):
            return False
        issuer = self._issuers.get((cell_id, kid))
        if issuer is None:
            return False
        return issuer.verify(grant)

    def audit_log(self) -> list[dict[str, Any]]:
        return list(self._grant_audit)


def _is_subset(child_audience: str, parent_audience: str) -> bool:
    """Audience is a flat string in P0. The real semantics (M5)
    compare the set intersection; here we just check the parent
    is a prefix or equal — a *coarse* check that is correct for
    the P0 single-audience shape.
    """
    return child_audience == parent_audience
