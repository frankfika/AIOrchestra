"""M7 — SLO calculator.

The M7 GA gate requires real SLO attainment numbers. This module
turns raw pilot telemetry (success / latency / error counts) into
the four SLOs the dev plan §M7 lists:

  * ``availability``  — % of pilot tasks that completed in a
                        terminal state without operator intervention
  * ``latency_p95``   — p95 of the per-task wall-clock time
  * ``recovery_time`` — p95 of time-to-recovery from a failed
                        pilot task to the next succeeded pilot task
  * ``rpo``           — Recovery Point Objective: max audit-trail
                        gap (in seconds) during a HA/DR drill

``compute_attainment`` returns one :class:`SLOResult` per SLO; the
result's ``attainment`` is a float in ``[0, 1]`` and ``meets``
mirrors the operator's configured target.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass
class SLO:
    """One Service Level Objective the GA gate measures."""

    name: str
    target: float           # e.g. 0.999 for availability
    comparator: str = "ge"  # 'ge' (>=) or 'le' (<=)
    unit: str = ""          # e.g. 's' for latency


@dataclass
class SLOResult:
    """The measured attainment for one :class:`SLO`."""

    name: str
    target: float
    measured: float
    attainment: float  # measured / target (clipped to [0, 1] for ge, else mirrored)
    meets: bool

    def to_dict(self) -> dict[str, float | str | bool]:
        return {
            "name": self.name,
            "target": self.target,
            "measured": self.measured,
            "attainment": self.attainment,
            "meets": self.meets,
        }


# ---------------------------------------------------------------------------
# SLO computation
# ---------------------------------------------------------------------------


def availability_slo(*, succeeded: int, total: int, target: float = 0.999) -> SLOResult:
    """``succeeded / total`` against the availability target.

    Operators set ``target`` to 0.999 for "three nines" or 0.9999
    for "four nines". A pilot with fewer than 100 tasks is too
    small for an availability number to be meaningful; the M7 gate
    flags it as ``meets=False`` with a reason in the attainment.
    """
    if total == 0:
        return SLOResult("availability", target, 0.0, 0.0, False)
    if total < 100:
        # Not enough data; report the raw number but mark as not
        # meeting the gate until the pilot has 100+ tasks.
        return SLOResult("availability", target, succeeded / total, 0.0, False)
    measured = succeeded / total
    return SLOResult("availability", target, measured, min(1.0, measured / target), measured >= target)


def latency_p95_slo(*, samples_ms: Iterable[float], target_ms: float) -> SLOResult:
    """p95 of the per-task wall-clock latency against a target."""
    samples = sorted(samples_ms)
    if not samples:
        return SLOResult("latency_p95_ms", target_ms, float("inf"), 0.0, False)
    # Pure-Python percentile (no numpy dep).
    idx = max(0, math.ceil(0.95 * len(samples)) - 1)
    p95 = samples[idx]
    return SLOResult("latency_p95_ms", target_ms, p95, min(1.0, target_ms / p95) if p95 > 0 else 0.0, p95 <= target_ms)


def recovery_time_slo(*, recovery_intervals_s: Iterable[float], target_s: float) -> SLOResult:
    """p95 of time-to-recovery after a failed task."""
    samples = sorted(recovery_intervals_s)
    if not samples:
        return SLOResult("recovery_time_p95_s", target_s, 0.0, 1.0, True)  # no failures yet
    idx = max(0, math.ceil(0.95 * len(samples)) - 1)
    p95 = samples[idx]
    return SLOResult("recovery_time_p95_s", target_s, p95, min(1.0, target_s / p95) if p95 > 0 else 0.0, p95 <= target_s)


def rpo_slo(*, audit_gap_seconds: float, target_s: float) -> SLOResult:
    """Recovery Point Objective: the longest audit-trail gap
    observed during a HA/DR drill. Lower is better."""
    if target_s <= 0:
        return SLOResult("rpo_s", target_s, audit_gap_seconds, 0.0, False)
    attainment = min(1.0, target_s / audit_gap_seconds) if audit_gap_seconds > 0 else 1.0
    return SLOResult("rpo_s", target_s, audit_gap_seconds, attainment, audit_gap_seconds <= target_s)


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


@dataclass
class PilotTelemetry:
    """The minimum telemetry the M7 gate needs from a pilot."""

    succeeded_tasks: int = 0
    total_tasks: int = 0
    latency_samples_ms: list[float] = field(default_factory=list)
    recovery_intervals_s: list[float] = field(default_factory=list)
    audit_gap_seconds: float = 0.0
    # Operator-set SLO targets.
    availability_target: float = 0.999
    latency_p95_target_ms: float = 5000.0
    recovery_p95_target_s: float = 60.0
    rpo_target_s: float = 60.0


@dataclass
class GAReadiness:
    """The composite GA-readiness verdict.

    A pilot is GA-ready if **all** configured SLOs meet their
    targets AND the deployment day count exceeds the M7 minimum.
    """

    slo_results: list[SLOResult]
    all_slos_meet: bool
    deployment_days: int
    minimum_deployment_days: int
    ga_ready: bool
    blockers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "slo_results": [r.to_dict() for r in self.slo_results],
            "all_slos_meet": self.all_slos_meet,
            "deployment_days": self.deployment_days,
            "minimum_deployment_days": self.minimum_deployment_days,
            "ga_ready": self.ga_ready,
            "blockers": self.blockers,
        }


def compute_attainment(t: PilotTelemetry) -> list[SLOResult]:
    """Compute all four SLO attainments for a pilot's telemetry."""
    return [
        availability_slo(succeeded=t.succeeded_tasks, total=t.total_tasks, target=t.availability_target),
        latency_p95_slo(samples_ms=t.latency_samples_ms, target_ms=t.latency_p95_target_ms),
        recovery_time_slo(recovery_intervals_s=t.recovery_intervals_s, target_s=t.recovery_p95_target_s),
        rpo_slo(audit_gap_seconds=t.audit_gap_seconds, target_s=t.rpo_target_s),
    ]


def evaluate_ga_readiness(
    t: PilotTelemetry, *, deployment_days: int, minimum_deployment_days: int = 14
) -> GAReadiness:
    """Decide whether a pilot is GA-ready.

    A pilot is GA-ready when:
      * All four SLOs meet their targets.
      * The pilot has been deployed for at least
        ``minimum_deployment_days`` (default 14, the M7 minimum).
    """
    results = compute_attainment(t)
    blockers: list[str] = []
    for r in results:
        if not r.meets:
            blockers.append(f"{r.name}: measured={r.measured}, target={r.target}")
    if deployment_days < minimum_deployment_days:
        blockers.append(
            f"deployment_days {deployment_days} < minimum {minimum_deployment_days}"
        )
    return GAReadiness(
        slo_results=results,
        all_slos_meet=all(r.meets for r in results),
        deployment_days=deployment_days,
        minimum_deployment_days=minimum_deployment_days,
        ga_ready=not blockers,
        blockers=blockers,
    )
