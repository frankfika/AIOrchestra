"""M13 — Instrumentation tests.

The four source modules (EgressPEP, ReleaseGate, Ingress,
PublishedRegistry) accept an optional ``metrics=`` argument. The
tests below prove:

  * the metrics handle the success and denial paths,
  * the metric *labels* match what the dashboard expects, and
  * ``metrics=None`` (the dev default) keeps the original behavior.

The :class:`Metrics` instance is fresh per test so the values
are deterministic and not affected by other tests' setup.
"""
from __future__ import annotations

import pytest

from orchestra.core.hashing import hmac_keygen
from orchestra.core.ids import new_id
from orchestra.core.schema import (
    Citation,
    CitationManifest,
    CitationSourceRef,
    DataClassification,
    SecurityLabel,
    SourceTrust,
)
from orchestra.observability import Metrics, render_prometheus
from orchestra.publishing.card import AgentCard, CardStatus
from orchestra.publishing.ingress import Ingress, IngressDenied
from orchestra.publishing.registry import PublishedRegistry
from orchestra.publishing.release_gate import ReleaseDenied, ReleaseGate
from orchestra.registry.bootstrap import load_default_field_manifests
from orchestra.xfr.egress_pep import EgressDenied, EgressPEP


# ---------------------------------------------------------------------------
# Helpers — kept local to this file so M13 tests don't depend on
# the M5 fixture shape (which uses different capability_ids).
# ---------------------------------------------------------------------------


def _safe_label() -> SecurityLabel:
    return SecurityLabel(
        classification=DataClassification.INTERNAL,
        residency="local",
        source_trust=SourceTrust.INTERNAL,
        retention_days=365,
        owner="tenant:demo",
    )


def _make_card(**overrides) -> AgentCard:
    base = dict(
        capability_id="cap.test",
        name="cap.test",
        version="1.0.0",
        partner_id="partner-test",
        partner_contract_id="contract-test",
        audiences=["partner"],
        data_views=[],
    )
    base.update(overrides)
    return AgentCard(**base)


def _citation(audience: str = "partner") -> Citation:
    return Citation(
        claim="ok",
        audience=audience,
        sources=[CitationSourceRef(kind="synthetic", ref="s1", label=_safe_label())],
    )


def _manifest(*citations) -> CitationManifest:
    return CitationManifest(
        task_run_id=f"trun-{new_id()[:8]}",
        citations=list(citations),
    )


# ---------------------------------------------------------------------------
# EgressPEP
# ---------------------------------------------------------------------------


def test_egress_pep_increments_projection_total_on_success():
    metrics = Metrics()
    pep = EgressPEP(
        manifest_lookup=lambda cid, view: load_default_field_manifests().get(
            (cid, view)
        ),
        metrics=metrics,
    )
    pep.project(
        capability_id="public.openai-compat",
        view_name="public-research",
        payload={"facts": {"vendor": "acme"}, "query": "industry classification"},
    )
    out = render_prometheus(metrics)
    assert (
        'orchestra_egress_pep_projection_total{capability="public.openai-compat"'
        ',view="public-research"} 1'
    ) in out
    assert 'orchestra_egress_pep_projection_bytes_count{capability="public.openai-compat"} 1' in out


def test_egress_pep_increments_denied_total_on_missing_manifest():
    metrics = Metrics()
    pep = EgressPEP(
        manifest_lookup=lambda cid, view: None,  # always missing
        metrics=metrics,
    )
    with pytest.raises(EgressDenied):
        pep.project(
            capability_id="does.not.exist",
            view_name="any",
            payload={"x": 1},
        )
    out = render_prometheus(metrics)
    assert (
        'orchestra_egress_pep_denied_total{capability="does.not.exist"'
        ',view="any"} 1'
    ) in out
    # The metric is registered (HELP / TYPE) but no value line
    # because no projection happened.
    lines = [
        l for l in out.splitlines()
        if l.startswith("orchestra_egress_pep_projection_total{")
    ]
    assert lines == []


def test_egress_pep_increments_denied_total_on_budget_exceeded():
    metrics = Metrics()
    from orchestra.core.schema import FieldManifest
    tight = FieldManifest(
        name="tight",
        source_view="tight-view",
        allowed_fields=["facts"],
        redaction_rules=[],
        byte_budget=4,  # smaller than the payload
    )
    pep = EgressPEP(
        manifest_lookup=lambda cid, view: tight,
        metrics=metrics,
    )
    with pytest.raises(EgressDenied):
        pep.project(
            capability_id="tight.cap",
            view_name="tight-view",
            payload={"facts": {"vendor": "acme corp"}},
        )
    out = render_prometheus(metrics)
    assert (
        'orchestra_egress_pep_denied_total{capability="tight.cap"'
        ',view="tight-view"} 1'
    ) in out


def test_egress_pep_works_without_metrics():
    """The default code path (no metrics) must behave as before —
    this is the regression guard for the optional kwarg."""
    pep = EgressPEP(manifest_lookup=lambda cid, view: None)
    with pytest.raises(EgressDenied):
        pep.project(capability_id="x", view_name="y", payload={})


# ---------------------------------------------------------------------------
# ReleaseGate
# ---------------------------------------------------------------------------


def test_release_gate_increments_denied_on_free_text():
    metrics = Metrics()
    card = _make_card()
    gate = ReleaseGate(card=card, metrics=metrics)
    with pytest.raises(ReleaseDenied):
        gate.release("not a dict", _manifest(_citation()))
    out = render_prometheus(metrics)
    assert (
        'orchestra_release_gate_denied_total{reason="not_a_dict"} 1'
    ) in out


