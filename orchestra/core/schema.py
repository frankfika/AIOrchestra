"""M0 frozen vocabulary (STIR / Capability Manifest / Plan / Event / Receipt).

This module is the **single source of truth** for the M0 data model. Every
other component imports from here. If you want to add a field, first check
``Orchestra_开发计划.md`` §0.1.2 — names and meanings are frozen at M0.

Why Pydantic v2: structural validation, JSON-Schema export, and good
ergonomics for the demo. The cost is one extra runtime dependency; the
benefit is that ``model_dump(mode="json")`` is canonical for hashing,
signing, and the JSON-Schema dump in :func:`export_json_schemas`.

M0 extends P0 with:
  - :class:`ValueRef` — typed handle to a value produced by a node
  - :class:`Requirement` — node-level non-functional requirement
  - :class:`InformationFlowRule` — formal label-propagation semantics
  - :class:`FieldManifest` — deterministic field-level projection spec
    used by :class:`XFR-001` (Schema Projection + Egress PEP, M3)
  - :class:`Citation` / :class:`CitationManifest` — claim-to-source
    mapping used by :class:`REL-001` (M5)
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from orchestra.core.ids import content_addressed_id, new_id
from orchestra.core.time import parse_utc_iso, utc_now_iso

# ---------------------------------------------------------------------------
# Classification & labels
# ---------------------------------------------------------------------------


class DataClassification(str, Enum):
    """Confidentiality tiers M0 understands.

    4-tier scale that maps to the white paper's restricted / internal /
    partner / public spectrum. **Restricted** data must never reach a
    public Adapter — that is invariant #1 in the 26-invariants matrix.
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

    def can_flow_to(self, other: SecurityLabel) -> bool:
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
    target: str | None = Field(default=None, description="logical target name")


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
    source: str | None = Field(default=None, description="logical source ref")


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
    # M3 XFR-001 — if set, the Coordinator will run the adapter inputs
    # through the EgressPEP under this FieldManifest view before sending.
    # Capabilities without a view never see the PEP (local tools, sinks,
    # in-process nodes, etc.).
    egress_view_name: str | None = Field(
        default=None,
        description="FieldManifest view to project the egress through (M3 XFR-001)",
    )

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
    fallback_capability_id: str | None = Field(
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
    decided_at: str | None = None
    decision: Literal["approve", "reject"] | None = None
    decided_by: str | None = None
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
    fallback_capability_id: str | None = None
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
    signature: str | None = Field(
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
    signature: str | None = None

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
    # M24 — Break-glass lifecycle (ADR-0012)
    BREAK_GLASS_REQUESTED = "break_glass.requested"
    BREAK_GLASS_FIRST_APPROVED = "break_glass.first_approved"
    BREAK_GLASS_ACTIVE = "break_glass.active"
    BREAK_GLASS_EXPIRED = "break_glass.expired"
    BREAK_GLASS_REVOKED = "break_glass.revoked"
    # M24 — Legal Hold lifecycle (ADR-0014)
    HOLD_CREATED = "hold.created"
    HOLD_RELEASED = "hold.released"
    DELETION_REQUESTED = "deletion.requested"
    DELETION_COMPLETED = "deletion.completed"
    DELETION_PARTIAL = "deletion.partial"
    DELETION_BLOCKED = "deletion.blocked"
    DELETION_FAILED = "deletion.failed"


class AuditEvent(BaseModel):
    """Append-only event in the Event Store.

    P0 stores each event in PostgreSQL with a unique ``event_id`` and a
    monotonically increasing ``seq``. There is no Merkle log in P0 (see
    ADR-0002) — every event is individually signed when it becomes a Receipt.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=new_id)
    seq: int = Field(default=0, description="monotonic per task_run_id")
    task_run_id: str | None = None
    node_run_id: str | None = None
    kind: EventKind
    occurred_at: str = Field(default_factory=utc_now_iso)
    actor: str = "orchestra"
    payload: dict[str, Any] = Field(default_factory=dict)
    prev_event_id: str | None = None

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


# ---------------------------------------------------------------------------
# M0 additions: ValueRef, Requirement, InformationFlowRule
# ---------------------------------------------------------------------------


class ValueRef(BaseModel):
    """A typed reference to a value produced by an upstream node.

    Used in :class:`PlanNode.input_views` and :class:`AdapterRequest.inputs`
    to express the dataflow topology. The producer node's id is part of
    the ref so a Trust Compiler can statically resolve the data lineage.

    The reference does NOT carry the value itself — the value lives in
    the Zone Artifact Store (M0 §0.2 Agent-3 deliverable) and is fetched
    by the Adapter with the Node Grant as authorization.
    """

    model_config = ConfigDict(extra="forbid")
    ref_id: str = Field(default_factory=new_id)
    producer_node_id: str
    producer_output: str = Field(description="logical output name of the producer")
    view_name: str | None = Field(default=None, description="resolved DataView name")
    type_hint: Literal["text", "json", "binary", "reference", "stream"] = "json"
    label: SecurityLabel | None = Field(
        default=None,
        description="label of the data at the time of ValueRef resolution",
    )


class Requirement(BaseModel):
    """Non-functional requirement a node declares about its execution
    environment. The Trust Compiler checks these against the
    Capability Manifest's ``runtime_requirements`` field at compile time.
    """

    model_config = ConfigDict(extra="forbid")
    kind: Literal[
        "region",
        "gpu",
        "memory-mb",
        "timeout-ms",
        "network",
        "tool",
        "language",
        "tier",
    ]
    op: Literal["eq", "ne", "lt", "le", "gt", "ge", "in"] = "eq"
    value: Any


class JoinSemantics(str, Enum):
    """How a node combines the labels of its inputs to produce the
    label of its outputs. The default for the M0 reference Contract
    Review flow is :attr:`JOIN` (the most restrictive label wins).
    """

    JOIN = "join"            # output = join(input_labels);  restrictive
    MEET = "meet"            # output = meet(input_labels); permissive
    PASSTHROUGH = "passthrough"  # output = first non-empty input
    EXPLICIT = "explicit"    # the node's manifest declares the output label


class InformationFlowRule(BaseModel):
    """Formal label-propagation rule attached to a node.

    The Trust Compiler in M1 enforces these rules on every Plan. P0
    ships a *declarative* form; the M0 deliverable (SPEC-001) freezes
    the shape so M1 can implement the checker without breaking P0.
    """

    model_config = ConfigDict(extra="forbid")
    rule_id: str
    join: JoinSemantics = JoinSemantics.JOIN
    # Optional explicit override (used when join=EXPLICIT).
    explicit_output_label: SecurityLabel | None = None
    # Restrict to specific input refs (default: all inputs).
    input_refs: list[str] = Field(default_factory=list)
    # If set, the rule applies only to outputs of this name.
    output_name: str | None = None


# ---------------------------------------------------------------------------
# M0 addition: FieldManifest (Schema Projection input)
# ---------------------------------------------------------------------------


class FieldManifest(BaseModel):
    """Deterministic field-level projection spec.

    The M3 Egress PEP (XFR-001) reads this manifest to know exactly which
    fields may leave the tenant. The Trust Compiler (M1) verifies that
    the Egress PEP actually uses the manifest — not the plan — as the
    source of truth for what crosses the boundary.
    """

    model_config = ConfigDict(extra="forbid")
    manifest_id: str = Field(default_factory=lambda: f"fldman:{new_id()[:8]}")
    name: str
    version: str = "0.1.0"
    source_view: str = Field(description="the DataView this manifest projects from")
    allowed_fields: list[str] = Field(default_factory=list)
    redaction_rules: list[dict[str, Any]] = Field(
        default_factory=list,
        description="list of {field, op: 'drop'|'hash'|'tokenize'|'partial-<n>'}",
    )
    byte_budget: int | None = Field(
        default=None,
        description="max bytes the projected payload may consume (Pareto-style enforcement)",
    )


# ---------------------------------------------------------------------------
# M0 addition: Citation + CitationManifest (M5 input)
# ---------------------------------------------------------------------------


class CitationSourceRef(BaseModel):
    """Pointer to a specific source (a Node output, an external public doc, etc)."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["node-output", "external-url", "external-doc", "synthetic"]
    ref: str
    version: str | None = None
    retrieved_at: str | None = None
    label: SecurityLabel | None = None


class Citation(BaseModel):
    """A claim with a list of source references.

    M5 REL-001 (Output/Citation Release Gate) verifies that every claim
    in a published output has at least one allowed citation, and that
    no citation leaks a higher-tier label than the claim's audience.
    """

    model_config = ConfigDict(extra="forbid")
    citation_id: str = Field(default_factory=new_id)
    claim: str
    sources: list[CitationSourceRef]
    audience: Literal["public", "partner", "internal", "restricted"] = "internal"
    release_class: Literal["public", "partner", "attested", "restricted"] = "attested"


class CitationManifest(BaseModel):
    """Structured claim-to-source map for a Plan's outputs.

    Produced by the Coordinator at the end of a run; consumed by the
    Release Gate (M5) before any output leaves the tenant.
    """

    model_config = ConfigDict(extra="forbid")
    manifest_id: str = Field(default_factory=lambda: f"citeman:{new_id()[:8]}")
    task_run_id: str
    citations: list[Citation]
    total_claims: int = 0
    unsourced_claims: int = 0


# ---------------------------------------------------------------------------
# M24 — Break-glass (ADR-0012) and Persistent Approval (ADR-0013)
# ---------------------------------------------------------------------------


class BreakGlassState(str, Enum):
    """Finite state machine for a Break-glass request.

    Transitions (ADR-0012):
        requested → first-approved → active → expired
                                          ↘ revoked
        requested → first-approved → revoked
    """

    REQUESTED = "requested"
    FIRST_APPROVED = "first-approved"
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


class BreakGlassRequest(BaseModel):
    """A two-person, time-bounded override of a policy ceiling.

    ADR-0012. The request carries the structured ``Effect`` that the
    approvers are allowing. The ``BreakGlassService`` enforces the
    effect ceiling at runtime (no label downgrade, no Zero-Egress
    bypass, no Egress PEP bypass, no tenant isolation bypass).
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(default_factory=lambda: f"bg:{new_id()[:12]}")
    tenant_id: str
    task_run_id: str | None = None
    purpose: str
    effect: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured Effect payload the break-glass is asking for",
    )
    resource_scope: dict[str, Any] = Field(
        default_factory=dict,
        description="{resource_kind, resource_id, [additional filters]}",
    )
    ticket: str | None = Field(default=None, description="external ticket / case id")
    requested_by: str
    requested_at: str = Field(default_factory=utc_now_iso)
    state: BreakGlassState = BreakGlassState.REQUESTED
    first_approver: str | None = None
    first_approved_at: str | None = None
    second_approver: str | None = None
    second_approved_at: str | None = None
    window_seconds: int = 900  # 15-minute default
    activated_at: str | None = None
    expires_at: str | None = None
    revoked_by: str | None = None
    revoked_at: str | None = None
    revoke_reason: str | None = None


class ApprovalState(str, Enum):
    """State for the persistent approval workflow (ADR-0013).

    Business approvals move pending → approved or pending → rejected
    atomically. Break-glass approvals move pending → first-approved →
    approved (the final transition is from the second approver, not
    a state machine inside the same row).
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FIRST_APPROVED = "first-approved"  # break-glass only


class ApprovalRecord(BaseModel):
    """Persistent approval record (ADR-0013).

    PG is the source of truth. The engine's in-process asyncio.Event
    is a wake-up cache; the row is what the API reads.
    """

    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(default_factory=lambda: f"apv:{new_id()[:12]}")
    task_run_id: str
    node_id: str
    tenant_id: str
    version: int = 0
    state: ApprovalState = ApprovalState.PENDING
    required_approvers: int = 1
    requested_at: str = Field(default_factory=utc_now_iso)
    requested_by: str
    ticket: str | None = None
    decided_at: str | None = None
    decision_payload: dict[str, Any] | None = None


class ApprovalDecision(BaseModel):
    """One row in the append-only approver log (ADR-0013)."""

    model_config = ConfigDict(extra="forbid")

    decision_id: str = Field(default_factory=lambda: f"apvd:{new_id()[:12]}")
    approval_id: str
    decision_seq: int  # 1, 2, ...
    decided_by: str
    decided_at: str = Field(default_factory=utc_now_iso)
    decision: Literal["approve", "reject"]
    rationale: str = ""
    identity_tenant_id: str  # must match ApprovalRecord.tenant_id


# ---------------------------------------------------------------------------
# M24 — Retention and Legal Hold (ADR-0014)
# ---------------------------------------------------------------------------


class ResourceKind(str, Enum):
    """Kinds of resource covered by a LifecyclePolicy / LegalHold."""

    ARTIFACT = "artifact"
    RECEIPT = "receipt"
    EVENT = "event"
    WEBHOOK = "webhook"
    CACHE = "cache"
    BACKUP = "backup"


class DeletionState(str, Enum):
    """State for an idempotent DeletionJob (ADR-0014)."""

    PENDING = "pending"
    RUNNING = "running"
    DELETED = "deleted"
    PARTIAL = "partial"
    FAILED = "failed"
    HELD = "held"  # blocked by a Legal Hold


class LifecyclePolicy(BaseModel):
    """What to keep, for how long, for which resource kind.

    ADR-0014. A tenant defines one policy per resource kind. When
    ``auto_delete`` is True and a resource is older than
    ``retention_seconds``, the LifecycleSweeper is allowed to
    create a DeletionJob for it. When False, the resource is
    retained indefinitely (or until a Legal Hold / explicit
    delete).
    """

    model_config = ConfigDict(extra="forbid")

    policy_id: str = Field(default_factory=lambda: f"pol:{new_id()[:12]}")
    tenant_id: str
    resource_kind: ResourceKind
    retention_seconds: int
    auto_delete: bool = False
    created_at: str = Field(default_factory=utc_now_iso)


class LegalHold(BaseModel):
    """An absolute, tenant-scoped freeze on deletion (ADR-0014).

    While a hold is active, every delete attempt on a resource in
    ``legal_hold_resources`` is denied. A hold is released by an
    authenticated user with the legal_hold_releaser role.
    """

    model_config = ConfigDict(extra="forbid")

    hold_id: str = Field(default_factory=lambda: f"hold:{new_id()[:12]}")
    tenant_id: str
    case_id: str
    reason: str
    created_at: str = Field(default_factory=utc_now_iso)
    created_by: str
    released_at: str | None = None
    released_by: str | None = None
    release_reason: str | None = None
    # Resources covered by the hold. A hold without resources is
    # valid — it freezes all deletion for the tenant.
    resource_kinds: list[ResourceKind] = Field(default_factory=list)
    resource_ids: list[str] = Field(default_factory=list)


class DeletionEvidence(BaseModel):
    """Per-job record of what was deleted and what remained.

    ADR-0014. The evidence is the only output of a successful
    DeletionJob. It contains the deletion_id, the per-copy
    status, and a digest of the deleted payload. The evidence
    row itself is never deleted (the audit trail is permanent).
    """

    model_config = ConfigDict(extra="forbid")

    deletion_id: str = Field(default_factory=lambda: f"del:{new_id()[:12]}")
    copies_deleted: int = 0
    copies_kept: int = 0
    kept_resources: list[dict[str, str]] = Field(default_factory=list)
    payload_digest: str | None = None
    completed_at: str = Field(default_factory=utc_now_iso)


class DeletionJob(BaseModel):
    """An idempotent, retryable unit of deletion work (ADR-0014).

    A unique index on (tenant_id, resource_kind, resource_id)
    makes LifecycleManager.delete() idempotent — a second call
    returns the same job rather than creating a new one.
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(default_factory=lambda: f"dj:{new_id()[:12]}")
    tenant_id: str
    resource_kind: ResourceKind
    resource_id: str
    state: DeletionState = DeletionState.PENDING
    attempt: int = 0
    max_attempts: int = 3
    requested_at: str = Field(default_factory=utc_now_iso)
    requested_by: str
    completed_at: str | None = None
    last_error: str | None = None
    evidence: DeletionEvidence | None = None


# ---------------------------------------------------------------------------
# M0 export helper
# ---------------------------------------------------------------------------


def export_json_schemas() -> dict[str, Any]:
    """Dump the M0 JSON Schemas for every public type.

    Used by:
      - the schema-registry CI gate (M0 §0.2 Agent-1 deliverable)
      - the Dify Task Tool to validate the on-the-wire payload
      - the AgenticHub adapter (M4) to generate TypeScript bindings
    """
    types = [
        SecurityLabel,
        DataView,
        Effect,
        Purpose,
        CapabilityManifest,
        TaskContract,
        TaskTemplate,
        NodeSpec,
        EdgeSpec,
        PlanNode,
        PlanEdge,
        ExecutionPlan,
        NodeGrant,
        AuditEvent,
        SignedReceipt,
        RoutingDecision,
        ApprovalSpec,
        FallbackPolicy,
        ValueRef,
        Requirement,
        InformationFlowRule,
        FieldManifest,
        Citation,
        CitationManifest,
        # M24 — Break-glass + Persistent approval + Retention / Legal Hold
        BreakGlassRequest,
        ApprovalRecord,
        ApprovalDecision,
        LifecyclePolicy,
        LegalHold,
        DeletionJob,
    ]
    out: dict[str, Any] = {}
    for t in types:
        out[t.__name__] = t.model_json_schema()
    return out
