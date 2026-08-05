"""M9 — Sample tenant / Agent Card data tests.

The :mod:`data.samples.tenants` module ships a pair of
illustrative tenants + cards. The tests here pin the shape
so a pilot onboarding change is intentional, not accidental.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from data.samples.tenants import (
    ACME_CLASSIFY_CARD,
    ACME_SUMMARIZE_CARD,
    ACME_TENANT,
    BETA_TENANT,
    all_cards,
    all_tenants,
)
from orchestra.publishing.card import AgentCard


def test_sample_tenants_have_required_fields():
    for t in all_tenants():
        assert "tenant_id" in t
        assert t["tenant_id"].startswith("tenant:")
        assert "name" in t
        assert "plan" in t
        # No secrets in the sample.
        assert "password" not in t
        assert "secret" not in t


def test_sample_cards_validate_against_agent_card_schema():
    """The sample cards must round-trip through the AgentCard
    Pydantic model without modification. A pilot that copy-pastes
    a card from the sample directory gets a working published
    capability."""
    for c in all_cards():
        card = AgentCard(**c)
        # The Card is a DRAFT by default; the registry flips it
        # to PUBLISHED at sign time. The sample is meant to be
        # POSTed to /admin/publish, which calls ``registry.publish``
        # with a DRAFT card.
        assert card.status.value == "draft"
        # Card body is JSON-serialisable.
        card.model_dump_json()


def test_sample_card_partner_consistent():
    """The cards are published to the same partner (Beta), so
    onboarding scripts can rely on the cross-reference."""
    for c in all_cards():
        assert c["partner_id"] == "partner-beta"
        assert c["partner_contract_id"] == "contract-acme-beta-001"


def test_sample_tenants_have_distinct_ids():
    ids = {t["tenant_id"] for t in all_tenants()}
    assert len(ids) == len(all_tenants())


def test_sample_cards_have_distinct_capability_ids():
    ids = {c["capability_id"] for c in all_cards()}
    assert len(ids) == len(all_cards())


def test_known_constants_match_module_values():
    """The ``ACME_*`` / ``BETA_*`` constants are exactly the same
    objects the helpers return — a pilot script that imports
    the constants gets a working config without re-typing."""
    assert ACME_TENANT in all_tenants()
    assert BETA_TENANT in all_tenants()
    assert ACME_SUMMARIZE_CARD in all_cards()
    assert ACME_CLASSIFY_CARD in all_cards()
