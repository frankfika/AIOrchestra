"""M1 BND-001 — Plan Signer.

The Plan Signer computes the Plan's content-addressed digest
and signs it with the Plan Signer key. The signed Plan is what
the M2 Reconciler uses to detect tampering (every state
transition carries the Plan's digest as a chain anchor).

The Signer is symmetric (HMAC) for M1; the dev plan notes that
production will use asymmetric keys (EdDSA / RS256) with
per-tenant ``kid``. The Pydantic model's :class:`ExecutionPlan`
already carries ``signed_by`` and ``signature`` fields.
"""
from __future__ import annotations

from orchestra.core.hashing import hmac_keygen, hmac_sign
from orchestra.core.schema import ExecutionPlan


class PlanSigner:
    def __init__(self, key: bytes, kid: str = "p1-plan-signer") -> None:
        self._key = key
        self._kid = kid

    def sign(self, plan: ExecutionPlan) -> ExecutionPlan:
        # Sign the body BEFORE updating signed_by. The signer is part
        # of the protected header, not the payload.
        body = plan.model_dump(mode="json", exclude={"signature", "signed_by"})
        sig = hmac_sign(self._key, body)
        return plan.model_copy(
            update={"signature": sig, "signed_by": self._kid}
        )

    def verify(self, plan: ExecutionPlan) -> bool:
        from orchestra.core.hashing import hmac_verify
        if not plan.signature:
            return False
        body = plan.model_dump(
            mode="json", exclude={"signature", "signed_by"}
        )
        return hmac_verify(self._key, body, plan.signature)