def test_release_gate_increments_denied_on_forbidden_key():
    metrics = Metrics()
    card = _make_card()
    gate = ReleaseGate(card=card, metrics=metrics)
    with pytest.raises(ReleaseDenied):
        gate.release(
            {"claims": ["x"], "error": "leak"},
            _manifest(_citation()),
        )
    out = render_prometheus(metrics)
    assert (
        'orchestra_release_gate_denied_total{reason="forbidden_key"} 1'
    ) in out


def test_release_gate_increments_denied_on_citation_count_mismatch():
    metrics = Metrics()
    card = _make_card()
    gate = ReleaseGate(card=card, metrics=metrics)
    with pytest.raises(ReleaseDenied):
        gate.release({"claims": []}, _manifest(_citation()))
    out = render_prometheus(metrics)
    assert (
        'orchestra_release_gate_denied_total{reason="citation_count_mismatch"} 1'
    ) in out


def test_release_gate_does_not_tick_on_success():
    metrics = Metrics()
    card = _make_card()
    gate = ReleaseGate(card=card, metrics=metrics)
    gate.release({"claims": ["ok"]}, _manifest(_citation()))
    out = render_prometheus(metrics)
    # The metric is registered (HELP / TYPE lines exist) but no
    # value line should appear because no denial happened.
    lines = [
        l for l in out.splitlines()
        if l.startswith("orchestra_release_gate_denied_total{")
    ]
    assert lines == []


# ---------------------------------------------------------------------------
# Ingress
# ---------------------------------------------------------------------------


def test_ingress_increments_admitted_on_success():
    metrics = Metrics()
    key = hmac_keygen()
    reg = PublishedRegistry(
        default_key=key, default_kid="k1", metrics=metrics,
    )
    card = _make_card(capability_id="cap.ing", partner_id="partner-ing")
    reg.publish(card, key=key, kid="k1")
    ingress = Ingress(reg, token_key=key, metrics=metrics)
    token = ingress.issue_token(
        issuer="iss", subject="sub", audience="partner",
        scopes=["read"],
    )
    _card, _bt = ingress.admit(capability_id="cap.ing", version=None, token=token)
    out = render_prometheus(metrics)
    assert (
        'orchestra_ingress_admit_total{outcome="admitted"} 1'
    ) in out


def test_ingress_increments_missing_scope_on_bad_token():
    metrics = Metrics()
    key = hmac_keygen()
    reg = PublishedRegistry(default_key=key, default_kid="k1", metrics=metrics)
    # Card requires BOTH "read" and "write" scopes.
    card = _make_card(
        capability_id="cap.ing2",
        partner_id="partner-ing2",
        partner_contract_id="contract-2",
    )
    # Override contract_snapshot to require both scopes.
    card = card.model_copy(update={
        "contract_snapshot": {
            "audiences": [
                {"name": "partner-ing2", "required_scopes": ["read", "write"]},
            ],
        },
    })
    reg.publish(card, key=key, kid="k1")
    ingress = Ingress(reg, token_key=key, metrics=metrics)
    token = ingress.issue_token(
        issuer="iss", subject="sub", audience="partner",
        scopes=["read"],  # missing "write"
    )
    with pytest.raises(IngressDenied):
        ingress.admit(capability_id="cap.ing2", version=None, token=token)
    out = render_prometheus(metrics)
    assert (
        'orchestra_ingress_admit_total{outcome="missing_scope"} 1'
    ) in out


def test_ingress_increments_not_found_on_unknown_capability():
    metrics = Metrics()
    key = hmac_keygen()
    reg = PublishedRegistry(default_key=key, default_kid="k1", metrics=metrics)
    ingress = Ingress(reg, token_key=key, metrics=metrics)
    token = ingress.issue_token(
        issuer="iss", subject="sub", audience="x", scopes=[],
    )
    with pytest.raises(IngressDenied):
        ingress.admit(capability_id="does.not.exist", version=None, token=token)
    out = render_prometheus(metrics)
    assert (
        'orchestra_ingress_admit_total{outcome="not_found"} 1'
    ) in out


# ---------------------------------------------------------------------------
# PublishedRegistry
# ---------------------------------------------------------------------------


def test_registry_increments_published_and_revoked():
    metrics = Metrics()
    key = hmac_keygen()
    reg = PublishedRegistry(default_key=key, default_kid="k1", metrics=metrics)
    card = _make_card()
    signed = reg.publish(card, key=key, kid="k1")
    reg.revoke(signed.capability_id, signed.version, reason="test")
    out = render_prometheus(metrics)
    assert "orchestra_publish_published_total 1" in out
    assert "orchestra_publish_revoked_total 1" in out
    # Revoked cards still occupy a slot in the registry; the
    # gauge reports the size of the table, not the count of
    # currently-valid cards. A SRE combines this with the
    # revoked counter to know how many are reachable.
    assert "orchestra_published_cards_total 1" in out


def test_registry_publish_count_keeps_climbing_across_versions():
    metrics = Metrics()
    key = hmac_keygen()
    reg = PublishedRegistry(default_key=key, default_kid="k1", metrics=metrics)
    for v in ("1.0.0", "1.1.0", "2.0.0"):
        card = _make_card().model_copy(update={"version": v})
        reg.publish(card, key=key, kid="k1")
    out = render_prometheus(metrics)
    assert "orchestra_publish_published_total 3" in out
    assert "orchestra_published_cards_total 3" in out


def test_registry_works_without_metrics():
    """Default code path stays unchanged."""
    key = hmac_keygen()
    reg = PublishedRegistry(default_key=key, default_kid="k1")
    card = _make_card()
    signed = reg.publish(card, key=key, kid="k1")
    assert signed.status == CardStatus.PUBLISHED
