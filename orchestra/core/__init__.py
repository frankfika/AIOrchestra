"""Core schemas and primitives for Orchestra P0/M0+.

The contents of this package are the **frozen vocabulary** for the demo. The
names and shapes here are referenced from LIT-001..005 (P0) and from SPEC-001,
SPEC-002, SPEC-003, SEC-001, SEC-002 (M0). Anything that wants to add a
parallel type should add an ADR, not a new module.

M0 extends P0 with:
  - ValueRef, Requirement, InformationFlowRule (SPEC-001 / STIR)
  - FieldManifest (XFR-001 input for Schema Projection + Egress PEP, M3)
  - Citation / CitationManifest (REL-001 input for Release Gate, M5)
"""
from orchestra.core.errors import NotInScopeError, ContractViolation
from orchestra.core.ids import (
    new_id,
    digest_json,
    digest_bytes,
    content_addressed_id,
)
from orchestra.core.hashing import (
    hmac_keygen,
    hmac_sign,
    hmac_verify,
    cose_like_envelope,
    verify_cose_like,
)
from orchestra.core.time import utc_now_iso, monotonic_ms
from orchestra.core.schema import (
    SecurityLabel,
    DataClassification,
    SourceTrust,
    DataView,
    Effect,
    EffectKind,
    Purpose,
    CapabilityKind,
    IntegrationLevel,
    CapabilityManifest,
    TaskContract,
    TaskTemplate,
    NodeSpec,
    EdgeSpec,
    ValueRef,
    Requirement,
    JoinSemantics,
    InformationFlowRule,
    FieldManifest,
    CitationSourceRef,
    Citation,
    CitationManifest,
    PlanNode,
    PlanEdge,
    ExecutionPlan,
    NodeRunState,
    TaskRunState,
    NodeGrant,
    AuditEvent,
    EventKind,
    SignedReceipt,
    RoutingDecision,
    FallbackPolicy,
    ApprovalSpec,
    export_json_schemas,
)

__all__ = [
    # errors
    "NotInScopeError",
    "ContractViolation",
    # ids
    "new_id",
    "digest_json",
    "digest_bytes",
    "content_addressed_id",
    # hashing
    "hmac_keygen",
    "hmac_sign",
    "hmac_verify",
    "cose_like_envelope",
    "verify_cose_like",
    # time
    "utc_now_iso",
    "monotonic_ms",
    # labels & data view
    "SecurityLabel",
    "DataClassification",
    "SourceTrust",
    "DataView",
    # effects & purpose
    "Effect",
    "EffectKind",
    "Purpose",
    # capabilities
    "CapabilityKind",
    "IntegrationLevel",
    "CapabilityManifest",
    # task / template
    "TaskContract",
    "TaskTemplate",
    "NodeSpec",
    "EdgeSpec",
    # M0: STIR additions
    "ValueRef",
    "Requirement",
    "JoinSemantics",
    "InformationFlowRule",
    "FieldManifest",
    "CitationSourceRef",
    "Citation",
    "CitationManifest",
    # plan & routing
    "PlanNode",
    "PlanEdge",
    "ExecutionPlan",
    "RoutingDecision",
    # state
    "NodeRunState",
    "TaskRunState",
    # grants & approvals
    "NodeGrant",
    "ApprovalSpec",
    "FallbackPolicy",
    # events & receipts
    "AuditEvent",
    "EventKind",
    "SignedReceipt",
    # helpers
    "export_json_schemas",
]
