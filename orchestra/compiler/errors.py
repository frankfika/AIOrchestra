"""M1 — Compile errors.

A :class:`CompileError` is what the Trust Compiler raises (or
returns in a :class:`CompileResult`) when a Plan violates an
invariant. It carries:
  - the kind (an enum mapped to an invariant number)
  - the offending node id
  - the data path the Compiler was walking when it found the
    violation (the counter-example input for CMP-003)
  - a human-readable reason
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CompileErrorKind(str, Enum):
    """Each kind maps to one of the 26 security invariants (SEC-002).

    The M1 Trust Compiler only emits errors for the invariants it
    enforces at compile time; the Coordinator's runtime checks
    handle the rest.
    """

    # Information-flow
    RESTRICTED_TO_PUBLIC = "restricted-to-public"        # invariant #1
    INTERNAL_LEAKS_VIA_NON_PUBLIC_NODE = "internal-leaks-via-non-public-node"  # #1
    TRUST_NOT_INHERITED = "trust-not-inherited"          # invariant #16

    # Effect / approval
    HIGH_RISK_EFFECT_WITHOUT_APPROVAL = "high-risk-effect-without-approval"  # #7
    EFFECT_ESCALATION = "effect-escalation"               # #3 / #20

    # Type / shape
    UNKNOWN_NODE = "unknown-node"                        # CMP-001
    UNKNOWN_EDGE = "unknown-edge"                        # CMP-001
    CYCLE = "cycle"                                       # CMP-001
    VIEW_MISMATCH = "view-mismatch"                      # SPEC-001
    MISSING_REQUIREMENT = "missing-requirement"          # SPEC-001

    # Delegation
    PURPOSE_ESCALATION = "purpose-escalation"            # #5 / #20
    AUDIENCE_ESCALATION = "audience-escalation"          # #5 / #20

    # Binding closure
    AMBIENT_AUTHORITY = "ambient-authority"              # #20
    INFEASIBLE_BINDING = "infeasible-binding"            # BND-001

    def invariant(self) -> str:
        return _KIND_TO_INVARIANT.get(self, "0")


_KIND_TO_INVARIANT: dict[CompileErrorKind, str] = {
    CompileErrorKind.RESTRICTED_TO_PUBLIC: "1",
    CompileErrorKind.INTERNAL_LEAKS_VIA_NON_PUBLIC_NODE: "1",
    CompileErrorKind.TRUST_NOT_INHERITED: "16",
    CompileErrorKind.HIGH_RISK_EFFECT_WITHOUT_APPROVAL: "7",
    CompileErrorKind.EFFECT_ESCALATION: "20",
    CompileErrorKind.UNKNOWN_NODE: "0",
    CompileErrorKind.UNKNOWN_EDGE: "0",
    CompileErrorKind.CYCLE: "0",
    CompileErrorKind.VIEW_MISMATCH: "0",
    CompileErrorKind.MISSING_REQUIREMENT: "0",
    CompileErrorKind.PURPOSE_ESCALATION: "5",
    CompileErrorKind.AUDIENCE_ESCALATION: "5",
    CompileErrorKind.AMBIENT_AUTHORITY: "20",
    CompileErrorKind.INFEASIBLE_BINDING: "20",
}


@dataclass
class CompileError:
    kind: CompileErrorKind
    node_id: str
    reason: str
    data_path: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def invariant(self) -> str:
        return self.kind.invariant()

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "invariant": self.invariant,
            "node_id": self.node_id,
            "reason": self.reason,
            "data_path": self.data_path,
            "details": self.details,
        }
