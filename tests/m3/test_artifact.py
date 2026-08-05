"""M3 COORD-001 — ArtifactStore (Zone-aware) tests.

The COORD-001 invariant: a Cell may only read Artifacts it owns.
Cross-cell reads raise :class:`ArtifactZoneError`.
"""
from __future__ import annotations

import pytest

from orchestra.artifact.store import ArtifactStore, ArtifactZoneError
from orchestra.core.errors import OrchestraError


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_artifact_store_put_and_get():
    store = ArtifactStore()
    a = store.put(
        cell_id="cell-a", task_run_id="trun-1", node_id="node-extract",
        name="facts", payload={"vendor": "Acme", "amount": 100},
    )
    assert a.cell_id == "cell-a"
    assert a.payload == {"vendor": "Acme", "amount": 100}
    assert a.size_bytes > 0
    assert a.digest

    b = store.get(
        cell_id="cell-a", task_run_id="trun-1", node_id="node-extract",
        name="facts",
    )
    assert b.artifact_id == a.artifact_id
    assert b.payload == a.payload


def test_artifact_store_get_by_digest():
    store = ArtifactStore()
    a = store.put(
        cell_id="cell-a", task_run_id="trun-1", node_id="n", name="x",
        payload={"k": "v"},
    )
    via_digest = store.get_by_digest(a.digest)
    assert via_digest.artifact_id == a.artifact_id


def test_artifact_store_dedup_index_by_digest():
    """Same payload -> same digest; index updates to the latest artifact_id
    for that digest, but the per-key table keeps every artifact."""
    store = ArtifactStore()
    a = store.put(cell_id="c1", task_run_id="t", node_id="n", name="k", payload={"v": 1})
    b = store.put(cell_id="c2", task_run_id="t", node_id="n", name="k", payload={"v": 1})
    assert a.digest == b.digest  # content-addressed
    assert a.artifact_id != b.artifact_id  # but distinct rows
    # The digest index points to the *latest* put.
    assert store.get_by_digest(a.digest).artifact_id == b.artifact_id


# ---------------------------------------------------------------------------
# Cross-cell Zone boundary (the COORD-001 invariant)
# ---------------------------------------------------------------------------


def test_artifact_store_cross_cell_read_raises_zone_error():
    store = ArtifactStore()
    store.put(
        cell_id="cell-tenant-a", task_run_id="trun-1", node_id="node-extract",
        name="facts", payload={"vendor": "Acme"},
    )
    with pytest.raises(ArtifactZoneError) as ei:
        store.get(
            cell_id="cell-tenant-b",  # different Cell!
            task_run_id="trun-1", node_id="node-extract", name="facts",
        )
    assert "tenant-b" in str(ei.value) or "cell-tenant-b" in str(ei.value)
    assert "cell-tenant-a" in str(ei.value)


def test_artifact_zone_error_is_orchestra_error():
    """ArtifactZoneError must be catchable as OrchestraError so callers
    can use one except clause for governance failures."""
    store = ArtifactStore()
    store.put(cell_id="a", task_run_id="t", node_id="n", name="x", payload={})
    with pytest.raises(OrchestraError):
        store.get(cell_id="b", task_run_id="t", node_id="n", name="x")


def test_artifact_store_unknown_artifact_raises_keyerror():
    store = ArtifactStore()
    with pytest.raises(KeyError):
        store.get(cell_id="c", task_run_id="t", node_id="n", name="missing")


def test_artifact_store_zone_check_only_triggers_on_cross_cell():
    """Sanity check: same-cell read of an existing artifact does not
    raise ArtifactZoneError, even if the lookup is by digest afterwards."""
    store = ArtifactStore()
    a = store.put(
        cell_id="cell-a", task_run_id="trun-1", node_id="node-x",
        name="result", payload={"x": 1},
    )
    # Same cell: succeeds.
    out = store.get(cell_id="cell-a", task_run_id="trun-1", node_id="node-x", name="result")
    assert out.artifact_id == a.artifact_id
    # By digest: succeeds regardless of cell (it's a content-addressed index).
    via_d = store.get_by_digest(a.digest)
    assert via_d.artifact_id == a.artifact_id


def test_artifact_store_size_bytes_reflects_payload():
    store = ArtifactStore()
    a = store.put(
        cell_id="c", task_run_id="t", node_id="n", name="big",
        payload={"k": "x" * 200},
    )
    assert a.size_bytes > 200
