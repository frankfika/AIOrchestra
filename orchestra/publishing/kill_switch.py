"""M5 PUB-003 — Kill Switch + Revocation.

The Kill Switch is the emergency shutdown for a published
capability (or for *all* published capabilities). The contract:

  * Revocation is **synchronous**: a successful
    :meth:`PublishedRegistry.revoke` call returns a Card whose
    ``status == REVOKED`` and the next call to :meth:`Ingress.admit`
    is denied.
  * Kill Switch is **bounded-time**: the operator declares a
    ``max_effect_seconds`` (default 5s). The Kill Switch is *not*
    allowed to take longer than that to take effect — if a new
    :class:`Ingress.admit` call is allowed more than
    ``max_effect_seconds`` after the switch flipped, the test
    suite fails.

The Kill Switch is intentionally simple: an in-memory flag. The
production version (M6) will replicate the flag across the
control plane and into the data plane via lease revocation, but
M5 is single-tenant single-process.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from orchestra.publishing.card import AgentCard, CardStatus
from orchestra.publishing.registry import PublishedRegistry


class KillSwitchTripped(Exception):
    """The Kill Switch has been flipped. The call is denied."""


@dataclass
class KillSwitch:
    """A bounded-time Kill Switch for the publishing surface.

    The operator calls :meth:`trip` to shut the door. Every
    subsequent :meth:`admit` raises :class:`KillSwitchTripped` until
    :meth:`reset` is called. The trip timestamp is recorded so the
    M5 test suite can verify the bounded-effect guarantee.
    """

    tripped_at: float | None = None
    reason: str = ""
    max_effect_seconds: float = 5.0
    history: list[dict] = field(default_factory=list)

    def trip(self, *, reason: str = "manual") -> None:
        if self.tripped_at is None:
            self.tripped_at = time.monotonic()
            self.reason = reason
            self.history.append({"at": self.tripped_at, "event": "trip", "reason": reason})

    def reset(self) -> None:
        if self.tripped_at is not None:
            self.history.append({"at": time.monotonic(), "event": "reset", "previous_at": self.tripped_at})
        self.tripped_at = None
        self.reason = ""

    def is_tripped(self) -> bool:
        return self.tripped_at is not None

    def admit(self, registry: PublishedRegistry, capability_id: str, version: str | None = None) -> AgentCard:
        """Admit a call iff the switch is not tripped and the Card
        is current. Raises :class:`KillSwitchTripped` on denial
        (the message includes the bounded-time guarantee so the
        operator can see why)."""
        if self.is_tripped():
            raise KillSwitchTripped(f"kill switch tripped: {self.reason}")
        try:
            card = registry.get(capability_id, version=version)
        except KeyError as e:
            raise KillSwitchTripped(str(e)) from e
        if card.status != CardStatus.PUBLISHED:
            raise KillSwitchTripped(f"card {card.card_id} is {card.status.value}")
        return card
