"""M2 EVD-001 — Merkle log per Cell.

The M0 Event Store is a flat, append-only table. M2 chains
events into a per-Cell Merkle log so any third party can prove
inclusion (Merkle inclusion proof) and the Cell cannot rewrite
history (Merkle consistency proof).

The M2 implementation is deliberately simple — a SHA-256
binary tree over the Event IDs, recomputed in memory. M3
swaps this for a real Merkle store (Postgres-backed or a
dedicated log like Trillian). The *interface* is the contract
M3+ must satisfy.
"""
from orchestra.evidence.merkle import MerkleLog, MerkleProof, verify_inclusion_proof

__all__ = ["MerkleLog", "MerkleProof", "verify_inclusion_proof"]
