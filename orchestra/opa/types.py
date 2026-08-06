"""M1 POL-001 — OPA wire types.

OPA returns a JSON shape::

    {
      "result": {
        "allow": true,
        "reason": "...",
        "invariant": "1",
        "rule_id": "no-restricted-to-public"
      }
    }

This module re-exports the Python-side types so callers can
import them from a single place.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OpaConfig:
    base_url: str = "http://127.0.0.1:8181"
    package: str = "orchestra"
    timeout_seconds: float = 2.0
    kid: str = "p1-opa-key"


@dataclass
class OpaDecision:
    allow: bool
    reason: str
    invariant: str
    rule_id: str
