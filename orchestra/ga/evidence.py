"""M7 — Pilot evidence model.

The M7 gate is "real pilots, real SLOs, real numbers, not a
plausible final answer". This module captures the minimum
real-world data points the gate requires:

  * :class:`PilotEvidence` — one pilot's data, signed.
  * :func:`collect_pilot_evidence` — turn a deployment + telemetry
    pair into an evidence record.

A signed evidence record is the deliverable Frank and the
investors can cite. The signature uses the M6 KMS / supply chain
path so the evidence can be verified out-of-band.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from orchestra.core.hashing import hmac_keygen, hmac_sign, hmac_verify
from orchestra.core.ids import new_id
from orchestra.core.time import utc_now_iso
from orchestra.ga.slo import GAReadiness, evaluate_ga_readiness, PilotTelemetry


@dataclass
class PilotEvidence:
    """One pilot's signed evidence record.

    The dev impl uses a local HMAC key. The production impl
    signs with a real KMS key (M6 ENT-004) so the evidence is
    verifiable by an out-of-band auditor.
    """

    evidence_id: str
    pilot_id: str
    pilot_name: str
    deployment_days: int
    deployment_person_days: float
    support_cost_usd: float
    gross_margin_signal: float
    renewal_intent_score: float  # 0.0 - 1.0
    telemetry: PilotTelemetry
    readiness: GAReadiness
    captured_at: str
    signature: Optional[str] = None
    kid: Optional[str] = None

    def to_signable(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "pilot_id": self.pilot_id,
            "pilot_name": self.pilot_name,
            "deployment_days": self.deployment_days,
            "deployment_person_days": self.deployment_person_days,
            "support_cost_usd": self.support_cost_usd,
            "gross_margin_signal": self.gross_margin_signal,
            "renewal_intent_score": self.renewal_intent_score,
            "telemetry": _telemetry_to_dict(self.telemetry),
            "readiness": self.readiness.to_dict(),
            "captured_at": self.captured_at,
        }

    def to_dict(self) -> dict:
        d = self.to_signable()
        d["signature"] = self.signature
        d["kid"] = self.kid
        return d


def collect_pilot_evidence(
    *,
    pilot_id: str,
    pilot_name: str,
    deployment_days: int,
    deployment_person_days: float,
    support_cost_usd: float,
    gross_margin_signal: float,
    renewal_intent_score: float,
    telemetry: PilotTelemetry,
    minimum_deployment_days: int = 14,
    signing_key: bytes | None = None,
    kid: str | None = None,
) -> PilotEvidence:
    """Build + sign a :class:`PilotEvidence` record.

    The signing step is opt-in: passing ``signing_key=None``
    produces an unsigned record (still valid for in-process
    consumption, just not for an external audit).
    """
    readiness = evaluate_ga_readiness(
        telemetry,
        deployment_days=deployment_days,
        minimum_deployment_days=minimum_deployment_days,
    )
    ev = PilotEvidence(
        evidence_id=f"ev:{new_id()[:8]}",
        pilot_id=pilot_id,
        pilot_name=pilot_name,
        deployment_days=deployment_days,
        deployment_person_days=deployment_person_days,
        support_cost_usd=support_cost_usd,
        gross_margin_signal=gross_margin_signal,
        renewal_intent_score=renewal_intent_score,
        telemetry=telemetry,
        readiness=readiness,
        captured_at=utc_now_iso(),
    )
    if signing_key is not None and kid is not None:
        ev.signature = hmac_sign(signing_key, ev.to_signable())
        ev.kid = kid
    return ev


def verify_pilot_evidence(ev: PilotEvidence, *, key: bytes) -> bool:
    """Verify an evidence record's signature."""
    if not ev.signature or not ev.kid:
        return False
    return hmac_verify(key, ev.to_signable(), ev.signature)


def _telemetry_to_dict(t: PilotTelemetry) -> dict:
    return {
        "succeeded_tasks": t.succeeded_tasks,
        "total_tasks": t.total_tasks,
        "latency_samples_ms": list(t.latency_samples_ms),
        "recovery_intervals_s": list(t.recovery_intervals_s),
        "audit_gap_seconds": t.audit_gap_seconds,
        "availability_target": t.availability_target,
        "latency_p95_target_ms": t.latency_p95_target_ms,
        "recovery_p95_target_s": t.recovery_p95_target_s,
        "rpo_target_s": t.rpo_target_s,
    }
