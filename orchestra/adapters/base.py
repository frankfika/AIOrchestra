"""Adapter interface for P0.

An Adapter is *anything* that can take an :class:`AdapterRequest` (which
contains a :class:`NodeGrant`, the previous node's outputs, and a timeout)
and return an :class:`AdapterResult`. The Coordinator doesn't care about
the underlying protocol — it sees the same shape for Local, Public, A2A,
and Sink.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from orchestra.core.errors import AdapterError
from orchestra.core.schema import DataView, NodeGrant, Purpose


@dataclass
class AdapterRequest:
    grant: NodeGrant
    inputs: dict[str, Any]
    data_view: DataView
    purpose: Purpose
    timeout_ms: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AdapterResult:
    outputs: dict[str, Any]
    raw: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


class Adapter(Protocol):
    name: str

    async def invoke(self, request: AdapterRequest) -> AdapterResult: ...


# Re-export the error so callers can ``from orchestra.adapters import AdapterError``
AdapterError = AdapterError
