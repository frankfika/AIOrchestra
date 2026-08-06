"""M2 EVD-001 — Per-Cell Merkle log over event IDs.

Each Cell (tenant) has a :class:`MerkleLog`. Appending an
``event_id`` produces a new root; the log can produce an
:class:`MerkleProof` for any past event so an Auditor can
verify inclusion without the Cell.

Production replaces this with a real Merkle store; the
interface is the contract M3+ must satisfy.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


def _h(*parts: bytes) -> str:
    return hashlib.sha256(b"|".join(parts)).hexdigest()


@dataclass
class MerkleProof:
    """Inclusion proof for an event in a Merkle log.

    ``siblings`` is the list of sibling hashes from the leaf
    to the root, paired with ``positions`` (``L`` = left,
    ``R`` = right) so the verifier knows which side to put
    each sibling on.
    """

    leaf_index: int
    leaf_hash: str
    siblings: list[str]
    positions: list[Literal[L, R]] = field(default_factory=list)  # type: ignore[name-defined]
    root: str = ""

    def to_dict(self) -> dict:
        return {
            "leaf_index": self.leaf_index,
            "leaf_hash": self.leaf_hash,
            "siblings": self.siblings,
            "positions": self.positions,
            "root": self.root,
        }


class MerkleLog:
    """In-memory Merkle log over event IDs (SHA-256)."""

    def __init__(self) -> None:
        self._leaves: list[str] = []

    def __len__(self) -> int:
        return len(self._leaves)

    def root(self) -> str:
        return self._compute_root(self._leaves)

    def append(self, event_id: str) -> str:
        self._leaves.append(_h(event_id.encode("utf-8")))
        return self.root()

    def inclusion_proof(self, leaf_index: int) -> MerkleProof:
        if not 0 <= leaf_index < len(self._leaves):
            raise IndexError(f"leaf_index {leaf_index} out of range")
        siblings: list[str] = []
        positions: list[str] = []
        layer = list(self._leaves)
        idx = leaf_index
        while len(layer) > 1:
            sibling_idx = idx ^ 1
            if sibling_idx >= len(layer):
                # Pad with the last element.
                sibling = layer[-1]
            else:
                sibling = layer[sibling_idx]
            siblings.append(sibling)
            positions.append("L" if sibling_idx < idx else "R")
            # Build the next layer.
            next_layer: list[str] = []
            for i in range(0, len(layer), 2):
                if i + 1 < len(layer):
                    next_layer.append(_h(layer[i].encode(), layer[i + 1].encode()))
                else:
                    next_layer.append(_h(layer[i].encode(), layer[i].encode()))
            layer = next_layer
            idx //= 2
        return MerkleProof(
            leaf_index=leaf_index,
            leaf_hash=self._leaves[leaf_index],
            siblings=siblings,
            positions=positions,
            root=self.root(),
        )

    @staticmethod
    def _compute_root(leaves: list[str]) -> str:
        if not leaves:
            return _h(b"")
        layer = list(leaves)
        while len(layer) > 1:
            next_layer: list[str] = []
            for i in range(0, len(layer), 2):
                if i + 1 < len(layer):
                    next_layer.append(_h(layer[i].encode(), layer[i + 1].encode()))
                else:
                    next_layer.append(_h(layer[i].encode(), layer[i].encode()))
            layer = next_layer
        return layer[0]


def verify_inclusion_proof(proof: MerkleProof) -> bool:
    """Verify a :class:`MerkleProof` against its declared root."""
    current = proof.leaf_hash
    for sib, pos in zip(proof.siblings, proof.positions):
        if pos == "L":
            current = _h(sib.encode(), current.encode())
        else:
            current = _h(current.encode(), sib.encode())
    return current == proof.root
