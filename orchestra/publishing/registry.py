"""M5 PUB-001/003 — Published Capability Registry.

The Registry is the in-memory store of Agent Cards. It supports:

  * publish — move a Card from DRAFT to PUBLISHED.
  * revoke — set status to REVOKED with a timestamp.
  * lookup by capability_id+version (current) or capability_id (latest).
  * list-by-partner for the partner's "what can I call?" view.

Version pinning: a published Card declares ``version``. A second
Card for the same ``capability_id`` with a new ``version`` does
NOT retire the old one; both stay published until the old Card is
explicitly deprecated or revoked. This is what lets partners
upgrade on their own schedule (PUB-003).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from orchestra.core.errors import OrchestraError
from orchestra.publishing.card import AgentCard, CardStatus, sign_card


class PublishError(OrchestraError):
    """A publish / revoke operation failed (e.g. duplicate card)."""


@dataclass
class _Entry:
    card: AgentCard
    key: bytes
    kid: str


class PublishedRegistry:
    def __init__(self, *, default_key: bytes | None = None, default_kid: str | None = None) -> None:
        # capability_id+version -> Entry. The same capability may
        # have multiple published versions side by side.
        self._by_version: dict[tuple[str, str], _Entry] = {}
        # capability_id -> latest published version (for "current").
        self._latest: dict[str, tuple[str, str]] = {}
        # partner_id -> set of (capability_id, version) for fast
        # partner-scoped lookups.
        self._by_partner: dict[str, set[tuple[str, str]]] = {}
        self._default_key = default_key
        self._default_kid = default_kid

    def publish(self, card: AgentCard, *, key: bytes | None = None, kid: str | None = None) -> AgentCard:
        """Sign + publish a Card.

        A Card in DRAFT becomes PUBLISHED. A Card already PUBLISHED
        is rejected (use :meth:`publish_new_version` for an upgrade).
        A Card in DEPRECATED or REVOKED is rejected.

        The signature is computed AFTER the status flips to
        PUBLISHED so the partner can verify the body they actually
        receive against the same body the tenant signed.
        """
        if card.status != CardStatus.DRAFT:
            raise PublishError(f"cannot publish a card in state {card.status}")
        # Idempotency / version uniqueness: a (capability_id, version)
        # pair may only be published once. A second publish under the
        # same version is a versioning bug.
        if (card.capability_id, card.version) in self._by_version:
            raise PublishError(
                f"{card.capability_id} v{card.version} is already published; "
                "use publish_new_version() for upgrades"
            )
        k = key or self._default_key
        k_kid = kid or self._default_kid
        if k is None or k_kid is None:
            raise PublishError("registry has no default key/kid; pass key= and kid=")
        # Flip state first, then sign. The signature covers the
        # as-published body (status=PUBLISHED, published_at set).
        as_published = card.model_copy(update={
            "status": CardStatus.PUBLISHED,
            "published_at": card.created_at,
        })
        signed = sign_card(as_published, key=k, kid=k_kid)
        entry = _Entry(card=signed, key=k, kid=k_kid)
        self._by_version[(card.capability_id, card.version)] = entry
        if card.capability_id not in self._latest:
            self._latest[card.capability_id] = (card.version, card.partner_id)
        self._by_partner.setdefault(card.partner_id, set()).add((card.capability_id, card.version))
        return signed

    def publish_new_version(self, card: AgentCard, *, key: bytes | None = None, kid: str | None = None) -> AgentCard:
        """Publish a new version of an existing capability.

        Unlike :meth:`publish`, the previous version is NOT
        auto-revoked. Partners can keep calling the old version
        until the tenant explicitly deprecates it. The new version
        becomes the "latest" pointer.
        """
        signed = self.publish(card, key=key, kid=kid)
        self._latest[card.capability_id] = (card.version, card.partner_id)
        return signed

    def get(self, capability_id: str, *, version: str | None = None) -> AgentCard:
        """Return the Card for ``capability_id`` (and ``version`` if
        given, else the latest published version)."""
        if version is not None:
            entry = self._by_version.get((capability_id, version))
            if entry is None:
                raise KeyError(f"no published card for {capability_id} v{version}")
            return entry.card
        latest = self._latest.get(capability_id)
        if latest is None:
            raise KeyError(f"no published card for {capability_id}")
        entry = self._by_version.get((capability_id, latest[0]))
        if entry is None:
            raise KeyError(f"no published card for {capability_id} v{latest[0]}")
        return entry.card

    def latest(self, capability_id: str) -> AgentCard:
        return self.get(capability_id)

    def list_for_partner(self, partner_id: str) -> list[AgentCard]:
        keys = self._by_partner.get(partner_id, set())
        return [self._by_version[k].card for k in sorted(keys)]

    def deprecate(self, capability_id: str, version: str) -> AgentCard:
        entry = self._by_version.get((capability_id, version))
        if entry is None:
            raise KeyError(f"no published card for {capability_id} v{version}")
        updated = entry.card.model_copy(update={"status": CardStatus.DEPRECATED})
        entry.card = updated
        return updated

    def revoke(self, capability_id: str, version: str, *, reason: str = "") -> AgentCard:
        """Revoke a published Card. All future calls referencing
        ``(capability_id, version)`` are denied by the ingress
        layer."""
        entry = self._by_version.get((capability_id, version))
        if entry is None:
            raise KeyError(f"no published card for {capability_id} v{version}")
        from orchestra.core.time import utc_now_iso
        updated = entry.card.model_copy(update={
            "status": CardStatus.REVOKED,
            "revoked_at": utc_now_iso(),
            "contract_snapshot": {**entry.card.contract_snapshot, "revoke_reason": reason},
        })
        entry.card = updated
        # Remove from "latest" if this was the latest version.
        latest = self._latest.get(capability_id)
        if latest and latest[0] == version:
            # If there's another published version, promote it.
            for (cid, ver), ent in self._by_version.items():
                if cid == capability_id and ent.card.status == CardStatus.PUBLISHED and ver != version:
                    self._latest[capability_id] = (ver, ent.card.partner_id)
                    break
            else:
                self._latest.pop(capability_id, None)
        # Remove from partner index.
        partner_set = self._by_partner.get(entry.card.partner_id)
        if partner_set:
            partner_set.discard((capability_id, version))
        return updated

    def is_current(self, capability_id: str, version: str | None = None) -> bool:
        try:
            card = self.get(capability_id, version=version)
        except KeyError:
            return False
        return card.is_current()
