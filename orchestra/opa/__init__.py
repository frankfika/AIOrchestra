"""M1 POL-001 — replaceable Policy Decision Point (PDP) backend.

P0 ships an in-process Rego-like engine (see
:mod:`orchestra.registry.policy`). M1 adds a *replaceable*
PDP interface and a real Open Policy Agent (OPA) HTTP client.

The PDP contract is:
  Input:  :class:`PolicyRequest`
  Output: :class:`PolicyDecision`

The in-process engine is the default. The OPA HTTP client calls
``POST {OPA_BASE_URL}/v1/data/orchestra/allow`` (the
``orchestra`` package is a Rego module the OPA bundle ships).
If OPA is unreachable, the call raises :class:`OPAUnavailable`;
the Caller is expected to **fail-closed** (invariant #8).
"""
from orchestra.opa.interface import (
    PolicyDecisionPoint,
    OPAUnavailable,
    OpaHttpClient,
    InProcessPDP,
)
from orchestra.opa.types import OpaConfig, OpaDecision

__all__ = [
    "PolicyDecisionPoint",
    "OPAUnavailable",
    "OpaHttpClient",
    "InProcessPDP",
    "OpaConfig",
    "OpaDecision",
]
