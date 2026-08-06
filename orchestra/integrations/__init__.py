"""M4 INT-* — Platform Integration Adapters.

The :mod:`orchestra.integrations` package bundles the reference
adapters that surface Orchestra to upstream Agent platforms (Dify,
AgenticHub, custom HTTP callers). The contract is:

  * We do **not** modify the upstream platform's core. Integration is
    *only* through Task Tool, API config, Endpoint/Proxy/Sidecar, or
    an Adapter that the platform's plugin SDK calls.
  * Each integration declares an
    :class:`~orchestra.integrations.delegation.DelegationMode` so the
    host platform knows who owns execution / idempotency / retry /
    cancel propagation.
  * Each integration records its
    :class:`~orchestra.integrations.delegation.IntegrationLevel`
    (``enforce`` / ``recommend`` / ``observe``) so consumers know how
    strongly Orchestra governs the call.
"""
from orchestra.integrations.delegation import (
    DelegationContract,
    DelegationMode,
    IntegrationLevel,
)

__all__ = ["DelegationMode", "DelegationContract", "IntegrationLevel"]
