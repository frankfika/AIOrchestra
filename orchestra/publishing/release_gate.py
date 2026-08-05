"""M5 REL-001 — Output / Citation Release Gate.

The Release Gate is the last line of defence before a structured
result leaves the tenant for a partner. It enforces:

  * the result is **structured** (not free-text). M5 supports a
    claim list + citation list shape; free-text blobs are refused.
  * every claim has **at least one citation** that points at an
    allowed source (node output, partner-supplied, synthetic).
  * no citation's source carries a higher classification than the
    claim's audience allows. (Restricted citations are *never*
    allowed to be released to a partner; they fail the gate.)
  * the result does not contain any ``error`` / ``trace`` /
    ``stacktrace`` field — those are not "structured output" and
    would leak internal diagnostics.

The Gate is intentionally cheap: a single pass over the result
plus its citation manifest. M5 is in-process; M6 will swap the
manifest store for the Postgres-backed one without changing the
interface.
"""
from __future__ import annotations

from typing import Any, Iterable

from orchestra.core.errors import OrchestraError
from orchestra.core.schema import Citation, CitationManifest, CitationSourceRef
from orchestra.publishing.card import AgentCard


class ReleaseDenied(OrchestraError):
    """The Release Gate refused to publish the result."""


# The four forbidden top-level keys. A partner that gets a result
# with any of these in it can reverse-engineer internal state.
_FORBIDDEN_KEYS = frozenset({
    "error", "errors", "trace", "traces", "stacktrace", "stack",
    "internal_id", "raw_payload",
})


class ReleaseGate:
    """The M5 Output / Citation Release Gate.

    Built around the :class:`CitationManifest` schema the M0 freeze
    already defined. The Gate's job is to refuse any release that
    would leak a higher-tier label than the Card's audience allows.
    """

    def __init__(self, *, card: AgentCard, max_unsourced_claims: int = 0) -> None:
        self._card = card
        self._max_unsourced = max_unsourced_claims

    @property
    def card(self) -> AgentCard:
        return self._card

    def release(self, result: dict[str, Any], manifest: CitationManifest) -> dict[str, Any]:
        """Validate ``result`` + its ``manifest`` against the Card's
        audience and the forbidden-key list.

        On success, returns the result unchanged (the Gate is
        transparent). On denial, raises :class:`ReleaseDenied` with
        a precise reason.
        """
        # 1. Structure check: result must be a dict with a
        #    ``claims`` list. Free-text is not a release.
        if not isinstance(result, dict):
            raise ReleaseDenied("result must be a dict, not free text")
        claims = result.get("claims")
        if not isinstance(claims, list):
            raise ReleaseDenied("result.claims must be a list (structured release only)")
        # 2. Forbidden keys.
        for k in result.keys():
            if k.lower() in _FORBIDDEN_KEYS:
                raise ReleaseDenied(f"forbidden key in release: {k!r}")
        # 3. Citation count must match claim count.
        if len(manifest.citations) != len(claims):
            raise ReleaseDenied(
                f"citation count {len(manifest.citations)} != claim count {len(claims)}"
            )
        # 4. Every claim has at least one citation; no
        #    unsourced claims past the budget.
        unsourced = sum(1 for c in manifest.citations if not c.sources)
        if unsourced > self._max_unsourced:
            raise ReleaseDenied(
                f"{unsourced} unsourced claims > budget {self._max_unsourced}"
            )
        # 5. No restricted sources cross the gate.
        for c in manifest.citations:
            for src in c.sources:
                if src.label is None:
                    continue
                # Restricted sources are NEVER releasable.
                cls = getattr(src.label, "classification", None)
                # DataClassification is an Enum; compare to .value.
                if cls is not None and getattr(cls, "value", str(cls)) == "restricted":
                    raise ReleaseDenied(
                        f"citation {c.citation_id} carries restricted source {src.ref!r}"
                    )
        # 6. Audience check: every citation's ``audience`` must be
        #    in the Card's audiences.
        for c in manifest.citations:
            if c.audience not in self._card.audiences:
                raise ReleaseDenied(
                    f"citation {c.citation_id} audience {c.audience!r} "
                    f"not in card audiences {self._card.audiences!r}"
                )
        return result
