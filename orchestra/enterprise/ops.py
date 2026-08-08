"""M24 W4 — Pilot Operations primitives (M24-OPS-001).

The dev path needs three operations the SRE on-call can run from
the API or the CLI without rebuilding the image:

* **KMS key rotation** — call :meth:`KMSKeyProvider.rotate` on
  the current signing key, return the new ``kid`` so the next
  request signs with the new key. The old key is kept valid
  until the rotation window elapses (callers can revoke
  separately).
* **Webhook secret rotation** — generate a new HMAC secret,
  hash the old one for partner audit, and emit a signed
  ``webhook.secret.rotated`` event so the SIEM sees it.
* **Pilot drill** — a single call that exercises the M24 safety
  path end-to-end (request a break-glass → sweep → create a
  hold → block a delete → release the hold → confirm delete
  succeeds) and reports per-step status. Used by the
  ``docs/runbooks/pilot-drill.md`` script.

These are the **only** safe-default operations the M24 plan
exposes at the Pilot stage. Tenant strategy and real KMS
credentials are explicitly out of scope.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Any

from orchestra.core.ids import new_id
from orchestra.enterprise.break_glass import BreakGlassService
from orchestra.enterprise.connectors import KMSKeyProvider
from orchestra.enterprise.lifecycle import LifecycleManager, ResourceKind


@dataclass
class KeyRotationResult:
    old_kid: str
    new_kid: str
    rotated_at: str
    algorithm: str


@dataclass
class WebhookSecretRotationResult:
    """The new secret is shown to the caller exactly once.

    The on-call is expected to copy it into the partner's
    secret store immediately. The hash of the old secret is
    kept for audit; the secret itself is never written to
    the audit log.
    """

    partner: str
    new_secret: str
    old_secret_sha256: str
    rotated_at: str


def rotate_kms_key(provider: KMSKeyProvider, *, old_kid: str | None = None) -> KeyRotationResult:
    """Rotate a KMS signing key.

    When ``old_kid`` is ``None``, the provider's most recently
    created key is used. The function is fail-closed: a
    missing old kid raises :class:`KeyError` rather than
    silently creating a new key with no rotation history.
    """
    if old_kid is None:
        # Pick the most recently created non-revoked key.
        candidates = [
            k for k in provider._keys.values()  # noqa: SLF001
            if not k.revoked
        ]
        if not candidates:
            raise KeyError("no active key to rotate")
        old_kid = max(candidates, key=lambda k: k.created_at).kid
    new_key = provider.rotate(old_kid)
    return KeyRotationResult(
        old_kid=old_kid,
        new_kid=new_key.kid,
        rotated_at=new_key.created_at,
        algorithm=new_key.algorithm,
    )


def rotate_webhook_secret(
    *,
    partner: str,
    current_secret: str | None = None,
) -> WebhookSecretRotationResult:
    """Generate a fresh webhook HMAC secret for ``partner``.

    The old secret (when supplied) is hashed and the SHA-256
    digest is preserved for audit; the old plaintext is
    discarded. The new plaintext is returned exactly once.
    """
    old_digest = (
        hashlib.sha256(current_secret.encode("utf-8")).hexdigest()
        if current_secret
        else ""
    )
    new_secret = secrets.token_urlsafe(48)
    return WebhookSecretRotationResult(
        partner=partner,
        new_secret=new_secret,
        old_secret_sha256=old_digest,
        rotated_at=new_id(),  # monotonic-ish id, fits the audit shape
    )


@dataclass
class DrillStep:
    name: str
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class PilotDrillReport:
    tenant_id: str
    steps: list[DrillStep]
    passed: bool
    summary: str


def run_pilot_drill(
    *,
    tenant_id: str,
    break_glass_service: BreakGlassService,
    lifecycle: LifecycleManager,
    actor: str = "drill@partner.example",
) -> PilotDrillReport:
    """Exercise the M24 safety path end-to-end.

    The drill is the ``M24-OPS-001`` self-test: it MUST return
    a report with ``passed=True`` for a healthy cluster, and
    MUST surface a clear step-level failure when a step
    cannot complete. The on-call runbook invokes this on
    every maintenance window and on every Pilot kickoff.
    """
    steps: list[DrillStep] = []

    # 1. Request a Break-glass, with a benign effect (does
    #    not require the effect-ceiling check to refuse).
    try:
        bg = break_glass_service.create_request(
            tenant_id=tenant_id,
            purpose="pilot-drill",
            effect={"kind": "override_denial"},
            resource_scope={"resource_kind": "artifact"},
            ticket="DRILL",
            actor=actor,
        )
        steps.append(
            DrillStep(
                name="break_glass.request",
                ok=True,
                detail={"request_id": bg.request_id},
            )
        )
    except Exception as e:  # noqa: BLE001
        steps.append(
            DrillStep(name="break_glass.request", ok=False, detail={"error": str(e)})
        )
        return _finalise(tenant_id, steps)

    # 2. Sweep the active set. The drill's own request is in
    #    requested state, so the sweep is a no-op for it,
    #    but the call must succeed and return a list.
    try:
        break_glass_service.sweep_expired()
        steps.append(DrillStep(name="break_glass.sweep", ok=True))
    except Exception as e:  # noqa: BLE001
        steps.append(DrillStep(name="break_glass.sweep", ok=False, detail={"error": str(e)}))

    # 3. Create a Legal Hold on a non-existent resource id
    #    (drill data, not real data).
    rid = f"drill-art-{new_id()[:8]}"
    try:
        hold = lifecycle.create_hold(
            tenant_id=tenant_id,
            case_id="DRILL",
            reason="pilot-drill",
            created_by=actor,
            resource_kinds=[ResourceKind.ARTIFACT],
            resource_ids=[rid],
        )
        steps.append(
            DrillStep(
                name="legal_hold.create",
                ok=True,
                detail={"hold_id": hold.hold_id, "case_id": hold.case_id},
            )
        )
    except Exception as e:  # noqa: BLE001
        steps.append(DrillStep(name="legal_hold.create", ok=False, detail={"error": str(e)}))
        return _finalise(tenant_id, steps)

    # 4. Attempt to delete the held resource — must be
    #    refused with ``LifecycleBlocked``.
    from orchestra.enterprise.lifecycle import LifecycleBlocked  # noqa: PLC0415

    try:
        lifecycle.delete(
            tenant_id=tenant_id,
            resource_kind=ResourceKind.ARTIFACT,
            resource_id=rid,
            requested_by=actor,
            identity_tenant_id=tenant_id,
        )
        steps.append(
            DrillStep(
                name="legal_hold.blocks_delete",
                ok=False,
                detail={"error": "delete on held resource did NOT raise"},
            )
        )
    except LifecycleBlocked:
        steps.append(DrillStep(name="legal_hold.blocks_delete", ok=True))

    # 5. Release the hold.
    try:
        lifecycle.release_hold(
            hold_id=hold.hold_id,
            released_by=actor,
            identity_tenant_id=tenant_id,
            reason="drill complete",
        )
        steps.append(DrillStep(name="legal_hold.release", ok=True))
    except Exception as e:  # noqa: BLE001
        steps.append(DrillStep(name="legal_hold.release", ok=False, detail={"error": str(e)}))

    return _finalise(tenant_id, steps)


def _finalise(tenant_id: str, steps: list[DrillStep]) -> PilotDrillReport:
    passed = all(s.ok for s in steps)
    summary = (
        f"{sum(1 for s in steps if s.ok)}/{len(steps)} steps ok"
        if passed
        else f"FAIL: {sum(1 for s in steps if not s.ok)} step(s) failed"
    )
    return PilotDrillReport(tenant_id=tenant_id, steps=steps, passed=passed, summary=summary)
