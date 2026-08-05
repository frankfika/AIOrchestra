"""M3 COORD-001 — Zone-aware Artifact Store.

The in-memory implementation; the production version is
Postgres-backed (or S3 + signed URLs). The interface is the
contract M3+ must satisfy.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from orchestra.core.errors import OrchestraError
from orchestra.core.ids import digest_json, new_id


class ArtifactZoneError(OrchestraError):
    """A Cell tried to read an Artifact outside its own Cell."""


@dataclass
class Artifact:
    artifact_id: str
    cell_id: str
    task_run_id: str
    node_id: str
    name: str
    payload: Any
    digest: str
    created_at: float = field(default_factory=time.time)
    size_bytes: int = 0


class ArtifactStore:
    def __init__(self) -> None:
        # The primary key is the (task_run_id, node_id, name) triple.
        # cell_id is the *ownership* tag; the Zone check on read
        # compares the caller's cell_id against the stored artifact's
        # cell_id. Two different cells CAN write to the same triple —
        # this is the cross-cell collision case the Zone boundary
        # refuses to read.
        self._by_triple: dict[tuple[str, str, str], Artifact] = {}
        self._by_digest: dict[str, Artifact] = {}

    def put(
        self,
        *,
        cell_id: str,
        task_run_id: str,
        node_id: str,
        name: str,
        payload: Any,
    ) -> Artifact:
        serialised = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        digest = digest_json(serialised)
        a = Artifact(
            artifact_id=new_id(),
            cell_id=cell_id,
            task_run_id=task_run_id,
            node_id=node_id,
            name=name,
            payload=payload,
            digest=digest,
            size_bytes=len(serialised.encode("utf-8")),
        )
        self._by_triple[(task_run_id, node_id, name)] = a
        # Digest index points to the most recent artifact with that
        # content-addressed digest. Multiple cells writing the same
        # payload simply overwrite the digest pointer; the per-triple
        # table keeps only the latest cell's version.
        self._by_digest[digest] = a
        return a

    def get(
        self,
        *,
        cell_id: str,
        task_run_id: str,
        node_id: str,
        name: str,
    ) -> Artifact:
        a = self._by_triple.get((task_run_id, node_id, name))
        if a is None:
            raise KeyError(f"artifact not found: {task_run_id}/{node_id}/{name}")
        if a.cell_id != cell_id:
            raise ArtifactZoneError(
                f"cell {cell_id} cannot read artifact owned by cell {a.cell_id}"
            )
        return a

    def get_by_digest(self, digest: str) -> Artifact:
        return self._by_digest[digest]
