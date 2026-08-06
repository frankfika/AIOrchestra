"""M3 COORD-001 — Artifact Manager (Zone-aware storage).

A :class:`ArtifactStore` is the in-M3 substitute for a real
Zone-aware artifact store. Each Artifact is identified by
``(cell_id, task_run_id, node_id, name)`` and stores a
content-addressed payload.

The Coordinator writes Artifacts as Node Runs complete; the
Adapter reads them with the Node Grant as authorisation. The
Artifact Manager enforces the *Zone* boundary: a Cell can
only read Artifacts in its own Cell.
"""
from orchestra.artifact.store import Artifact, ArtifactStore

__all__ = ["ArtifactStore", "Artifact"]
