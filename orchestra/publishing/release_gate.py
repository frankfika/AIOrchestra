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

from typing import TYPE_CHECKING, Any

from orchestra.core.errors import OrchestraError
from orchestra.core.schema import CitationManifest
from orchestra.publishing.card import AgentCard

if TYPE_CHECKING:  # pragma: no cover
    from orchestra.observability.metrics import Metrics


class ReleaseDenied(OrchestraError):
    """The Release Gate refused to publish the result."""


# The four forbidden top-level keys. A partner that gets a result
# with any of these in it can reverse-engineer internal state.
_FORBIDDEN_KEYS = frozenset(
    {
        "error",
        "errors",
        "trace",
        "traces",
        "stacktrace",
        "stack",
        "internal_id",
        "raw_payload",
    }
)


class ReleaseGate:
    """The M5 Output / Citation Release Gate.

    Built around the :class:`CitationManifest` schema the M0 freeze
    already defined. The Gate's job is to refuse any release that
    would leak a higher-tier label than the Card's audience allows.
    """

    def __init__(
        self,
        *,
        card: AgentCard,
        max_unsourced_claims: int = 0,
        metrics: Metrics | None = None,
    ) -> None:
        self._card = card
        self._max_unsourced = max_unsourced_claims
        # M13 — when metrics is set, every denial increments the
        # ``orchestra_release_gate_denied_total{reason=...}`` counter
        # so a SRE can see gate pressure in Grafana.
        self._metrics = metrics
        if metrics is not None:
            self._m_denied = metrics.counter(
                "orchestra_release_gate_denied_total",
                "Total ReleaseGate denials.",
                labels=("reason",),
            )
        else:
            self._m_denied = None

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
            self._deny("not_a_dict", "result must be a dict, not free text")
        claims = result.get("claims")
        if not isinstance(claims, list):
            self._deny("no_claims_list", "result.claims must be a list (structured release only)")
        # 2. Forbidden keys.
        for k in result:
            if k.lower() in _FORBIDDEN_KEYS:
                self._deny("forbidden_key", f"forbidden key in release: {k!r}")
        # 3. Citation count must match claim count.
        if len(manifest.citations) != len(claims):
            self._deny(
                "citation_count_mismatch",
                f"citation count {len(manifest.citations)} != claim count {len(claims)}",
            )
        # 4. Every claim has at least one citation; no
        #    unsourced claims past the budget.
        unsourced = sum(1 for c in manifest.citations if not c.sources)
        if unsourced > self._max_unsourced:
            self._deny(
                "unsourced_over_budget",
                f"{unsourced} unsourced claims > budget {self._max_unsourced}",
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
                    self._deny(
                        "restricted_source",
                        f"citation {c.citation_id} carries restricted source {src.ref!r}",
                    )
        # 6. Audience check: every citation's ``audience`` must be
        #    in the Card's audiences.
        for c in manifest.citations:
            if c.audience not in self._card.audiences:
                self._deny(
                    "audience_mismatch",
                    f"citation {c.citation_id} audience {c.audience!r} "
                    f"not in card audiences {self._card.audiences!r}",
                )
        return result

    def _deny(self, reason: str, message: str) -> None:
        """Record the denial in metrics (if available) and raise.

        The ``reason`` label is a coarse bucket (so cardinality stays
        small); the full ``message`` is the precise text SREs see in
        the traceback.
        """
        if self._m_denied is not None:
            self._m_denied.inc(reason=reason)
        raise ReleaseDenied(message)
