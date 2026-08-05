"""P0 error types.

``NotInScopeError`` is the marker we raise when a caller tries to use a
component that P0 explicitly defers to a later milestone (see ADR-0002).
This is deliberate: it makes the demo *visibly* honest about what it does
not implement, instead of silently returning a wrong answer.
"""
from __future__ import annotations


class OrchestraError(Exception):
    """Base for all Orchestra-raised errors."""


class NotInScopeError(OrchestraError):
    """Raised when a caller touches a component P0 defers to M1+.

    The message must name the component and the milestone where it becomes
    real. See ``ADR/0002-p0-boundary-and-not-in-scope.md`` for the full list.
    """

    def __init__(self, component: str, milestone: str, hint: str | None = None) -> None:
        self.component = component
        self.milestone = milestone
        msg = (
            f"{component!r} is not in scope for P0. "
            f"It is scheduled to become a real implementation in {milestone}."
        )
        if hint:
            msg += f" Hint: {hint}"
        super().__init__(msg)


class ContractViolation(OrchestraError):
    """Raised when a Task Contract or Capability Manifest violates a frozen
    rule (e.g. unknown Effect, missing Data View, mismatched classification).
    """


class PolicyDenied(OrchestraError):
    """Raised when the in-process OPA-style PDP denies a request.

    The denial is always accompanied by an audit event so reviewers can see
    *why* the path was blocked.
    """


class AdapterError(OrchestraError):
    """Raised when an Adapter call fails (timeout, 4xx/5xx, schema mismatch)."""
