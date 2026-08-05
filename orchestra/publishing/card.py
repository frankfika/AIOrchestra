"""M5 PUB-001 — Signed Agent Card.

A published capability's Agent Card is a JSON document that the
partner fetches to learn:

  * what the capability does (``name``, ``description``)
  * which version is current (``version``)
  * which audiences may call it (``audiences``)
  * which data views / fields leave the tenant (``data_views``)
  * which contract binds the partner (``partner_contract_id``)
  * what the partner needs to send in its bearer token
    (``token_requirements``)

The Card is content-addressed (``card_id``) and signed with an
HMAC key. The signature uses the same :class:`cose_like_envelope`
shape as the M2 Receipts so partners only need one verification
path.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from orchestra.core.hashing import cose_like_envelope, verify_cose_like
from orchestra.core.ids import new_id
from orchestra.core.time import utc_now_iso


class CardStatus(str, Enum):
    """Lifecycle of a published Agent Card.

    The Card moves forward through these states. Revocation sets
    ``status = REVOKED`` from any prior state. A revoked card is
    rejected by :func:`verify_card`.
    """

    DRAFT = "draft"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"
    REVOKED = "revoked"


class AgentCard(BaseModel):
    """A signed, content-addressed description of a published capability.

    The Card is what a partner fetches before calling the capability.
    The Coordinator's ingress layer rejects any call whose token
    does not match the Card's ``token_requirements`` and whose
    capability_id+version is not on the current published set.
    """

    model_config = ConfigDict(extra="forbid")

    card_id: str = Field(default_factory=lambda: f"card:{new_id()[:8]}")
    capability_id: str
    name: str
    version: str
    description: str = ""
    # M5 — the partner this Card is published to. M5 is
    # single-partner per Card; M6 introduces many-to-many.
    partner_id: str
    partner_contract_id: str
    # The audiences that may call this capability. A caller presenting
    # a token whose ``aud`` claim is NOT in this set is rejected.
    audiences: list[str] = Field(default_factory=list)
    # The data views the partner may use. Each view name is a key
    # into the published FieldManifest registry. The Egress PEP
    # refuses to forward a call whose view_name is not in this set.
    data_views: list[str] = Field(default_factory=list)
    # The partner-facing contract. A copy is embedded so the Card is
    # self-contained.
    contract_snapshot: dict[str, Any] = Field(default_factory=dict)
    # Token requirements. The partner's bearer token must satisfy
    # these (issuer, audience, scope).
    token_requirements: dict[str, Any] = Field(
        default_factory=lambda: {"issuer": None, "scope": []},
    )
    status: CardStatus = CardStatus.DRAFT
    created_at: str = Field(default_factory=utc_now_iso)
    published_at: Optional[str] = None
    revoked_at: Optional[str] = None
    # The signature is computed *over* the body (excluding the
    # signature field itself). M5 uses HMAC-SHA256 like the rest of
    # the dev plan; the real partner shipping is HMAC + JWS in M6.
    signature: Optional[str] = None
    signer_kid: Optional[str] = None

    def to_signable(self) -> dict[str, Any]:
        body = self.model_dump(mode="json", exclude={"signature", "signer_kid"})
        return body

    def is_current(self) -> bool:
        """A Card is "current" when it has been published and not
        revoked. :func:`verify_card` is the authoritative check; this
        helper is for UX rendering."""
        return self.status == CardStatus.PUBLISHED


def sign_card(card: AgentCard, *, key: bytes, kid: str) -> AgentCard:
    """Sign a Card with an HMAC key.

    Returns a copy of the Card with ``signature`` and ``signer_kid``
    set. The original is not mutated so callers can keep a draft.
    """
    body = card.to_signable()
    envelope = cose_like_envelope(protected={"kid": kid, "alg": "HS256"}, payload=body, key=key, kid=kid)
    return card.model_copy(update={
        "signature": envelope["signature"],
        "signer_kid": envelope["protected"]["kid"],
    })


def verify_card(card: AgentCard, *, key: bytes) -> bool:
    """Verify a Card's signature.

    Returns ``True`` if the signature is valid *and* the Card is in
    the ``PUBLISHED`` state. A revoked or deprecated Card is
    rejected even if the signature matches — the lifecycle state is
    authoritative.
    """
    if card.status != CardStatus.PUBLISHED:
        return False
    if not card.signature or not card.signer_kid:
        return False
    return verify_cose_like(
        envelope={
            "signature": card.signature,
            "protected": {"kid": card.signer_kid, "alg": "HS256"},
            "payload": card.to_signable(),
        },
        key=key,
    )
