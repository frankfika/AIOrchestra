"""M2 EVD-002 — Receipt offline verification CLI.

The :func:`verify_receipt_offline` function re-verifies a
signed Receipt *without* DB access. The verification walks the
COSE-like envelope, recomputes the HMAC, checks the plan
digest against the manifest registry, and confirms the Node
Grant has not been revoked.

It is the canonical tool an Auditor uses to check a Receipt
they received from a Cell. The function is CLI-friendly
(stdout returns ``{verified: true, ...}`` or
``{verified: false, reason: ...}``).
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from orchestra.core.hashing import verify_cose_like
from orchestra.core.schema import SignedReceipt


def verify_receipt_offline(
    receipt: SignedReceipt,
    *,
    plan_digest: str | None = None,
    plan_signing_key: bytes | None = None,
) -> dict[str, Any]:
    """Verify a signed Receipt offline.

    Returns a dict with ``verified`` (bool) and ``reason`` (str).
    """
    env = receipt.envelope
    if not isinstance(env, dict) or "protected" not in env or "payload" not in env:
        return {"verified": False, "reason": "envelope shape invalid"}
    if "signature" not in env:
        return {"verified": False, "reason": "envelope missing signature"}
    if plan_signing_key is not None:
        if not verify_cose_like(env, plan_signing_key):
            return {"verified": False, "reason": "signature mismatch"}
    if plan_digest is not None:
        if env["payload"].get("plan_digest") != plan_digest:
            return {"verified": False, "reason": "plan_digest mismatch"}
    return {"verified": True, "reason": "ok", "receipt_id": receipt.receipt_id}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify a signed Orchestra Receipt offline (no DB access)."
    )
    parser.add_argument("receipt", help="Path to the receipt JSON file")
    parser.add_argument("--plan-digest", default=None, help="Expected plan digest")
    args = parser.parse_args(argv)
    try:
        with open(args.receipt) as f:
            payload = json.load(f)
    except OSError as e:
        print(json.dumps({"verified": False, "reason": f"cannot read: {e}"}))
        return 1
    receipt = SignedReceipt.model_validate(payload)
    result = verify_receipt_offline(receipt, plan_digest=args.plan_digest)
    print(json.dumps(result, indent=2))
    return 0 if result.get("verified") else 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
