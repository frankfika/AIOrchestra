"""In-memory Capability Manifest store.

P0 is single-tenant and static, so the store is just a dict keyed by
``capability_id``. M0/M1 will add versioning and snapshot isolation; for
P0, every manifest loaded is the live one.
"""
from __future__ import annotations

from typing import Iterable

from orchestra.core.errors import ContractViolation
from orchestra.core.schema import CapabilityManifest


class ManifestStore:
    def __init__(self, manifests: Iterable[CapabilityManifest] | None = None) -> None:
        self._by_id: dict[str, CapabilityManifest] = {}
        if manifests:
            for m in manifests:
                self.add(m)

    def add(self, manifest: CapabilityManifest) -> None:
        if manifest.capability_id in self._by_id:
            raise ContractViolation(
                f"capability_id {manifest.capability_id!r} already registered"
            )
        self._by_id[manifest.capability_id] = manifest

    def get(self, capability_id: str) -> CapabilityManifest:
        try:
            return self._by_id[capability_id]
        except KeyError as e:
            raise ContractViolation(f"unknown capability_id {capability_id!r}") from e

    def all(self) -> list[CapabilityManifest]:
        return list(self._by_id.values())

    def by_kind(self, kind: str) -> list[CapabilityManifest]:
        return [m for m in self._by_id.values() if m.kind.value == kind]

    def __len__(self) -> int:
        return len(self._by_id)

    def __contains__(self, capability_id: object) -> bool:
        return capability_id in self._by_id
