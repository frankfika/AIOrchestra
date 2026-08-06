"""Signed Receipt builder.

A Receipt is a COSE-like envelope over a digest of the relevant state at
node-completion time:

  protected: {"type": "node-receipt", "alg": "HS256", "kid": <kid>}
  payload: {
    receipt_id, task_run_id, node_run_id, node_id,
    plan_digest, capability_id, manifest_id, data_view_digest,
    inputs_digest, outputs_digest, started_at, ended_at, status
  }
  signature: base64url(HMAC-SHA256(protected || payload))

P0 does **not** include a Merkle root or inclusion proof (see ADR-0002).
The verification step is therefore: rebuild the envelope, recompute the
HMAC, compare. This is what ``Coordinator._verify_receipts`` does at
the end of a run.
"""
from __future__ import annotations

from typing import Any

from orchestra.core.hashing import cose_like_envelope, verify_cose_like
from orchestra.core.ids import digest_json, new_id
from orchestra.core.schema import SignedReceipt
from orchestra.core.time import utc_now_iso


def _digest(payload: Any) -> str:
    return digest_json(payload)


class ReceiptBuilder:
    def __init__(self, key: bytes, kid: str = "p0-receipt-key") -> None:
        self._key = key
        self._kid = kid

    def build(
        self,
        *,
        task_run_id: str,
        node_run_id: str,
        node_id: str,
        plan_digest: str,
        capability_id: str,
        manifest_id: str,
        data_view: dict[str, Any],
        inputs: Any,
        outputs: Any,
        started_at: str,
        ended_at: str,
        status: str,
    ) -> SignedReceipt:
        payload = {
            "receipt_id": new_id(),
            "task_run_id": task_run_id,
            "node_run_id": node_run_id,
            "node_id": node_id,
            "plan_digest": plan_digest,
            "capability_id": capability_id,
            "manifest_id": manifest_id,
            "data_view_digest": _digest(data_view),
            "inputs_digest": _digest(inputs),
            "outputs_digest": _digest(outputs),
            "started_at": started_at,
            "ended_at": ended_at,
            "status": status,
        }
        env = cose_like_envelope(
            protected={"type": "node-receipt"},
            payload=payload,
            key=self._key,
            kid=self._kid,
        )
        return SignedReceipt(
            receipt_id=payload["receipt_id"],
            task_run_id=task_run_id,
            node_run_id=node_run_id,
            node_id=node_id,
            envelope=env,
            created_at=utc_now_iso(),
        )

    def verify(self, receipt: SignedReceipt) -> bool:
        return verify_cose_like(receipt.envelope, self._key)
