"""M6 (enterprise-beta) test suite.

The M6 B6 gate requires:

  * Cross-tenant reads are impossible at the storage layer (ENT-001).
  * SBOM is built from project metadata and components are pinned
    (ENT-003).
  * Artifact signatures are HMAC over SHA-256 and verify.
  * Provenance attestations are signed and verify.
  * Enterprise connectors (OIDC/SCIM/KMS/SIEM) satisfy the
    production-swap contract (ENT-004).
"""
