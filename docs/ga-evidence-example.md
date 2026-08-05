# GA Evidence — Worked Example

> **Purpose:** Show Frank + the M7 GA gate what a *real*
> pilot evidence record looks like. The numbers below are
> synthetic but the shape is the production one. A real pilot
> replaces each value with a measurement; the gate consumes
> the same JSON.

## The shape

A :class:`orchestra.ga.evidence.PilotEvidence` is a single
JSON object. The signature covers the body excluding the
``signature`` and ``kid`` fields (see
:mod:`orchestra.enterprise.supply_chain`'s ``Provenance`` for
the same pattern). The production swap signs with the M6 KMS;
the dev path uses an HMAC key.

## Worked example

```json
{
  "evidence_id": "ev:ab12cd34",
  "pilot_id": "pilot-acme-2026-q3",
  "pilot_name": "ACME Q3 2026 Pilot",
  "deployment_days": 42,
  "deployment_person_days": 9.5,
  "support_cost_usd": 5400.0,
  "gross_margin_signal": 0.61,
  "renewal_intent_score": 0.85,
  "telemetry": {
    "succeeded_tasks": 12_847,
    "total_tasks": 12_910,
    "latency_samples_ms": [145, 220, 310, 410, ... 1847, 3210],
    "recovery_intervals_s": [3, 7, 12, 4, 5, 8, ... 22],
    "audit_gap_seconds": 1.5,
    "availability_target": 0.999,
    "latency_p95_target_ms": 5000.0,
    "recovery_p95_target_s": 60.0,
    "rpo_target_s": 60.0
  },
  "readiness": {
    "slo_results": [
      {
        "name": "availability",
        "target": 0.999,
        "measured": 0.9951,
        "attainment": 0.9961,
        "meets": false
      },
      {
        "name": "latency_p95_ms",
        "target": 5000.0,
        "measured": 3210.0,
        "attainment": 1.0,
        "meets": true
      },
      {
        "name": "recovery_time_p95_s",
        "target": 60.0,
        "measured": 22.0,
        "attainment": 1.0,
        "meets": true
      },
      {
        "name": "rpo_s",
        "target": 60.0,
        "measured": 1.5,
        "attainment": 1.0,
        "meets": true
      }
    ],
    "all_slos_meet": false,
    "deployment_days": 42,
    "minimum_deployment_days": 14,
    "ga_ready": false,
    "blockers": [
      "availability: measured=0.9951, target=0.999"
    ]
  },
  "captured_at": "2026-08-06T18:30:00Z",
  "signature": "x4j1K8...base64url...",
  "kid": "key:kms-1"
}
```

## Reading the evidence

| Field | What it tells the M7 GA gate |
| --- | --- |
| ``pilot_id`` / ``pilot_name`` | Identifies the pilot. ``pilot_id`` is stable across the pilot; ``pilot_name`` is the human label. |
| ``deployment_days`` | 42 > 14 (M7 minimum). The pilot has been on the system long enough to be a real signal. |
| ``deployment_person_days`` | 9.5 person-days to stand up. The M7 gate requires this to be ≤ 4-6 weeks of calendar time; 9.5 days inside 6 weeks is well within target. |
| ``support_cost_usd`` | 5,400 USD for the 42 days = $128/day. The M7 gate is not a hard number; the trend matters. |
| ``gross_margin_signal`` | 0.61. The M7 gate requires the pilot to be on the path to a positive gross margin; 0.61 is in the band where the gross-margin-sensitive line items are tracking to target. |
| ``renewal_intent_score`` | 0.85. A non-binding NPS-style score from the pilot's CSM; 0.85 is strong positive intent. |
| ``telemetry.succeeded_tasks`` / ``total_tasks`` | 12,847 / 12,910 = 0.9951 availability. Below the 0.999 target. |
| ``telemetry.latency_samples_ms`` | The full per-task wall-clock distribution; the p95 = 3,210ms is below the 5,000ms target. |
| ``readiness.ga_ready`` | ``false`` — availability is the blocker. The pilot needs another two weeks to push the availability above 0.999 (a 0.4 percentage-point improvement over 42 days of running). |

## What blocks GA readiness

The blockers are explicit in the ``readiness.blockers`` list.
The M7 gate consumes the list and reports a single
``ga_ready: bool``. The two failure modes the gate is built
to catch:

1. **Below-target SLO**: at least one of availability / latency /
   recovery / RPO is below the operator's target. The pilot
   must either tune the system or relax the target (with
   written justification) before the next evidence capture.
2. **Below-minimum deployment days**: the pilot has not been
   on the system long enough to be a real signal. The M7
   default is 14 days; a 7-day pilot is not GA-ready regardless
   of the SLO numbers.

## Producing the evidence

```python
from orchestra.core.hashing import hmac_keygen
from orchestra.ga.evidence import collect_pilot_evidence
from orchestra.ga.slo import PilotTelemetry

telemetry = PilotTelemetry(
    succeeded_tasks=12_847,
    total_tasks=12_910,
    latency_samples_ms=read_p95_samples(),
    recovery_intervals_s=read_recovery_intervals(),
    audit_gap_seconds=read_max_wal_gap(),
)

evidence = collect_pilot_evidence(
    pilot_id="pilot-acme-2026-q3",
    pilot_name="ACME Q3 2026 Pilot",
    deployment_days=42,
    deployment_person_days=9.5,
    support_cost_usd=5_400.0,
    gross_margin_signal=0.61,
    renewal_intent_score=0.85,
    telemetry=telemetry,
    signing_key=hmac_keygen(),  # production: KMS key
    kid="key:kms-1",
)

# Sign + verify the evidence out-of-band.
assert verify_pilot_evidence(evidence, key=kms_key)

# The investor-facing summary is the readiness block.
print(evidence.readiness.ga_ready)  # False
print(evidence.readiness.blockers)  # ["availability: ..."]
```

## When GA readiness flips to True

The M7 gate consumes the next evidence record. When
``ga_ready`` flips to True:

1. The pilot is GA-ready: all SLOs meet, deployment days ≥
   minimum, and the sign-off from the M7 sign-off chain (CSM
   + Engineering + Compliance) is attached.
2. The next release is tagged ``v0.<next>`` and the
   [`docs/walkthrough-publishing.md`](./walkthrough-publishing.md)
   is updated to reflect the GA launch.
3. The evidence record is published to the data room and
   cited in the M7 GA submission to the next pilot.

Until then, the evidence record's ``ga_ready`` is the operator's
to-do list: which SLO is below target, which deployment day
is missing, which audit-trail gap is too wide.
