"""M5 — Publishing Preview (PUB-001/002/003 + REL-001).

The Publishing module is the surface for capabilities a tenant
chooses to *expose* to a single, named partner under a Partner
Contract. The P0/M0–M4 code path is for *consuming* capabilities;
M5 introduces the *publishing* side.

Sub-modules:

  * :mod:`orchestra.publishing.card`  — signed Agent Cards (PUB-001)
  * :mod:`orchestra.publishing.contract` — Partner Contract (PUB-002)
  * :mod:`orchestra.publishing.registry` — published-capability store
                                       with version + revocation
  * :mod:`orchestra.publishing.ingress` — Ingress Identity verification
  * :mod:`orchestra.publishing.release_gate` — Output / Citation
                                            Release Gate (REL-001)
  * :mod:`orchestra.publishing.kill_switch` — Revocation + Kill Switch
                                            (PUB-003)

M5 is single-partner and isolated by design. Multi-tenant open
publishing is M6 territory.
"""
from orchestra.publishing.card import AgentCard, sign_card, verify_card
from orchestra.publishing.contract import AudienceSpec, PartnerContract
from orchestra.publishing.kill_switch import KillSwitch
from orchestra.publishing.registry import PublishedRegistry
from orchestra.publishing.release_gate import ReleaseGate

__all__ = [
    "AgentCard", "sign_card", "verify_card",
    "PartnerContract", "AudienceSpec",
    "PublishedRegistry",
    "KillSwitch",
    "ReleaseGate",
]
