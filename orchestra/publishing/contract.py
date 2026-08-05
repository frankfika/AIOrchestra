"""M5 PUB-002 — Partner Contract.

A Partner Contract is the *legal+technical* agreement between a
tenant and a single named partner. The Agent Card embeds a
snapshot of the contract so the partner can verify what they
agreed to without an out-of-band document lookup.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orchestra.core.ids import new_id
from orchestra.core.time import utc_now_iso


class AudienceSpec(BaseModel):
    """The audience claim a partner's token must carry.

    ``audience_id`` is the OAuth / OIDC ``aud`` value; the partner
    embeds it in its bearer token. The Card's ``audiences`` list
    is the union of every AudienceSpec in the contract.
    """

    model_config = ConfigDict(extra="forbid")

    audience_id: str
    description: str = ""
    # Scopes the partner must hold. Each scope is a string the
    # partner's IdP issues; the Ingress layer checks the token's
    # ``scope`` claim contains every required scope.
    required_scopes: list[str] = Field(default_factory=list)


class PartnerContract(BaseModel):
    """The partner-facing contract.

    A tenant signs one PartnerContract per partner. The contract
    pins:

      * which capabilities the partner may call (capability_ids)
      * which audiences the partner may claim (audiences)
      * which data views the partner may use (data_views)
      * when the contract expires (expires_at)
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: str = Field(default_factory=lambda: f"contract:{new_id()[:8]}")
    tenant_id: str
    partner_id: str
    partner_name: str
    capability_ids: list[str] = Field(default_factory=list)
    audiences: list[AudienceSpec] = Field(default_factory=list)
    data_views: list[str] = Field(default_factory=list)
    expires_at: str | None = None
    created_at: str = Field(default_factory=utc_now_iso)
    # Free-form metadata. The contract is the legal artefact; the
    # metadata is the human-readable summary.
    metadata: dict[str, Any] = Field(default_factory=dict)
