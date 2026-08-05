"""M9 — Sample tenant configurations for pilot onboarding.

A new pilot can import these to get a working multi-tenant
setup in < 30 seconds. The configs are deliberately
*illustrative* — Frank replaces the IDs / names with the
pilot's real values during the onboarding call.

Usage:

    from data.samples.tenants import ACME_TENANT, ACME_CARD
    client = TestClient(create_app())
    r = client.post("/admin/tenants", json=ACME_TENANT)
    r = client.post("/admin/publish", json=ACME_CARD)
"""
from __future__ import annotations

ACME_TENANT = {
    "tenant_id": "tenant:acme",
    "name": "ACME Corp",
    "plan": "pilot",
}

BETA_TENANT = {
    "tenant_id": "tenant:beta",
    "name": "Beta Insights",
    "plan": "pilot",
}


ACME_SUMMARIZE_CARD = {
    "capability_id": "acme.summarize",
    "name": "ACME Summarise",
    "version": "0.1.0",
    "description": "Summarise a contract review into a structured risk profile.",
    "partner_id": "partner-beta",
    "partner_contract_id": "contract-acme-beta-001",
    "audiences": ["partner-beta-api", "partner"],
    "data_views": ["view:safe-summary"],
}


ACME_CLASSIFY_CARD = {
    "capability_id": "acme.classify",
    "name": "ACME Classify",
    "version": "0.1.0",
    "description": "Classify a contract by industry and risk tier.",
    "partner_id": "partner-beta",
    "partner_contract_id": "contract-acme-beta-001",
    "audiences": ["partner-beta-api", "partner"],
    "data_views": ["view:industry"],
}


def all_tenants() -> list[dict]:
    return [ACME_TENANT, BETA_TENANT]


def all_cards() -> list[dict]:
    return [ACME_SUMMARIZE_CARD, ACME_CLASSIFY_CARD]
