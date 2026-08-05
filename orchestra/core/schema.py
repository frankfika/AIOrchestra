"""P0 frozen vocabulary (Task / Capability / Plan / Event / Receipt).

This module is the **single source of truth** for the P0 data model. Every
other component imports from here. If you want to add a field, first check
``Orchestra_开发计划.md`` §0.1.2 — names and meanings are frozen at M0, and
P0 implements a subset.

Why Pydantic v2: structural validation, JSON-Schema export, and good
ergonomics for the demo. The cost is one extra runtime dependency; the
benefit is that ``model_dump()`` is canonical for hashing and signing.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from orchestra.core.ids import content_addressed_id, new_id
from orchestra.core.time import parse_utc_iso, utc_now_iso

# ---------------------------------------------------------------------------
# Classification & labels
# ---------------------------------------------------------------------------


class DataClassification(str, Enum):
    """Confidentiality tiers P0 understands.

    P0 uses a 4-tier scale that maps to the white paper's restricted /
    internal / partner / public spectrum. **Restricted** data must never
    reach a public Adapter — that is invariant #1 in the 26-invariants
    matrix.
    """

    PUBLIC = "public"
    PARTNER = "partner"
    INTERNAL = "internal"
    RESTRICTED = "restricted"


class SourceTrust(str, Enum):
    """How much the data origin can be trusted.

    Self-reported by callers is *not* trusted. The plan reserves full
    SecurityLabel trust evaluation for M0/M1; P0 only uses the tag as a
    policy input, never as a security boundary by itself.
    """

    SYNTHETIC = "synthetic"  # test data, safe to share
    PUBLIC = "public"  # fetched from a versioned public source
    PARTNER = "partner"  # partner-provided under a contract
    INTERNAL = "internal"  # produced inside the tenant
    RESTRICTED = "restricted"  # see DataClassification.RESTRICTED


class SecurityLabel(BaseModel):
    """The minimum label P0 needs to make routing decisions.

    See §6.1 of the white paper for the full SecurityLabel algebra. P0
    implements a strict subset: classification, residency (single region),
    source trust, retention in days, and a free-form owner identifier.
    """

    model_config = ConfigDict(extra="forbid")

    classification: DataClassification
    residency: str = Field(default="local", description="ISO-3166 alpha-2 or 'local'")
    source_trust: SourceTrust = SourceTrust.INTERNAL
    retention_days: int = Field(default=30, ge=0)
    owner: str = Field(default="tenant:root", description="owner identifier")

    def can_flow_to(self, other: "SecurityLabel") -> bool:
        """Conservative flow check: classification must flow to an
        equal-or-stricter context, and residency must be compatible.

        Semantics:
        - "self can flow to other" means *data labelled self* is allowed
          to enter a *context labelled other*. Therefore the destination
          (``other``) must be at least as sensitive as the data
          (``self``); otherwise the data would leak upward.
        - If either side has ``residency == "local"`` it acts as a
          wildcard: any specific residency (``cn``, ``us``, …) can flow
          to a ``local`` context, and a ``local`` data origin can flow
          to any specific context.
        """
        order = {
            DataClassification.PUBLIC: 0,
            DataClassification.PARTNER: 1,
            DataClassification.INTERNAL: 2,
            DataClassification.RESTRICTED: 3,
        }
        if order[self.classification] > order[other.classification]:
            return False
        if self.residency == "local" or other.residency == "local":
            return True
        return self.residency == other.residency


# ---------------------------------------------------------------------------
# Effects, purposes, data views
# ---------------------------------------------------------------------------


class EffectKind(str, Enum):
    """Side-effect categories a node may declare.

    P0 enforces: any node declaring ``WRITE`` or ``DELETE`` or ``PAYMENT``
    or ``PUBLISH`` requires a downstream ``approval`` step (invariant #7).
    """

    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    PAYMENT = "payment"
    PUBLISH = "publish"
    NOTIFY = "notify"


class Effect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: EffectKind
    target: Optional[str] = Field(default=None, description="logical target name")


class Purpose(BaseModel):
    """Business purpose. Delegation cannot change purpose (invariant #5, #20)."""

    model_config = ConfigDict(extra="forbid")
    code: str = Field(description="short stable code, e.g. 'contract-review'")
    description: str = ""


class DataView(BaseModel):
    """A bounded projection of a data source.

    P0 only supports two shapes: ``reference`` (pointer, no payload) and
    ``fields`` (allowlist of named fields). Free-text dumps are explicitly
    out of scope for P0 (invariant #11, #16).
    """

    model_config = ConfigDict(extra="forbid")
    name: str
    shape: Literal["reference", "fields"]
    fields: list[str] = Field(default_factory=list)
    source: Optional[str] = Field(default=None, description="logical source ref")


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class CapabilityKind(str, Enum):
    LOCAL_MODEL = "local-model"
    PUBLIC_MODEL = "public-model"
    A2A_AGENT = "a2a-agent"
    TOOL = "tool"
    HUMAN = "human"
    SINK = "sink"  # e.g. Mock Procurement Sink


class IntegrationLevel(str, Enum):
    """P0 only uses ``enforce`` and ``observe``; ``recommend`` is reserved."""

    ENFORCE = "enforce"
    RECOMMEND = "recommend"
    OBSERVE = "observe"


class CapabilityManifest(BaseModel):
    """Static registration of a capability the Router can choose.

    P0 uses *static* manifests — see plan §0.1.1 P0 row. Snapshots are
    content-addressed and pinned into every Plan.
    """

    model_config = ConfigDict(extra="forbid")

    capability_id: str
    name: str
    kind: CapabilityKind
    version: str = "0.1.0"
    description: str = ""
    endpoint: str = Field(description="how the Coordinator reaches the capability")
    integration_level: IntegrationLevel = IntegrationLevel.ENFORCE
    accepts_labels: list[SecurityLabel] = Field(default_factory=list)
    produces_labels: list[SecurityLabel] = Field(default_factory=list)
    declared_effects: list[Effect] = Field(default_factory=list)
    supports_idempotency: bool = True
    supports_cancel: bool = False
    supports_status_query: bool = True
    cost_estimate_usd: float = Field(default=0.0, ge=0)
    p50_latency_ms: int = Field(default=1000, ge=0)
    p95_latency_ms: int = Field(default=3000, ge=0)
    tags: dict[str, str] = Field(default_factory=dict)

    def manifest_id(self) -> str:
        """Content-addressed ID for this manifest snapshot."""
        return content_addressed_id("manifest", self.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Templates & contracts
# ---------------------------------------------------------------------------


class NodeSpec(BaseModel):
    """Template-level description of a node, before binding."""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    name: str
    requires_purpose: Purpose
    requires_views: list[DataView] = Field(default_factory=list)
    eligible_capability_kinds: list[CapabilityKind] = Field(default_factory=list)
    declared_effects: list[Effect] = Field(default_factory=list)
    requires_approval: bool = False
    fallback_capability_id: Optional[str] = Field(
        default=None,
        description="pre-approved Fallback; must be set in P0 (plan §0.1.1 P0 row)",
    )
    timeout_ms: int = 30_000


class EdgeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_node: str
    to_node: str
    when: str = Field(default="always", description="predicate name or 'always'")


class TaskTemplate(BaseModel):
    """A frozen Task Template. P0 ships exactly one: contract-review."""

    model_config = ConfigDict(extra="forbid")

    template_id: str
    name: str
    description: str
    version: str = "0.1.0"
    nodes: list[NodeSpec]
    edges: list[EdgeSpec]
    required_purposes: list[Purpose] = Field(default_factory=list)
    max_runtime_ms: int = 120_000

    def node(self, node_id: str) -> NodeSpec:
        for n in self.nodes:
            if n.node_id == node_id:
                return n
        raise KeyError(f"unknown node_id {node_id!r}")


class TaskContract(BaseModel):
    """What an upper-layer (Dify, custom UI, CLI) submits to Orchestra."""

    model_config = ConfigDict(extra="forbid")

    contract_id: str = Field(default_factory=new_id)
    template_id: str
    submitted_by: str
    submitted_at: str = Field(default_factory=utc_now_iso)
    inputs: list[DataView] = Field(default_factory=list)
    purpose: Purpose
    requested_outputs: list[str] = Field(default_factory=list)
    budget_usd: float = Field(default=1.0, ge=0)
    region: str = "local"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ApprovalSpec(BaseModel):
    """Human-in-the-loop step. P0 only allows one per Plan."""

    model_config = ConfigDict(extra="forbid")
    approval_id: str = Field(default_factory=new_id)
    node_id: str
    requested_at: str = Field(default_factory=utc_now_iso)
    decided_at: Optional[str] = None
    decision: Optional[Literal["approve", "reject"]] = None
    decided_by: Optional[str] = None
    rationale: str = ""


# ---------------------------------------------------------------------------
# Plan & routing
# ---------------------------------------------------------------------------


class RoutingDecision(BaseModel):
    """Why the Router picked a specific Capability for a Node."""

    model_config = ConfigDict(extra="forbid")
    node_id: str
    chosen_capability_id: str
    chosen_manifest_id: str
    eligible_set: list[str]
    rejected: dict[str, str] = Field(default_factory=dict)
    rationale: str
    decided_at: str = Field(default_factory=utc_now_iso)


class PlanNode(BaseModel):
    """A node in a resolved Plan (one capability bound, one fallback set)."""

    model_config = ConfigDict(extra="forbid")
    node_id: str
    capability_id: str
    manifest_id: str
    purpose: Purpose
    input_views: list[DataView]
    expected_outputs: list[str]
    timeout_ms: int
    fallback_capability_id: Optional[str] = None
    requires_approval: bool = False
    status: Literal["pending", "running", "succeeded", "failed", "awaiting-approval"] = "pending"


class PlanEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_node: str
    to_node: str
    when: str = "always"


class ExecutionPlan(BaseModel):
    """The signed, resolved plan. P0 signs it with HMAC-SHA256."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(default_factory=new_id)
    contract_id: str
    template_id: str
    template_version: str
    nodes: list[PlanNode]
    edges: list[PlanEdge]
    routing: list[RoutingDecision] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)
    signed_by: str = "p0-local-signer"
    signature: Optional[str] = Field(
        default=None, description="HMAC over canonical JSON, base64url"
    )

    def digest(self) -> str:
        from orchestra.core.ids import digest_json

        body = self.model_dump(mode="json", exclude={"signature"})
        return digest_json(body)

    def plan_id_content(self) -> str:
        """Stable Plan ID derived from content (excluding the random UUID and
        the signature). Useful for tracing in tests.
        """
        from orchestra.core.ids import digest_json

        body = self.model_dump(mode="json", exclude={"plan_id", "signature"})
        return f"plan:{digest_json(body)[:12]}"


# ---------------------------------------------------------------------------
# State machines
# ---------------------------------------------------------------------------


class NodeRunState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting-approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskRunState(str, Enum):
    CREATED = "created"
    PLANNED = "planned"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting-approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Node Grant
# ---------------------------------------------------------------------------


class NodeGrant(BaseModel):
    """P0 dev credential: locally signed, short-lived, target-bound.

    See ADR-0002: this is *not* a delegated OAuth token chain (that is
    ENT-001). P0 binds: task, node, capability, view, purpose, expiry.
    """

    model_config = ConfigDict(extra="forbid")

    grant_id: str = Field(default_factory=new_id)
    task_run_id: str
    node_run_id: str
    task_id: str
    node_id: str
    capability_id: str
    manifest_id: str
    data_view: DataView
    purpose: Purpose
    issued_at: str = Field(default_factory=utc_now_iso)
    not_before: str = Field(default_factory=utc_now_iso)
    expires_at: str
    audience: str = "p0"
    signature: Optional[str] = None

    @field_validator("expires_at")
    @classmethod
    def _must_be_future(cls, v: str) -> str:
        # We don't enforce "future" here because tests construct expired
        # grants on purpose; runtime checks are done in coordinator.
        parse_utc_iso(v)
        return v


# ---------------------------------------------------------------------------
# Audit events & receipts
# ---------------------------------------------------------------------------


class EventKind(str, Enum):
    # Lifecycle
    TASK_RECEIVED = "task.received"
    PLAN_CREATED = "plan.created"
    PLAN_SIGNED = "plan.signed"
    NODE_STARTED = "node.started"
    NODE_AWAITING_APPROVAL = "node.awaiting-approval"
    NODE_APPROVED = "node.approved"
    NODE_REJECTED = "node.rejected"
    NODE_SUCCEEDED = "node.succeeded"
    NODE_FAILED = "node.failed"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    # Data flow
    IO_INTENT = "io.intent"
    IO_SENT = "io.sent"
    IO_RECEIVED = "io.received"
    EXTERNAL_OUTCOME = "external.outcome"
    # Decisions
    POLICY_DECISION = "policy.decision"
    ROUTING_DECISION = "routing.decision"
    GRANT_ISSUED = "grant.issued"
    RECEIPT_SIGNED = "receipt.signed"
    FALLBACK_TRIGGERED = "fallback.triggered"


class AuditEvent(BaseModel):
    """Append-only event in the Event Store.

    P0 stores each event in PostgreSQL with a unique ``event_id`` and a
    monotonically increasing ``seq``. There is no Merkle log in P0 (see
    ADR-0002) — every event is individually signed when it becomes a Receipt.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=new_id)
    seq: int = Field(default=0, description="monotonic per task_run_id")
    task_run_id: Optional[str] = None
    node_run_id: Optional[str] = None
    kind: EventKind
    occurred_at: str = Field(default_factory=utc_now_iso)
    actor: str = "orchestra"
    payload: dict[str, Any] = Field(default_factory=dict)
    prev_event_id: Optional[str] = None

    def event_id_content(self) -> str:
        """Stable ID if we ever want to dedupe (test helper)."""
        from orchestra.core.ids import digest_json

        body = self.model_dump(mode="json", exclude={"event_id"})
        return f"event:{digest_json(body)[:12]}"


class SignedReceipt(BaseModel):
    """A COSE-like signed envelope over an AuditEvent.

    P0 produces one Receipt per node, not per task. A task-level Receipt is
    just a list of node Receipt IDs. ``envelope`` is a COSE_Sign1-like dict
    produced by :func:`orchestra.core.hashing.cose_like_envelope`.
    """

    model_config = ConfigDict(extra="forbid")

    receipt_id: str = Field(default_factory=new_id)
    task_run_id: str
    node_run_id: str
    node_id: str
    envelope: dict[str, Any]
    created_at: str = Field(default_factory=utc_now_iso)


# ---------------------------------------------------------------------------
# Fallback policy
# ---------------------------------------------------------------------------


class FallbackPolicy(BaseModel):
    """One pre-approved Fallback per Plan, per the P0 rules.

    The plan (§0.1.1 P0 row) allows *one* pre-approved Fallback. If a
    scenario needs more, the implementation must regenerate the Plan with
    a new candidate set — that workflow is P3 / M1 territory and is
    explicitly out of scope here.
    """

    model_config = ConfigDict(extra="forbid")
    from_node: str
    fallback_capability_id: str
    trigger: Literal["policy-deny", "capability-error", "timeout", "all"] = "all"
