"""M4 — Delegation contract.

The three integration modes the white paper / dev plan §M4 require:

  * ``delegate-task``   — Orchestra owns the entire task lifecycle.
                         The host platform (Dify / AgenticHub / …) only
                         calls in once with the inputs and waits for a
                         final result.
  * ``delegate-node``   — Orchestra owns *one* node (or a sub-graph).
                         The host platform drives the surrounding
                         workflow; Orchestra publishes progress and
                         final state for *its* slice.
  * ``observe-only``    — The host platform runs the task itself; the
                         Orchestra Adapter is a witness that publishes
                         audit events but does **not** gate execution.

Each mode pins five ownership slots so the host and Orchestra never
disagree about who does what:

  * ``execution_owner``      — who runs the actual work
  * ``idempotency_owner``    — who deduplicates retries
  * ``retry_owner``          — who decides when to retry
  * ``cancel_owner``         — who propagates cancellation
  * ``final_state_authority``— whose state is canonical

The contract is intentionally tiny so the platform SDK can render it
as a static tool spec without runtime introspection.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal


class DelegationMode(str, Enum):
    DELEGATE_TASK = "delegate-task"
    DELEGATE_NODE = "delegate-node"
    OBSERVE_ONLY = "observe-only"


class IntegrationLevel(str, Enum):
    """How strongly Orchestra governs the integration."""

    ENFORCE = "enforce"      # the platform cannot bypass the policy
    RECOMMEND = "recommend"  # Orchestra suggests; platform may override
    OBSERVE = "observe"      # Orchestra records only; platform owns policy


_OWNER_LITERAL = Literal["orchestra", "host"]


@dataclass(frozen=True)
class DelegationContract:
    """The fixed delegation contract for one integration.

    The three modes each pin all five ownership slots. The host SDK
    surfaces this struct to the user as a tooltip so they know who
    is doing what without having to read the source.
    """

    mode: DelegationMode
    execution_owner: _OWNER_LITERAL
    idempotency_owner: _OWNER_LITERAL
    retry_owner: _OWNER_LITERAL
    cancel_owner: _OWNER_LITERAL
    final_state_authority: _OWNER_LITERAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "execution_owner": self.execution_owner,
            "idempotency_owner": self.idempotency_owner,
            "retry_owner": self.retry_owner,
            "cancel_owner": self.cancel_owner,
            "final_state_authority": self.final_state_authority,
        }


# ---------------------------------------------------------------------------
# The three fixed contracts
# ---------------------------------------------------------------------------

DELEGATE_TASK = DelegationContract(
    mode=DelegationMode.DELEGATE_TASK,
    execution_owner="orchestra",
    idempotency_owner="orchestra",
    retry_owner="orchestra",
    cancel_owner="orchestra",
    final_state_authority="orchestra",
)

DELEGATE_NODE = DelegationContract(
    mode=DelegationMode.DELEGATE_NODE,
    execution_owner="orchestra",
    idempotency_owner="host",
    retry_owner="host",
    cancel_owner="host",
    final_state_authority="host",
)

OBSERVE_ONLY = DelegationContract(
    mode=DelegationMode.OBSERVE_ONLY,
    execution_owner="host",
    idempotency_owner="host",
    retry_owner="host",
    cancel_owner="host",
    final_state_authority="host",
)


def contract_for_mode(mode: DelegationMode) -> DelegationContract:
    """Return the canonical contract for ``mode``.

    Raises ``ValueError`` if the mode is unknown — caller is expected
    to validate before the Adapter is built. Keeping this pure makes
    the contract trivially testable.
    """
    if mode == DelegationMode.DELEGATE_TASK:
        return DELEGATE_TASK
    if mode == DelegationMode.DELEGATE_NODE:
        return DELEGATE_NODE
    if mode == DelegationMode.OBSERVE_ONLY:
        return OBSERVE_ONLY
    raise ValueError(f"unknown delegation mode: {mode!r}")


def governance_state_for(
    *,
    mode: DelegationMode,
    task_state: str,
    plan_id: str | None,
    audit_url: str,
    route_url: str,
    error: str | None = None,
) -> dict[str, Any]:
    """Build the governance-state payload the host platform renders.

    The shape is stable across modes; ``delegation`` is the only field
    that varies. Host platforms (Dify, AgenticHub) consume this as the
    tool's structured output.
    """
    contract = contract_for_mode(mode)
    return {
        "state": task_state,
        "plan_id": plan_id,
        "audit_url": audit_url,
        "route_url": route_url,
        "delegation": contract.to_dict(),
        "error": error or "",
    }
