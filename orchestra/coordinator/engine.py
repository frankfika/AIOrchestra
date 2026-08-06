"""Coordinator Engine — the heart of LIT-004.

The Coordinator walks a Plan in topological order. For each node it:

1. Asks the :class:`Router` to pick a capability (Eligible Set + PDP).
2. Issues a :class:`NodeGrant` (locally signed dev credential).
3. Persists the grant to the Event Store.
4. Invokes the chosen :class:`Adapter`.
5. If the node declares ``requires_approval``, pauses and waits for a
   human decision (call :meth:`decide_approval`).
6. Records :class:`AuditEvent` for every step.
7. Builds a :class:`SignedReceipt` per node and verifies it on the way
   out (catches a bad signature before the user sees a result).

P0 has *one* pre-approved Fallback per node. The Router handles this; if
all primary candidates are denied by the PDP, the Router returns the
fallback manifest. The Coordinator surfaces the Fallback as a
``fallback.triggered`` event so the audit timeline shows the swap.

M3 XFR-001 — when the Coordinator is built with an ``egress_pep``, every
Adapter whose manifest declares ``egress_view_name`` runs its inputs
through the PEP first. The PEP replaces the inputs with the projected
payload (only the FieldManifest's allowed_fields, after redactions) and
the audit timeline records an ``io.sent`` event with the projected
digest — never the raw payload.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from orchestra.adapters.base import Adapter, AdapterRequest
from orchestra.coordinator.event_store import EventStore
from orchestra.coordinator.node_grant import NodeGrantIssuer
from orchestra.coordinator.receipt import ReceiptBuilder
from orchestra.core.errors import ContractViolation, OrchestraError
from orchestra.core.ids import new_id
from orchestra.core.schema import (
    AuditEvent,
    DataView,
    EventKind,
    ExecutionPlan,
    NodeRunState,
    PlanNode,
    RoutingDecision,
    SecurityLabel,
    TaskRunState,
)
from orchestra.core.time import utc_now_iso
from orchestra.registry.policy import PolicyEngine, default_p0_rules
from orchestra.registry.router import Router
from orchestra.templates.contract_review import (
    CONTRACT_REVIEW_TEMPLATE,
    build_contract_review_plan,
    get_default_purpose,
)
from orchestra.xfr.egress_pep import EgressDenied, EgressPEP

ApprovalHandler = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]
"""Callable invoked when the Coordinator needs a human decision.

