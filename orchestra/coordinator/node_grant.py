"""P0 Node Grant issuer.

The grant is a local-signed dev credential (see ADR-0002). It binds:

- task_run_id, node_run_id
- task_id, node_id
- capability_id, manifest_id
- data_view (what payload the Adapter is allowed to see)
- purpose (cannot be changed by delegation)
- expiry (short, ≤ 1h by default)

The signature is HMAC-SHA256 over the canonical-JSON body. The Coordinator
checks expiry before invoking the Adapter.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from orchestra.core.hashing import cose_like_envelope, verify_cose_like
from orchestra.core.ids import new_id
from orchestra.core.schema import (
    DataView,
    NodeGrant,
    Purpose,
)
from orchestra.core.time import parse_utc_iso, utc_now_iso


DEFAULT_TTL_SECONDS = 600  # 10 minutes — P0 dev credential


class NodeGrantIssuer:
    def __init__(self, key: bytes, kid: str = "p0-grant-key", ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._key = key
        self._kid = kid
        self._ttl = ttl_seconds

    def issue(
        self,
        *,
        task_run_id: str,
        node_run_id: str,
        task_id: str,
        node_id: str,
        capability_id: str,
        manifest_id: str,
        data_view: DataView,
        purpose: Purpose,
    ) -> NodeGrant:
        now = utc_now_iso()
        expires = (
            parse_utc_iso(now) + timedelta(seconds=self._ttl)
        ).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z"
        grant = NodeGrant(
            grant_id=new_id(),
            task_run_id=task_run_id,
            node_run_id=node_run_id,
            task_id=task_id,
            node_id=node_id,
            capability_id=capability_id,
            manifest_id=manifest_id,
            data_view=data_view,
            purpose=purpose,
            issued_at=now,
            not_before=now,
            expires_at=expires,
            audience="p0",
        )
        env = cose_like_envelope(
            protected={"type": "node-grant"},
            payload=grant.model_dump(mode="json"),
            key=self._key,
            kid=self._kid,
        )
        signed = grant.model_copy(update={"signature": env["signature"]})
        signed.__dict__["_envelope"] = env  # cached for the receipt step
        return signed

    def envelope_for(self, grant: NodeGrant) -> dict[str, Any]:
        return cose_like_envelope(
            protected={"type": "node-grant"},
            payload=grant.model_dump(mode="json"),
            key=self._key,
            kid=self._kid,
        )

    def verify(self, grant: NodeGrant) -> bool:
        if not grant.signature:
            return False
        env = self.envelope_for(grant)
        return verify_cose_like(env, self._key)

    def is_expired(self, grant: NodeGrant, now_iso: str | None = None) -> bool:
        now = parse_utc_iso(now_iso or utc_now_iso())
        return parse_utc_iso(grant.expires_at) <= now
