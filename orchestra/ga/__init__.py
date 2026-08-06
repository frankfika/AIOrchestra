"""M7 — GA Candidate.

The M7 gate is mostly about commercial evidence: real pilots,
real SLOs, real runbooks. The :mod:`orchestra.ga` package holds
the code artefacts that go with the runbooks:

  * :mod:`orchestra.ga.slo`      — SLO calculator. Operator runs
    :func:`compute_attainment` against the last 30 days of pilot
    telemetry; the result is the SLO attainment evidence that
    M7 requires.
  * :mod:`orchestra.ga.evidence`  — Pilot evidence model. A
    dataclass that captures the minimum set of real-world data
    points the GA gate requires: deployment days, support
    cost, gross-margin signals, SLO attainment, renewal
    intent.

The runbooks themselves live under ``docs/runbooks/``.
"""
from orchestra.ga.evidence import PilotEvidence, collect_pilot_evidence
from orchestra.ga.slo import (
    SLO,
    SLOResult,
    compute_attainment,
    evaluate_ga_readiness,
)

__all__ = [
    "SLO", "SLOResult", "compute_attainment", "evaluate_ga_readiness",
    "PilotEvidence", "collect_pilot_evidence",
]
