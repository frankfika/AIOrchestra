"""Core schemas and primitives for Orchestra P0.

The contents of this package are the **frozen vocabulary** for the demo. The
names and shapes here are referenced from LIT-001, LIT-002, LIT-004, and the
Audit Timeline. Anything that wants to add a parallel type should add an ADR,
not a new module.
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
    Effect,
    EffectKind,
    CapabilityKind,
    CapabilityManifest,
    TaskContract,
    TaskTemplate,
    NodeSpec,
    EdgeSpec,
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
    DataView,
    Purpose,
    FallbackPolicy,
    ApprovalSpec,
)

__all__ = [
    "NotInScopeError",
    "ContractViolation",
    "new_id",
    "digest_json",
    "digest_bytes",
    "content_addressed_id",
    "hmac_keygen",
    "hmac_sign",
    "hmac_verify",
    "cose_like_envelope",
    "verify_cose_like",
    "utc_now_iso",
    "monotonic_ms",
    "SecurityLabel",
    "DataClassification",
    "Effect",
    "EffectKind",
    "CapabilityKind",
    "CapabilityManifest",
    "TaskContract",
    "TaskTemplate",
    "NodeSpec",
    "EdgeSpec",
    "PlanNode",
    "PlanEdge",
    "ExecutionPlan",
    "NodeRunState",
    "TaskRunState",
    "NodeGrant",
    "AuditEvent",
    "EventKind",
    "SignedReceipt",
    "RoutingDecision",
    "DataView",
    "Purpose",
    "FallbackPolicy",
    "ApprovalSpec",
]
