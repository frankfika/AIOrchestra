# Security Policy

> **Scope:** This document covers the security posture of
> Orchestra at the M0–M9 milestone boundary. It is the input
> the M6 ENT-004 (SIEM connector) gate expects and the M7
> runbook references.

## Supported versions

| Branch | Supported |
| --- | --- |
| `main` (latest) | ✅ |
| Older tags | ❌ (pilot onboarding always uses `main`) |

The dev path is for local pilots. The production deployment
swaps the dev connectors (M6 ENT-004) and uses a real KMS / OIDC
/ SIEM; the security boundary lives in those external systems,
not in this codebase.

## What is NOT in scope

- **Vulnerabilities in the local PostgreSQL / Docker images**:
  see your platform's security advisories. The dev compose
  uses `postgres:16-alpine` for parity with the runbook; a real
  deployment should pin a hardened image (e.g. Bitnami's
  hardened PostgreSQL).
- **Vulnerabilities in the host OS**: standard OS patching.
- **Vulnerabilities in uvicorn / FastAPI / Starlette**: see
  the [known-issues](#known-issues) section below.

## Threat model (M6)

The M6 ENT-001/004 invariants are the security boundary. The
threats explicitly addressed:

1. **Cross-tenant read** — Tenant A cannot read Tenant B's
   audit timeline, task_runs, events, receipts, grants, or
   approvals. Enforced at the storage layer (M6 ADR-0003);
   verified by `tests/m6/test_isolation.py` (5 tests).
2. **Restricted egress** — A node that accepts restricted data
   cannot ship the raw payload to a public Adapter. Enforced
   by the M3 Egress PEP (XFR-001); verified by
   `tests/m3/test_xfr.py` and `tests/m3/test_e2e_m3.py`.
3. **Restricted citation release** — A partner-facing result
   cannot cite a restricted source. Enforced by the M5
   Release Gate; verified by `tests/m5/test_publishing.py`.
4. **Kill Switch** — The publish-side Kill Switch takes effect
   within `max_effect_seconds` (default 5s). Enforced by
   `tests/m5/test_publishing.py::test_kill_switch_takes_effect_within_bounded_time`.
5. **Bearer token forgery** — A partner's bearer token must
   pass HMAC verify (M5 dev) / OIDC discovery (M6 production).
   Verified by `tests/m5/test_publishing.py::test_ingress_*`.
6. **Tampered Agent Card** — A modified Card fails
   `verify_card`. Verified by `tests/m5/test_publishing.py::test_*_sign*`.

The threats **explicitly NOT** addressed by the dev path:

- **Production KMS / OIDC / SIEM**: the dev path uses a
  per-process random HMAC key. The production swap plugs in
  a real KMS (M6 ENT-004), which is where the actual
  cryptographic boundary lives.
- **Rate limiting / DDoS**: not in the dev path; production
  deployments should front Orchestra with an API gateway.
- **Audit-trail gap detection at the application layer**: the
  M2 Merkle log proves inclusion; the M6 SLO target for RPO
  is enforced via Postgres + WAL archival (M7 runbook).
- **Tenant compromise**: a hostile tenant with admin role can
  see other tenants via `list_tenants_for_admin`. The
  production swap adds SIEM alerting on cross-tenant admin
  reads (M6 ENT-004).

## Reporting a vulnerability

Email `security@orchestra.local` (placeholder; production
should configure an alias that pages on-call). The first
response target is 24 hours. Coordinated disclosure is the
default; a fix ships before the advisory is published.

## How to harden the dev path for a real pilot

1. **Replace the dev signing key** in `orchestra/api/app.py`
   with a key fetched from the production KMS. The publish
   route's `state._publish_key` is per-process; production
   must replace it with a KMS-backed key.
2. **Replace `DevHMACIdP`** with a real OIDC verifier
   (Okta, Azure AD, Keycloak). The interface in
   `orchestra/enterprise/connectors.py` is the contract.
3. **Replace `InMemorySCIMDirectory`** with a real SCIM
   directory. The interface is the contract.
4. **Replace `InMemorySIEMForwarder`** with a real SIEM
   (Splunk HEC, Elastic, Datadog). The interface is the
   contract.
5. **Pin the dev compose to a hardened PostgreSQL image**
   (Bitnami's hardened image, or use a managed service).
6. **Set `ORCHESTRA_LOG_LEVEL=WARNING` in production** to
   avoid leaking structured logs to the public network.
7. **Run `pip-audit -r requirements.txt` before every release**
   and pin the deps to the latest secure versions. The known
   issues below are tracked in this doc.

## Known issues (M11 audit, 2026-08-06)

`pip-audit -r requirements.txt` reports:

- **starlette 0.38.6** (transitive of fastapi 0.115.0): 8 CVEs
  — `PYSEC-2026-161`, `PYSEC-2026-248`, `PYSEC-2026-249`,
  `PYSEC-2026-1941`, `PYSEC-2026-1943`, `PYSEC-2026-2280`,
  `PYSEC-2026-2281`. Fixed in starlette 1.0.1 / 0.40.0 / 0.47.2.
- **pytest 8.3.3**: 1 CVE — `PYSEC-2026-1845`. Fixed in
  pytest 9.0.3.

**Mitigation**: pin `fastapi>=0.116` (which pulls starlette
>=1.0.1) and `pytest>=9.0.3` in the next release. The dev
path is unaffected by these CVEs in the local-only deployment
mode; the production swap should pin the secure versions
before going live.

## Codebase audit (M11, 2026-08-06)

A static review of `orchestra/` for the common high-impact
issues:

- **No hardcoded secrets**. The dev signing key is generated
  per-process via `hmac_keygen()`; KMS material is generated
  per-key. The only `b"k" * 32` patterns are in test fixtures
  (`tests/m*/*`).
- **No raw SQL injection risk**. All SQL is built from a
  fixed allowlist of column names; user input is passed as
  psycopg parameters (`%s`) which the driver quotes.
- **No XSS in templates**. `orchestra/ux/templates.py` uses
  `html.escape` (`_esc`) for every dynamic value.
- **No `eval` / `exec`** in user-input paths. The `compile`
  call in the SBOM parser operates on a fixed TOML
  expression, not user input.
- **No `subprocess=True` shells**. `git rev-parse` runs in
  `try/except` and the result is treated as opaque text.
- **No file-write paths from user input**. The SBOM / artifact
  paths come from the operator, not the API.

The audit does not substitute for `pip-audit` or a real SAST
tool; it covers the patterns the team has explicit tests for.
