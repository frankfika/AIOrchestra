"""M1 POL-001 — PDP interface, OPA HTTP client, in-process default."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from orchestra.core.errors import OrchestraError
from orchestra.registry.policy import (
    PolicyDecision,
    PolicyEngine,
    PolicyRequest,
    default_p0_rules,
)


class OPAUnavailable(OrchestraError):
    """Raised when the OPA backend is unreachable. Callers must fail-closed."""


class PolicyDecisionPoint(Protocol):
    """The PDP contract.

    A PDP turns a :class:`PolicyRequest` into a
    :class:`PolicyDecision`. M1 ships two implementations: the
    in-process engine (default) and the OPA HTTP client.
    """

    def decide(self, request: PolicyRequest) -> PolicyDecision: ...


@dataclass
class InProcessPDP:
    """The P0 in-process engine wrapped as a PDP.

    Kept as a thin wrapper so the swap is mechanical: replace
    ``InProcessPDP()`` with ``OpaHttpClient(config)`` and the
    rest of the system is unchanged.
    """

    rules: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        self._engine = PolicyEngine(self.rules if self.rules is not None else default_p0_rules())

    def decide(self, request: PolicyRequest) -> PolicyDecision:
        return self._engine.decide(request)


@dataclass
class OpaConfig:
    base_url: str = "http://127.0.0.1:8181"
    package: str = "orchestra"
    timeout_seconds: float = 2.0
    kid: str = "p1-opa-key"


class OpaHttpClient:
    """A real Open Policy Agent (OPA) HTTP client.

    Calls ``POST {base_url}/v1/data/{package}/allow`` with the
    request payload. The Rego module must define a rule
    ``allow`` that returns ``{"allow": bool, "reason": str,
    "invariant": str, "rule_id": str}``.

    Fail-closed semantics: a timeout, 5xx, or malformed
    response raises :class:`OPAUnavailable`. The Coordinator /
    Compiler catches this and treats the request as denied.
    """

    def __init__(self, config: OpaConfig | None = None) -> None:
        self._config = config or OpaConfig()
        self._client = httpx.Client(timeout=self._config.timeout_seconds)

    def decide(self, request: PolicyRequest) -> PolicyDecision:
        url = f"{self._config.base_url}/v1/data/{self._config.package}/allow"
        payload = {
            "input": {
                "node_id": request.node.node_id,
                "node_requires_approval": request.node.requires_approval,
                "node_eligible_kinds": [k.value for k in request.node.eligible_capability_kinds],
                "node_effects": [e.model_dump() for e in request.node.declared_effects],
                "capability_id": request.capability.capability_id,
                "capability_kind": request.capability.kind.value,
                "capability_effects": [e.model_dump() for e in request.capability.declared_effects],
                "data_classification": request.data_label.classification.value,
                "data_residency": request.data_label.residency,
                "purpose_code": request.purpose_code,
                "region": request.region,
                "budget_remaining_usd": request.budget_remaining_usd,
            }
        }
        try:
            r = self._client.post(url, json=payload)
        except httpx.HTTPError as e:
            raise OPAUnavailable(f"OPA HTTP error: {e}") from e
        if r.status_code != 200:
            raise OPAUnavailable(
                f"OPA returned {r.status_code}: {r.text[:200]}"
            )
        try:
            data = r.json().get("result", {})
        except Exception as e:  # noqa: BLE001
            raise OPAUnavailable(f"OPA response not JSON: {e}") from e
        if not isinstance(data, dict) or "allow" not in data:
            raise OPAUnavailable(f"OPA response shape invalid: {data}")
        return PolicyDecision(
            allow=bool(data["allow"]),
            reason=str(data.get("reason", "")),
            invariant=str(data.get("invariant", "")),
            rule_id=str(data.get("rule_id", "")),
        )

    def health(self) -> dict[str, Any]:
        url = f"{self._config.base_url}/health"
        try:
            r = self._client.get(url)
        except httpx.HTTPError as e:
            raise OPAUnavailable(f"OPA health error: {e}") from e
        return {"status_code": r.status_code, "body": r.text[:200]}


@dataclass
class OpaDecision:
    """Wire-level response from OPA. Mirrors :class:`PolicyDecision`."""

    allow: bool
    reason: str
    invariant: str
    rule_id: str
