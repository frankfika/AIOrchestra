"""M6 ENT-004 — Enterprise connector interfaces.

Four connectors every production deployment needs:

  * :class:`OIDCVerifier`        — verify IdP-issued tokens
  * :class:`SCIMDirectory`       — sync users/groups
  * :class:`KMSKeyProvider`      — issue / rotate / revoke signing keys
  * :class:`SIEMForwarder`       — forward audit events to a SIEM

M6 ships a *dev* implementation for each. The production swap
replaces the dev impl with a real backend (Okta, Azure AD, AWS
KMS, Splunk) without changing the call site. The dev impls share
the same interface as the production ones so the test suite proves
the contract.
"""
from __future__ import annotations

import abc
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from orchestra.core.hashing import hmac_keygen, hmac_sign, hmac_verify
from orchestra.core.ids import new_id
from orchestra.core.time import utc_now_iso


# ---------------------------------------------------------------------------
# OIDC
# ---------------------------------------------------------------------------


class OIDCVerifier(abc.ABC):
    """Verify an IdP-issued bearer token.

    The dev impl uses a static HMAC key. The production swap uses
    a real OIDC discovery document + JWKS.
    """

    @abc.abstractmethod
    def verify(self, token: str) -> dict[str, Any]:
        """Return the token's claims or raise :class:`TokenInvalid`."""


class TokenInvalid(Exception):
    pass


@dataclass
class DevHMACIdP:
    """A dev IdP that mints + verifies HMAC-signed tokens.

    The token shape is the same as :class:`Ingress.issue_token` in
    M5: ``<base64url-payload>.<sig>``.
    """

    issuer: str
    audience: str
    key: bytes

    def mint(self, subject: str, scopes: list[str], *, expires_at: Optional[int] = None) -> str:
        import base64
        body = {"iss": self.issuer, "sub": subject, "aud": self.audience, "scope": " ".join(scopes)}
        if expires_at is not None:
            body["exp"] = expires_at
        payload = json.dumps(body, sort_keys=True, ensure_ascii=False)
        sig = hmac_sign(self.key, payload)
        return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii") + "." + sig

    def verify(self, token: str) -> dict[str, Any]:
        import base64
        try:
            payload_b64, sig = token.rsplit(".", 1)
            payload = base64.urlsafe_b64decode(payload_b64.encode("ascii")).decode("utf-8")
        except Exception as e:  # noqa: BLE001
            raise TokenInvalid(f"malformed token: {e}") from e
        if not hmac_verify(self.key, payload, sig):
            raise TokenInvalid("signature invalid")
        return json.loads(payload)


# ---------------------------------------------------------------------------
# SCIM
# ---------------------------------------------------------------------------


@dataclass
class SCIMUser:
    user_id: str
    email: str
    display_name: str
    active: bool = True
    groups: list[str] = field(default_factory=list)


class SCIMDirectory(abc.ABC):
    """A minimal SCIM 2.0 directory.

    Production: bind to Okta / Azure AD / Google Workspace. Dev:
    in-memory dict.
    """

    @abc.abstractmethod
    def upsert_user(self, user: SCIMUser) -> None: ...

    @abc.abstractmethod
    def get_user(self, user_id: str) -> Optional[SCIMUser]: ...

    @abc.abstractmethod
    def list_users(self) -> list[SCIMUser]: ...

    @abc.abstractmethod
    def deactivate(self, user_id: str) -> None: ...


class InMemorySCIMDirectory(SCIMDirectory):
    def __init__(self) -> None:
        self._users: dict[str, SCIMUser] = {}

    def upsert_user(self, user: SCIMUser) -> None:
        self._users[user.user_id] = user

    def get_user(self, user_id: str) -> Optional[SCIMUser]:
        return self._users.get(user_id)

    def list_users(self) -> list[SCIMUser]:
        return list(self._users.values())

    def deactivate(self, user_id: str) -> None:
        if user_id in self._users:
            self._users[user_id] = SCIMUser(
                user_id=self._users[user_id].user_id,
                email=self._users[user_id].email,
                display_name=self._users[user_id].display_name,
                active=False,
                groups=self._users[user_id].groups,
            )


# ---------------------------------------------------------------------------
# KMS
# ---------------------------------------------------------------------------


@dataclass
class KMSKey:
    kid: str
    algorithm: str
    material: bytes
    created_at: str = field(default_factory=utc_now_iso)
    rotated_to: Optional[str] = None
    revoked: bool = False


class KMSKeyProvider(abc.ABC):
    """Issue + rotate + revoke signing keys.

    Production: AWS KMS / GCP KMS / HashiCorp Vault. Dev: in-memory
    dict keyed by ``kid``.
    """

    @abc.abstractmethod
    def create_key(self, algorithm: str = "HS256") -> KMSKey: ...

    @abc.abstractmethod
    def get_key(self, kid: str) -> Optional[KMSKey]: ...

    @abc.abstractmethod
    def rotate(self, old_kid: str) -> KMSKey: ...

    @abc.abstractmethod
    def revoke(self, kid: str) -> None: ...


class InMemoryKMSKeyProvider(KMSKeyProvider):
    def __init__(self) -> None:
        self._keys: dict[str, KMSKey] = {}

    def create_key(self, algorithm: str = "HS256") -> KMSKey:
        kid = f"key:{new_id()[:8]}"
        key = KMSKey(kid=kid, algorithm=algorithm, material=hmac_keygen())
        self._keys[kid] = key
        return key

    def get_key(self, kid: str) -> Optional[KMSKey]:
        k = self._keys.get(kid)
        if k is None or k.revoked:
            return None
        return k

    def rotate(self, old_kid: str) -> KMSKey:
        old = self._keys.get(old_kid)
        if old is None:
            raise KeyError(f"no such key: {old_kid}")
        new_key = self.create_key(algorithm=old.algorithm)
        old.rotated_to = new_key.kid
        return new_key

    def revoke(self, kid: str) -> None:
        if kid in self._keys:
            self._keys[kid].revoked = True


# ---------------------------------------------------------------------------
# SIEM
# ---------------------------------------------------------------------------


class SIEMForwarder(abc.ABC):
    """Forward audit events to a SIEM.

    Production: Splunk HEC / Elastic / Datadog. Dev: in-memory list
    the test suite can assert against.
    """

    @abc.abstractmethod
    def forward(self, event: dict[str, Any]) -> None: ...


class InMemorySIEMForwarder(SIEMForwarder):
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def forward(self, event: dict[str, Any]) -> None:
        # SIEMs typically want an enriched event with a timestamp
        # and a forwarder id. The dev impl preserves the body
        # verbatim and adds a forwarded_at field.
        self.events.append({**event, "forwarded_at": utc_now_iso()})
