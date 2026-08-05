"""M6 — Enterprise Beta.

This package holds the production-grade surface that goes beyond
the single-tenant M0–M5 demo:

  * :mod:`orchestra.enterprise.tenant`       — TenantContext + RBAC
  * :mod:`orchestra.enterprise.isolation`    — multi-tenant EventStore
  * :mod:`orchestra.enterprise.supply_chain` — SBOM, signing, provenance
  * :mod:`orchestra.enterprise.connectors`   — OIDC/SCIM/KMS/SIEM

The M6 B6 gate (ENT-001/002/003/004) requires:

  * Cross-tenant reads are impossible at the storage layer
    (ENT-001).
  * A published SBOM is shipped with each release and the wheel
    is signed (ENT-003).
  * Every connector has a single dev implementation that satisfies
    the production interface (ENT-004); the production swap is a
    config change.
"""
