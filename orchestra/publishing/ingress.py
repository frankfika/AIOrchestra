"""M5 PUB-002 — Ingress Identity verification.

A published capability is only reachable by callers that present a
bearer token satisfying the Card's :class:`AudienceSpec` and whose
``capability_id`` + ``version`` are in the published registry.

The Ingress layer is invoked at the *start* of every external
call. It does NOT decide what data the call sees (the Release Gate
does that). It only decides whether the call is allowed to start.

The token verification is intentionally minimal: M5 accepts HMAC-
signed bearer tokens (mirroring the M2 Credential Broker shape).
M6 will swap the verifier for an OIDC / SPIFFE-aware one without
changing the interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from orchestra.core.errors import OrchestraError
from orchestra.core.hashing import hmac_sign, hmac_verify
from orchestra.publishing.card import AgentCard, CardStatus
from orchestra.publishing.registry import PublishedRegistry

if TYPE_CHECKING:  # pragma: no cover
    from orchestra.observability.metrics import Metrics


class IngressDenied(OrchestraError):
    """The Ingress refused to start the call."""


@dataclass
class BearerToken:
    """The minimum set of claims the Ingress layer cares about.

    A real OAuth/OIDC token has many more claims; the Ingress only
    inspects the four below. Partners are expected to send a token
    whose ``aud`` matches one of the Card's audiences and whose
    ``scope`` covers the required scopes.
    """

    issuer: str
    subject: str
    audience: str
    scopes: list[str]
    # Optional expiration in Unix seconds. M5 does not enforce;
    # M6 will swap in a real clock check.
    expires_at: int | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BearerToken:
        scopes = d.get("scope") or d.get("scopes") or []
        if isinstance(scopes, str):
            scopes = scopes.split()
        return cls(
            issuer=d.get("iss", ""),
            subject=d.get("sub", ""),
            audience=d.get("aud", ""),
            scopes=list(scopes),
            expires_at=d.get("exp"),
        )

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


class Ingress:
    """The Ingress layer for published capabilities.

    Constructed with a :class:`PublishedRegistry` and the HMAC key
    that signs the tokens. Real M5 deployments use a partner-specific
    key; the dev demo uses the default key.
    """

    def __init__(
        self,
        registry: PublishedRegistry,
        *,
        token_key: bytes,
        metrics: Metrics | None = None,
    ) -> None:
        self._registry = registry
        self._token_key = token_key
        # M13 — every admit call records an outcome so a SRE can
        # see admit / reject pressure in the dashboard.
        self._metrics = metrics
        if metrics is not None:
            self._m_admit = metrics.counter(
                "orchestra_ingress_admit_total",
                "Total Ingress.admit calls.",
                labels=("outcome",),
            )
        else:
            self._m_admit = None

    def issue_token(
        self,
        *,
        issuer: str,
        subject: str,
        audience: str,
        scopes: list[str],
        expires_at: int | None = None,
    ) -> str:
        """Helper for tests and dev demos: mint a token signed with
        ``token_key``. M5 keeps the verifier + signer symmetric; M6
        will split them so the signer is the partner's IdP, not us."""
        body = {
            "iss": issuer,
            "sub": subject,
            "aud": audience,
            "scope": " ".join(scopes),
        }
        if expires_at is not None:
            body["exp"] = expires_at
        import json

        payload = json.dumps(body, sort_keys=True, ensure_ascii=False)
        sig = hmac_sign(self._token_key, payload)  # str
        import base64

        return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii") + "." + sig

    def verify_token(self, token: str) -> BearerToken:
        import base64
        import json as _json

        try:
            payload_b64, sig = token.rsplit(".", 1)
        except ValueError as e:
            raise IngressDenied("malformed bearer token") from e
        try:
            payload = base64.urlsafe_b64decode(payload_b64.encode("ascii")).decode("utf-8")
        except Exception as e:  # noqa: BLE001
            raise IngressDenied("bearer token payload undecodable") from e
        # hmac_verify expects the same base64url signature that
        # hmac_sign produced; sig is already a str.
        if not hmac_verify(self._token_key, payload, sig):
            raise IngressDenied("bearer token signature invalid")
        try:
            claims = _json.loads(payload)
        except Exception as e:  # noqa: BLE001
            raise IngressDenied("bearer token claims undecodable") from e
        return BearerToken.from_dict(claims)

    def admit(
        self,
        *,
        capability_id: str,
        version: str | None,
        token: str,
    ) -> tuple[AgentCard, BearerToken]:
        """Verify the call is allowed to start.

        Returns ``(card, token)`` on success. Raises
        :class:`IngressDenied` on:

          * unknown / revoked / deprecated Card
          * token signature invalid
          * token audience not in the Card's audiences list
          * token missing a required scope
        """
        # 1. Card lookup. The version pin matters for M5: a partner
        #    that pinned to v1 keeps working when v2 ships.
        try:
            card = self._registry.get(capability_id, version=version)
        except KeyError:
            self._record_admit("not_found")
            raise IngressDenied(f"no published card for {capability_id} v{version}")
        if card.status != CardStatus.PUBLISHED:
            self._record_admit("not_published")
            raise IngressDenied(f"card {card.card_id} is {card.status.value}, not published")
        # 2. Token verification.
        try:
            bt = self.verify_token(token)
        except IngressDenied:
            self._record_admit("bad_token")
            raise
        # 3. Audience check.
        if bt.audience not in card.audiences:
            self._record_admit("audience_mismatch")
            raise IngressDenied(
                f"token audience {bt.audience!r} not in card audiences {card.audiences!r}"
            )
        # 4. Scope check: every required scope must be present.
        for spec in card.contract_snapshot.get("audiences", []) or []:
            for scope in spec.get("required_scopes", []):
                if not bt.has_scope(scope):
                    self._record_admit("missing_scope")
                    raise IngressDenied(f"token missing required scope {scope!r}")
        self._record_admit("admitted")
        return card, bt

    def _record_admit(self, outcome: str) -> None:
        if self._m_admit is not None:
            self._m_admit.inc(outcome=outcome)
