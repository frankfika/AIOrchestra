"""M7 — SLO + Pilot evidence + GA readiness tests."""
from __future__ import annotations

import json

import pytest

from orchestra.core.hashing import hmac_keygen
from orchestra.ga.evidence import (
    PilotEvidence,
    collect_pilot_evidence,
    verify_pilot_evidence,
)
from orchestra.ga.slo import (
    GAReadiness,
    PilotTelemetry,
    availability_slo,
    compute_attainment,
    evaluate_ga_readiness,
    latency_p95_slo,
    recovery_time_slo,
    rpo_slo,
)


# ---------------------------------------------------------------------------
# SLO helpers
# ---------------------------------------------------------------------------


def test_availability_slo_meets_target():
    r = availability_slo(succeeded=999, total=1000, target=0.999)
    assert r.meets is True
    assert r.attainment == 1.0  # measured == target


def test_availability_slo_below_target():
    r = availability_slo(succeeded=995, total=1000, target=0.999)
    assert r.meets is False
    assert r.measured == 0.995


def test_availability_slo_too_few_samples_flags_as_not_meeting():
    """A pilot with < 100 tasks is too small to claim the SLO.
    The measured value is still reported but meets=False so the
    GA gate does not pass on a tiny sample."""
    r = availability_slo(succeeded=99, total=99, target=0.999)
    assert r.meets is False


def test_availability_slo_zero_total():
    r = availability_slo(succeeded=0, total=0, target=0.999)
    assert r.measured == 0.0
    assert r.meets is False


def test_latency_p95_slo_meets_target():
    samples = [100, 200, 150, 180, 250, 300, 220, 800, 1200, 4500]
    r = latency_p95_slo(samples_ms=samples, target_ms=5000)
    # p95 is the 95th percentile of sorted samples; index
    # ceil(0.95 * 10) - 1 = ceil(9.5) - 1 = 10 - 1 = 9 -> 4500.
    assert r.measured == 4500
    assert r.meets is True


def test_latency_p95_slo_exceeds_target():
    samples = [100, 200, 150, 180, 250, 300, 220, 800, 1200, 6000]
    r = latency_p95_slo(samples_ms=samples, target_ms=5000)
    assert r.measured == 6000
    assert r.meets is False


def test_recovery_time_slo_no_failures_meets_target():
    """A pilot with no failures meets recovery-time by definition
    (the empty set has no outliers)."""
    r = recovery_time_slo(recovery_intervals_s=[], target_s=60.0)
    assert r.meets is True


def test_recovery_time_slo_meets_target():
    r = recovery_time_slo(recovery_intervals_s=[5, 10, 8, 15, 3, 20, 7, 12, 6, 9], target_s=60.0)
    assert r.meets is True


def test_rpo_slo_meets_target():
    r = rpo_slo(audit_gap_seconds=2.0, target_s=60.0)
    assert r.meets is True
    assert r.attainment == 1.0


def test_rpo_slo_exceeds_target():
    r = rpo_slo(audit_gap_seconds=120.0, target_s=60.0)
    assert r.meets is False


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


def _good_telemetry() -> PilotTelemetry:
    return PilotTelemetry(
        succeeded_tasks=999,
        total_tasks=1000,
        latency_samples_ms=[100, 200, 150, 180, 250, 300, 220, 800, 1200, 4500],
        recovery_intervals_s=[5, 10, 8, 15, 3, 20, 7, 12, 6, 9],
        audit_gap_seconds=2.0,
    )


def test_compute_attainment_returns_all_four():
    results = compute_attainment(_good_telemetry())
    assert len(results) == 4
    assert {r.name for r in results} == {
        "availability", "latency_p95_ms", "recovery_time_p95_s", "rpo_s",
    }


def test_evaluate_ga_readiness_pilot_meets_all_slos():
    r = evaluate_ga_readiness(_good_telemetry(), deployment_days=30)
    assert r.all_slos_meet is True
    assert r.ga_ready is True
    assert r.blockers == []