Signature: ``(task_run_id, node_id, request_payload) -> decision_payload``
The default in :class:`Coordinator` blocks on an :class:`asyncio.Event`
that :meth:`Coordinator.decide_approval` sets. Tests inject their own.
"""


@dataclass
class CoordinatorResult:
    task_run_id: str
    plan: ExecutionPlan
    state: TaskRunState
    node_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    receipts: list[dict[str, Any]] = field(default_factory=dict)
    error: str | None = None


class Coordinator:
    def __init__(
        self,
        *,
        store: EventStore,
        router: Router,
        adapters: dict[str, Adapter],
        grant_issuer: NodeGrantIssuer,
        receipt_builder: ReceiptBuilder,
        approval_handler: ApprovalHandler | None = None,
        egress_pep: EgressPEP | None = None,
    ) -> None:
        self._store = store
        self._router = router
        self._adapters = adapters
        self._grant_issuer = grant_issuer
        self._receipt_builder = receipt_builder
        # Internal approval gate: an asyncio.Event the default handler waits
        # on. ``decide_approval`` sets it.
        self._approval_events: dict[tuple[str, str], tuple[asyncio.Event, dict[str, Any]]] = {}
        self._approval_handler = approval_handler or self._default_approval_handler
        # ``run()`` stashes the original initial_inputs here so the
        # in-process "ingest_contract" node can re-derive the contract
        # text without re-reading the request.
        self._initial_inputs: dict[str, Any] | None = None
        # M3 XFR-001 — if set, the Coordinator runs every public-capability
        # adapter call through the EgressPEP. Local tools, sinks, and
        # in-process nodes never see the PEP.
        self._egress_pep: EgressPEP | None = egress_pep

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        *,
        task_run_id: str,
        contract_id: str,
        data_label: SecurityLabel,
        initial_inputs: dict[str, Any],
        budget_usd: float = 1.0,
    ) -> CoordinatorResult:
        """Run the full Contract Review flow.

        ``initial_inputs`` must include ``contract_text`` (used by the
        local extractor) and ``vendor_id`` (used by the public model).
        """
        if "contract_text" not in initial_inputs:
            raise ContractViolation("initial_inputs must include 'contract_text'")
        if "vendor_id" not in initial_inputs:
            raise ContractViolation("initial_inputs must include 'vendor_id'")
        region = "local"
        self._initial_inputs = dict(initial_inputs)
        purpose = get_default_purpose()

        # Reserve the task row up front.
        self._store.upsert_task_run(
            task_run_id=task_run_id,
            contract_id=contract_id,
            template_id=CONTRACT_REVIEW_TEMPLATE.template_id,
            state=TaskRunState.CREATED,
        )
        self._emit(
            task_run_id=task_run_id,
            node_run_id=None,
            kind=EventKind.TASK_RECEIVED,
            payload={
                "contract_id": contract_id,
                "purpose": purpose.code,
                "data_label": data_label.model_dump(),
                "initial_inputs_keys": list(initial_inputs.keys()),
            },
        )

        # Plan the whole graph up front (Eager Planning) so the audit
        # timeline shows the whole topology before execution starts.
        routing_results: list[RoutingDecision] = []
        capability_bindings: dict[str, str] = {}
        manifest_bindings: dict[str, str] = {}
        budget_remaining = budget_usd
        for spec in CONTRACT_REVIEW_TEMPLATE.nodes:
            if spec.requires_approval:
                # The human-approval node is bound to a synthetic HUMAN
                # capability that lives in the manifest store at runtime
                # via a virtual capability. For P0 we skip the Router
                # (the human is the capability) and bind directly.
                cap_id = "human.approver"
                capability_bindings[spec.node_id] = cap_id
                manifest_bindings[spec.node_id] = "manifest:human-approver"
                continue
            result = self._router.route(
                node=spec,
                data_label=data_label,
                purpose_code=purpose.code,
                region=region,
                budget_remaining_usd=budget_remaining,
            )
            routing_results.append(result.decision)
            capability_bindings[spec.node_id] = result.decision.chosen_capability_id
            manifest_bindings[spec.node_id] = result.decision.chosen_manifest_id
            # Update the budget conservatively: subtract the chosen
            # capability's estimate.
            try:
                m = self._router._store.get(result.decision.chosen_capability_id)
                budget_remaining -= m.cost_estimate_usd
            except Exception:  # noqa: BLE001
                pass

        plan = build_contract_review_plan(
            contract_id=contract_id,
            routing=routing_results,
            capability_bindings=capability_bindings,
            manifest_bindings=manifest_bindings,
        )
        # Plan digest becomes the binding identifier for receipts.
        plan_signature = self._sign_plan(plan)
        plan = plan.model_copy(update={"signature": plan_signature})

        self._store.upsert_task_run(
            task_run_id=task_run_id,
            contract_id=contract_id,
            template_id=CONTRACT_REVIEW_TEMPLATE.template_id,
            state=TaskRunState.PLANNED,
            plan_id=plan.plan_id,
        )
        self._emit(
            task_run_id=task_run_id,
            node_run_id=None,
            kind=EventKind.PLAN_CREATED,
            payload={
                "plan_id": plan.plan_id,
                "plan_digest": plan.digest(),
                "nodes": [n.node_id for n in plan.nodes],
            },
        )
        self._emit(
            task_run_id=task_run_id,
            node_run_id=None,
            kind=EventKind.PLAN_SIGNED,
            payload={"plan_digest": plan.digest(), "signed_by": plan.signed_by},
        )

        # Execute in template order. The template is mostly sequential;
        # ``public_research`` runs after ``extract_facts_local``; ``merge``
        # waits on both ``extract_facts_local`` and ``public_research``.
        node_results: dict[str, dict[str, Any]] = {}
        receipts_out: list[dict[str, Any]] = []
        state = TaskRunState.RUNNING
        self._store.update_task_state(task_run_id, state)

        try:
            await self._exec_node(
                task_run_id=task_run_id,
                plan_node=plan.nodes[0],  # ingest_contract
                previous_outputs={},
                node_results=node_results,
            )
            await self._exec_node(
                task_run_id=task_run_id,
                plan_node=plan.nodes[1],  # extract_facts_local
                previous_outputs=node_results["ingest_contract"],
                node_results=node_results,
            )
            await self._exec_node(
                task_run_id=task_run_id,
                plan_node=plan.nodes[2],  # public_research
                previous_outputs={
                    "facts": node_results["extract_facts_local"].get("facts", {}),
                    "query": (
                        f"vendor={node_results['extract_facts_local'].get('facts', {}).get('vendor_name', 'unknown')}; "
                        "industry classification; public registry lookup"
                    ),
                },
                node_results=node_results,
            )
            await self._exec_node(
                task_run_id=task_run_id,
                plan_node=plan.nodes[3],  # merge
                previous_outputs={
                    "facts": node_results["extract_facts_local"].get("facts", {}),
                    "research": node_results["public_research"].get("research", {}),
                },
                node_results=node_results,
                force_capability_id="local.contract-extractor",
            )
            # Approval — pause
            decision_payload = await self._approval_handler(
                task_run_id, "human_approval", node_results["merge"]
            )
            node_results["human_approval"] = decision_payload
            self._emit(
                task_run_id=task_run_id,
                node_run_id=None,
                kind=EventKind.NODE_APPROVED
                if decision_payload.get("decision") == "approve"
                else EventKind.NODE_REJECTED,
                payload={
                    "node_id": "human_approval",
                    "decided_by": decision_payload.get("decided_by", "human"),
                    "rationale": decision_payload.get("rationale", ""),
                },
            )
            if decision_payload.get("decision") != "approve":
                state = TaskRunState.CANCELLED
                self._store.update_task_state(task_run_id, state)
                self._emit(
                    task_run_id=task_run_id,
                    node_run_id=None,
                    kind=EventKind.TASK_FAILED,
                    payload={"reason": "rejected at human_approval"},
                )
                return CoordinatorResult(
                    task_run_id=task_run_id,
                    plan=plan,
                    state=state,
                    node_results=node_results,
                    receipts=receipts_out,
                    error="rejected at human_approval",
                )
            await self._exec_node(
                task_run_id=task_run_id,
                plan_node=plan.nodes[5],  # write_sink
                previous_outputs=node_results["merge"],
                node_results=node_results,
                approval_payload=decision_payload,
            )
            state = TaskRunState.SUCCEEDED
            self._store.update_task_state(task_run_id, state)
            self._emit(
                task_run_id=task_run_id,
                node_run_id=None,
                kind=EventKind.TASK_COMPLETED,
                payload={"node_results_keys": list(node_results.keys())},
            )
        except Exception as e:  # noqa: BLE001
            state = TaskRunState.FAILED
            self._store.update_task_state(task_run_id, state)
            self._emit(
                task_run_id=task_run_id,
                node_run_id=None,
                kind=EventKind.TASK_FAILED,
                payload={"reason": str(e), "error_type": type(e).__name__},
            )
            return CoordinatorResult(
                task_run_id=task_run_id,
                plan=plan,
                state=state,
                node_results=node_results,
                receipts=receipts_out,
                error=str(e),
            )

        # Final pass: verify all receipts.
        verified = self._verify_receipts(task_run_id)
        return CoordinatorResult(
            task_run_id=task_run_id,
            plan=plan,
            state=state,
            node_results=node_results,
            receipts=verified,
        )

    async def decide_approval(
        self,
        task_run_id: str,
        node_id: str,
        *,
        decision: str,
        decided_by: str,
        rationale: str = "",
    ) -> None:
        """Resolve a pending human approval. Call from the API/UI when
        a human clicks approve/reject.
        """
        key = (task_run_id, node_id)
        if key not in self._approval_events:
            raise OrchestraError(f"no pending approval for {key}")
        event, request_payload = self._approval_events[key]
        request_payload["decision"] = decision
        request_payload["decided_by"] = decided_by
        request_payload["decided_at"] = utc_now_iso()
        request_payload["rationale"] = rationale
        # Persist the approval record.
        self._store.save_approval(
            {
                "approval_id": new_id(),
                "task_run_id": task_run_id,
                "node_id": node_id,
                "requested_at": request_payload["requested_at"],
                "decided_at": request_payload["decided_at"],
                "decision": decision,
                "decided_by": decided_by,
                "rationale": rationale,
            }
        )
        event.set()

    # ------------------------------------------------------------------
    # Internal: per-node execution
    # ------------------------------------------------------------------

    async def _exec_node(
        self,
        *,
        task_run_id: str,
        plan_node: PlanNode,
        previous_outputs: dict[str, Any],
        node_results: dict[str, dict[str, Any]],
        force_capability_id: str | None = None,
        approval_payload: dict[str, Any] | None = None,
    ) -> None:
        node_run_id = new_id()
        cap_id = force_capability_id or plan_node.capability_id
        # Resolve the manifest snapshot id.
        manifest = self._router._store.get(cap_id)
        manifest_id = manifest.manifest_id()
        self._store.upsert_node_run(
            node_run_id=node_run_id,
            task_run_id=task_run_id,
            node_id=plan_node.node_id,
            state=NodeRunState.RUNNING,
            capability_id=cap_id,
            manifest_id=manifest_id,
        )
        self._emit(
            task_run_id=task_run_id,
            node_run_id=node_run_id,
            kind=EventKind.NODE_STARTED,
            payload={
                "node_id": plan_node.node_id,
                "capability_id": cap_id,
                "manifest_id": manifest_id,
            },
        )

        # The "merge" node is an in-process deterministic function, not
        # an HTTP Adapter call. Same for "ingest_contract" — it just
        # passes the initial contract_text through.
        if plan_node.node_id in ("merge", "ingest_contract"):
            if plan_node.node_id == "ingest_contract":
                # Lift the initial inputs into the node_results bucket so
                # downstream nodes can read ``contract_text`` and
                # ``vendor_id``. The Coordinator carries the original
                # initial_inputs in ``self._initial_inputs``.
                result_payload = dict(self._initial_inputs or previous_outputs)
            else:
                result_payload = _deterministic_merge(previous_outputs)
            self._store.update_node_state(
                node_run_id,
                NodeRunState.SUCCEEDED,
                output=result_payload,
                started=True,
                ended=True,
            )
            node_results[plan_node.node_id] = result_payload
            self._emit(
                task_run_id=task_run_id,
                node_run_id=node_run_id,
                kind=EventKind.NODE_SUCCEEDED,
                payload={"node_id": plan_node.node_id, "outputs_keys": list(result_payload.keys())},
            )
            return

        # Issue a Node Grant. The grant's data view is the *first* input
        # view of the plan node (P0 is single-view per node).
        data_view = plan_node.input_views[0] if plan_node.input_views else DataView(
            name="default", shape="reference", fields=[]
        )
        grant = self._grant_issuer.issue(
            task_run_id=task_run_id,
            node_run_id=node_run_id,
            task_id=plan_node.node_id,  # P0 only has one contract per run
            node_id=plan_node.node_id,
            capability_id=cap_id,
            manifest_id=manifest_id,
            data_view=data_view,
            purpose=plan_node.purpose,
        )
        self._store.save_grant(
            {**grant.model_dump(mode="json"), "signature": grant.signature}
        )
        self._emit(
            task_run_id=task_run_id,
            node_run_id=node_run_id,
            kind=EventKind.GRANT_ISSUED,
            payload={
                "grant_id": grant.grant_id,
                "capability_id": cap_id,
                "data_view": data_view.model_dump(),
                "expires_at": grant.expires_at,
            },
        )

        # Look up the adapter; raise clearly if missing.
        if cap_id not in self._adapters:
            raise OrchestraError(
                f"no adapter registered for capability {cap_id!r}"
            )
        adapter = self._adapters[cap_id]
        started_at = utc_now_iso()
        inputs: dict[str, Any] = dict(previous_outputs)
        if approval_payload is not None:
            inputs["approval"] = approval_payload

        # M3 XFR-001 — if the capability declares an egress_view_name AND
        # an EgressPEP is configured, run the inputs through the PEP
        # before invoking the Adapter. The PEP projects the payload
        # down to the FieldManifest's allowed_fields and redactions; the
        # adapter never sees fields the manifest does not list.
        egress_projection: dict[str, Any] | None = None
        egress_manifest: dict[str, Any] | None = None
        egress_dropped: list[str] = []
        egress_projected_bytes: int = 0
        if manifest.egress_view_name and self._egress_pep is not None:
            try:
                projected, manifest_dict = self._egress_pep.project(
                    capability_id=cap_id,
                    view_name=manifest.egress_view_name,
                    payload=inputs,
                )
            except EgressDenied as e:
                # The PEP refused the call. Emit a denial audit event so
                # the timeline shows *why* the node failed, then fail the
                # node.
                self._emit(
                    task_run_id=task_run_id,
                    node_run_id=node_run_id,
                    kind=EventKind.POLICY_DECISION,
                    payload={
                        "node_id": plan_node.node_id,
                        "capability_id": cap_id,
                        "view_name": manifest.egress_view_name,
                        "decision": "deny",
                        "reason": str(e),
                        "policy": "xfr-001.egress-pep",
                    },
                )
                self._store.update_node_state(
                    node_run_id, NodeRunState.FAILED, started=True, ended=True
                )
                self._emit(
                    task_run_id=task_run_id,
                    node_run_id=node_run_id,
                    kind=EventKind.NODE_FAILED,
                    payload={
                        "node_id": plan_node.node_id,
                        "error": str(e),
                        "error_type": "EgressDenied",
                        "policy": "xfr-001.egress-pep",
                    },
                )
                raise
            # The PEP succeeded. Compute dropped_fields so the audit
            # event can record exactly what was filtered.
            from orchestra.xfr.projector import FieldProjector

            proj = FieldProjector()
            proj_result = proj.project(
                _manifest_for_projection(manifest_dict), inputs,
            )
            egress_projection = projected
            egress_manifest = manifest_dict
            egress_dropped = proj_result.dropped_fields
            egress_projected_bytes = proj_result.projected_bytes
            inputs = projected

        t0 = time.monotonic()
        try:
            adapter_result = await adapter.invoke(
                AdapterRequest(
                    grant=grant,
                    inputs=inputs,
                    data_view=data_view,
                    purpose=plan_node.purpose,
                    timeout_ms=plan_node.timeout_ms,
                    metadata={"decided_by": approval_payload.get("decided_by", "human")} if approval_payload else {},
                )
            )
        except Exception as e:  # noqa: BLE001
            self._store.update_node_state(
                node_run_id, NodeRunState.FAILED, started=True, ended=True
            )
            self._emit(
                task_run_id=task_run_id,
                node_run_id=node_run_id,
                kind=EventKind.NODE_FAILED,
                payload={"node_id": plan_node.node_id, "error": str(e), "error_type": type(e).__name__},
            )
            raise
        latency_ms = int((time.monotonic() - t0) * 1000)
        ended_at = utc_now_iso()

        # I/O intent + sent + received events (P0 audit shape).
        self._emit(
            task_run_id=task_run_id,
            node_run_id=node_run_id,
            kind=EventKind.IO_INTENT,
            payload={"node_id": plan_node.node_id, "capability_id": cap_id, "data_view": data_view.name},
        )
        if egress_manifest is not None:
            # XFR-001 path: the io.sent event carries the projected
            # digest + dropped fields. The raw payload never appears in
            # the audit timeline.
            io_sent_payload = {
                "node_id": plan_node.node_id,
                "capability_id": cap_id,
                "view_name": manifest.egress_view_name,
                "manifest_id": egress_manifest.get("manifest_id"),
                "projected_digest": _projected_digest(egress_projection),
                "projected_bytes": egress_projected_bytes,
                "dropped_fields": egress_dropped,
                "latency_ms": latency_ms,
            }
        else:
            io_sent_payload = {
                "node_id": plan_node.node_id,
                "capability_id": cap_id,
                "latency_ms": latency_ms,
            }
        self._emit(
            task_run_id=task_run_id,
            node_run_id=node_run_id,
            kind=EventKind.IO_SENT,
            payload=io_sent_payload,
        )
        self._emit(
            task_run_id=task_run_id,
            node_run_id=node_run_id,
            kind=EventKind.IO_RECEIVED,
            payload={
                "node_id": plan_node.node_id,
                "outputs_keys": list(adapter_result.outputs.keys()),
                "latency_ms": latency_ms,
            },
        )

        # Persist output, build receipt.
        self._store.update_node_state(
            node_run_id,
            NodeRunState.SUCCEEDED,
            output=adapter_result.outputs,
            started=True,
            ended=True,
        )
        node_results[plan_node.node_id] = adapter_result.outputs
        receipt = self._receipt_builder.build(
            task_run_id=task_run_id,
            node_run_id=node_run_id,
            node_id=plan_node.node_id,
            plan_digest=self._store.get_task_run(task_run_id).get("plan_id", "") or "",
            capability_id=cap_id,
            manifest_id=manifest_id,
            data_view=data_view.model_dump(),
            inputs=inputs,
            outputs=adapter_result.outputs,
            started_at=started_at,
            ended_at=ended_at,
            status="succeeded",
        )
        self._store.save_receipt(receipt)
        self._emit(
            task_run_id=task_run_id,
            node_run_id=node_run_id,
            kind=EventKind.RECEIPT_SIGNED,
            payload={"receipt_id": receipt.receipt_id, "node_id": plan_node.node_id},
        )
        self._emit(
            task_run_id=task_run_id,
            node_run_id=node_run_id,
            kind=EventKind.NODE_SUCCEEDED,
            payload={
                "node_id": plan_node.node_id,
                "outputs_keys": list(adapter_result.outputs.keys()),
                "latency_ms": latency_ms,
                "metadata": adapter_result.metadata,
            },
        )

    # ------------------------------------------------------------------
    # Internal: helpers
    # ------------------------------------------------------------------

    def _emit(
        self,
        *,
        task_run_id: str,
        node_run_id: str | None,
        kind: EventKind,
        payload: dict[str, Any],
    ) -> None:
        ev = AuditEvent(
            task_run_id=task_run_id,
            node_run_id=node_run_id,
            kind=kind,
            payload=payload,
        )
        self._store.append_event(ev)

    def _sign_plan(self, plan: ExecutionPlan) -> str:
        from orchestra.core.hashing import hmac_sign

        body = plan.model_dump(mode="json", exclude={"signature"})
        return hmac_sign(self._grant_issuer._key, body)

    def _verify_receipts(self, task_run_id: str) -> list[dict[str, Any]]:
        rows = self._store.get_receipts(task_run_id)
        verified: list[dict[str, Any]] = []
        for r in rows:
            from orchestra.core.schema import SignedReceipt

            receipt = SignedReceipt(
                receipt_id=r["receipt_id"],
                task_run_id=r["task_run_id"],
                node_run_id=r["node_run_id"],
                node_id=r["node_id"],
                envelope=r["envelope"],
                created_at=str(r["created_at"]),
            )
            ok = self._receipt_builder.verify(receipt)
            r["verified"] = ok
            verified.append(r)
        return verified

    async def _default_approval_handler(
        self, task_run_id: str, node_id: str, request_payload: dict[str, Any]
    ) -> dict[str, Any]:
        # Public API path: wait for decide_approval().
        event = asyncio.Event()
        bucket: dict[str, Any] = {
            "requested_at": utc_now_iso(),
            "task_run_id": task_run_id,
            "node_id": node_id,
            "request": {k: v for k, v in request_payload.items() if isinstance(v, (str, int, float, bool))},
        }
        self._approval_events[(task_run_id, node_id)] = (event, bucket)
        self._store.save_approval(
            {
                "approval_id": new_id(),
                "task_run_id": task_run_id,
                "node_id": node_id,
                "requested_at": bucket["requested_at"],
            }
        )
        self._emit(
            task_run_id=task_run_id,
            node_run_id=None,
            kind=EventKind.NODE_AWAITING_APPROVAL,
            payload={"node_id": node_id},
        )
        await event.wait()
        return bucket


# ---------------------------------------------------------------------------
# In-process merge (deterministic, not an HTTP Adapter)
# ---------------------------------------------------------------------------


def _manifest_for_projection(manifest_dict: dict[str, Any]):
    """Re-hydrate a FieldManifest from a dict so the Projector can re-run."""
    from orchestra.core.schema import FieldManifest

    return FieldManifest.model_validate(manifest_dict)


def _projected_digest(projected: dict[str, Any] | None) -> str:
    """Stable digest of the projected payload (matches the PEP's digest)."""
    from orchestra.core.ids import digest_json

    return digest_json(projected or {})


def _deterministic_merge(inputs: dict[str, Any]) -> dict[str, Any]:
    facts = inputs.get("facts", {})
    research = inputs.get("research", {}) or {}
    # The public_research node may have been routed to either the public
    # OpenAI-compat model (which nests under ``public_summary``) or to
    # the A2A Reference Agent (which nests under ``a2a_artefact``).
    # Normalise: prefer public_summary when present, otherwise lift
    # a2a_artefact into the same shape.
    public_summary = research.get("public_summary", {})
    if not public_summary and "a2a_artefact" in research:
        artefact = research.get("a2a_artefact", {})
        # If the artefact has industry classification, treat it as the
        # public summary for the merge.
        public_summary = {
            "vendor_id": facts.get("vendor_id", "unknown"),
            "vendor_name": artefact.get("industry") or facts.get("vendor_name", "unknown"),
            "jurisdiction": facts.get("jurisdiction", "unknown"),
            "regulatory_actions": [],
            "source": "in-repo A2A Reference Agent",
        }
    industry_context = research.get("industry_context", {}) or research.get(
        "a2a_artefact", {}
    )
    risk_flags: list[str] = []
    contract_amount = facts.get("contract_amount", "")
    if contract_amount:
        digits = "".join(ch for ch in contract_amount if ch.isdigit() or ch == ".")
        try:
            val = float(digits) if digits else 0.0
        except ValueError:
            val = 0.0
        if "万" in contract_amount and val >= 1000:
            risk_flags.append("high-value-contract")
        elif "百万" in contract_amount or "billion" in contract_amount.lower():
            risk_flags.append("very-high-value-contract")
    if isinstance(public_summary, dict):
        for action in public_summary.get("regulatory_actions", []):
            sev = action.get("severity", "low")
            if sev in ("medium", "high"):
                risk_flags.append(f"regulatory-action-{sev}")
    return {
        "vendor_id": facts.get("vendor_id", "unknown"),
        "vendor_name": facts.get("vendor_name", public_summary.get("vendor_name", "unknown")),
        "facts": facts,
        "public_summary": public_summary,
        "industry_context": industry_context,
        "risk_flags": risk_flags,
        "summary": (
            f"Vendor {facts.get('vendor_name', '?')} "
            f"in {public_summary.get('jurisdiction', '?')}; "
            f"contract value: {contract_amount or 'unknown'}; "
            f"risk flags: {', '.join(risk_flags) if risk_flags else 'none'}."
        ),
    }


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_default_coordinator(
    *,
    store: EventStore,
    endpoints: dict[str, str] | None = None,
    egress_pep: EgressPEP | None = None,
) -> Coordinator:
    """Construct a Coordinator with the default registry, policy, and
    four reference Adapters.

    ``endpoints`` overrides the default Adapter endpoints — pass
    :func:`orchestra.adapters.servers.start_all_servers` output to pin
    the manifests to the actual ports the demo allocated.

    ``store`` is the :class:`EventStore` (PostgreSQL). The
    :class:`ManifestStore` is constructed internally from the bootstrap
    data; do not pass the manifest store as ``store`` — they are two
    different things with similar names.

    ``egress_pep`` is the M3 XFR-001 Egress PEP. When provided, every
    Adapter whose manifest declares ``egress_view_name`` runs its
    inputs through the PEP before invocation. ``None`` disables
    projection (the P0/P2 default for tests that only care about
    routing).
    """
    from orchestra.registry.bootstrap import load_default_manifests as _load

    manifest_store = _load(endpoints=endpoints or {})
    router = Router(manifest_store, default_p0_rules_engine())
    from orchestra.coordinator.node_grant import NodeGrantIssuer as _NGI
    from orchestra.coordinator.receipt import ReceiptBuilder as _RB
    from orchestra.core.hashing import hmac_keygen

    grant_key = hmac_keygen()
    receipt_key = hmac_keygen()
    grant_issuer = _NGI(grant_key)
    receipt_builder = _RB(receipt_key)
    adapters = _build_default_adapters(endpoints=endpoints or {})
    return Coordinator(
        store=store,
        router=router,
        adapters=adapters,
        grant_issuer=grant_issuer,
        receipt_builder=receipt_builder,
        egress_pep=egress_pep,
    )


def default_p0_rules_engine() -> PolicyEngine:
    return PolicyEngine(default_p0_rules())


def _build_default_adapters(endpoints: dict[str, str]) -> dict[str, Adapter]:
    from orchestra.adapters.a2a_reference import A2AReferenceAdapter
    from orchestra.adapters.local_model import LocalModelAdapter
    from orchestra.adapters.mock_sink import MockSinkAdapter
    from orchestra.adapters.openai_compat import OpenAICompatAdapter

    return {
        "local.contract-extractor": LocalModelAdapter(
            endpoint=endpoints.get(
                "local.contract-extractor", "http://127.0.0.1:8101/v1/extract"
            )
        ),
        "public.openai-compat": OpenAICompatAdapter(
            endpoint=endpoints.get(
                "public.openai-compat", "http://127.0.0.1:8102/v1/chat/completions"
            )
        ),
        "a2a.reference-agent": A2AReferenceAdapter(
            endpoint=endpoints.get("a2a.reference-agent", "http://127.0.0.1:8103/a2a/v1")
        ),
        "sink.mock-procurement": MockSinkAdapter(
            endpoint=endpoints.get("sink.mock-procurement", "http://127.0.0.1:8104/sink")
        ),
    }
