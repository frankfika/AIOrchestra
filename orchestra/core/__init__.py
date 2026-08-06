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
from orchestra.core.errors import ContractViolation, NotInScopeError
from orchestra.core.hashing import (
    cose_like_envelope,
    hmac_keygen,
    hmac_sign,
    hmac_verify,
    verify_cose_like,
)
from orchestra.core.ids import (
    content_addressed_id,
    digest_bytes,
    digest_json,
    new_id,
)
from orchestra.core.schema import (
    ApprovalSpec,
    AuditEvent,
    CapabilityKind,
    CapabilityManifest,
    Citation,
    CitationManifest,
    CitationSourceRef,
    DataClassification,
    DataView,
    EdgeSpec,
    Effect,
    EffectKind,
    EventKind,
    ExecutionPlan,
    FallbackPolicy,
    FieldManifest,
    InformationFlowRule,
    IntegrationLevel,
    JoinSemantics,
    NodeGrant,
    NodeRunState,
    NodeSpec,
    PlanEdge,
    PlanNode,
    Purpose,
    Requirement,
    RoutingDecision,
    SecurityLabel,
    SignedReceipt,
    SourceTrust,
    TaskContract,
    TaskRunState,
    TaskTemplate,
    ValueRef,
    export_json_schemas,
)
from orchestra.core.time import monotonic_ms, utc_now_iso

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