def test_evaluate_ga_readiness_blocks_on_short_deployment():
    """The M7 minimum deployment window is 14 days. A pilot under
    that is not GA-ready even if the SLOs pass — the operator
    cannot prove the SLOs are stable without runway."""
    r = evaluate_ga_readiness(_good_telemetry(), deployment_days=7, minimum_deployment_days=14)
    assert r.ga_ready is False
    assert any("deployment_days" in b for b in r.blockers)


def test_evaluate_ga_readiness_blocks_on_failing_availability():
    bad = PilotTelemetry(
        succeeded_tasks=900,
        total_tasks=1000,
        latency_samples_ms=[100],
        recovery_intervals_s=[],
        audit_gap_seconds=2.0,
    )
    r = evaluate_ga_readiness(bad, deployment_days=30)
    assert r.ga_ready is False
    assert any("availability" in b for b in r.blockers)


def test_ga_readiness_to_dict_is_json_serialisable():
    r = evaluate_ga_readiness(_good_telemetry(), deployment_days=30)
    serialised = json.dumps(r.to_dict())
    reloaded = json.loads(serialised)
    assert reloaded["ga_ready"] is True
    assert len(reloaded["slo_results"]) == 4


# ---------------------------------------------------------------------------
# Pilot evidence
# ---------------------------------------------------------------------------


def test_collect_pilot_evidence_unsigned():
    ev = collect_pilot_evidence(
        pilot_id="pilot-acme",
        pilot_name="ACME Pilot 1",
        deployment_days=30,
        deployment_person_days=8.5,
        support_cost_usd=4200.0,
        gross_margin_signal=0.62,
        renewal_intent_score=0.8,
        telemetry=_good_telemetry(),
    )
    assert ev.evidence_id.startswith("ev:")
    assert ev.readiness.ga_ready is True
    assert ev.signature is None
    assert ev.kid is None


def test_collect_pilot_evidence_signed_verifies():
    key = hmac_keygen()
    ev = collect_pilot_evidence(
        pilot_id="pilot-acme",
        pilot_name="ACME Pilot 1",
        deployment_days=30,
        deployment_person_days=8.5,
        support_cost_usd=4200.0,
        gross_margin_signal=0.62,
        renewal_intent_score=0.8,
        telemetry=_good_telemetry(),
        signing_key=key,
        kid="key-1",
    )
    assert ev.signature is not None
    assert ev.kid == "key-1"
    assert verify_pilot_evidence(ev, key=key) is True
    # Tampering with any field invalidates the signature.
    tampered = PilotEvidence(
        evidence_id=ev.evidence_id,
        pilot_id=ev.pilot_id,
        pilot_name="TAMPERED",
        deployment_days=ev.deployment_days,
        deployment_person_days=ev.deployment_person_days,
        support_cost_usd=ev.support_cost_usd,
        gross_margin_signal=ev.gross_margin_signal,
        renewal_intent_score=ev.renewal_intent_score,
        telemetry=ev.telemetry,
        readiness=ev.readiness,
        captured_at=ev.captured_at,
        signature=ev.signature,
        kid=ev.kid,
    )
    assert not verify_pilot_evidence(tampered, key=key)


def test_unsigned_evidence_does_not_verify():
    ev = collect_pilot_evidence(
        pilot_id="pilot-unsigned",
        pilot_name="Unsigned",
        deployment_days=30,
        deployment_person_days=1.0,
        support_cost_usd=0.0,
        gross_margin_signal=0.0,
        renewal_intent_score=0.0,
        telemetry=_good_telemetry(),
    )
    assert not verify_pilot_evidence(ev, key=hmac_keygen())


def test_evidence_to_dict_round_trip():
    ev = collect_pilot_evidence(
        pilot_id="pilot-rt",
        pilot_name="Roundtrip",
        deployment_days=30,
        deployment_person_days=4.0,
        support_cost_usd=100.0,
        gross_margin_signal=0.5,
        renewal_intent_score=0.5,
        telemetry=_good_telemetry(),
    )
    d = ev.to_dict()
    serialised = json.dumps(d)
    reloaded = json.loads(serialised)
    assert reloaded["pilot_id"] == "pilot-rt"
    assert reloaded["readiness"]["ga_ready"] is True
    assert len(reloaded["readiness"]["slo_results"]) == 4
