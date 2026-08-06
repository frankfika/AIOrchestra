"""Dify Task Tool reference client — M4 enriched with 3 delegation modes.

This is the *Python* side of the integration. A real Dify plugin would
wrap this in the Dify plugin SDK; the contract is the HTTP call shape.

Three delegation modes (M4):

  * ``delegate-task``  — full task lifecycle owned by Orchestra.
                         Dify submits once, polls the canonical state.
  * ``delegate-node``  — Orchestra owns a single node (sub-graph).
                         Dify's workflow drives retry/cancel; the
                         call is idempotent at the Dify level.
  * ``observe-only``   — Dify runs the work; Orchestra is a witness
                         and writes audit events only.

Usage from a Dify workflow node::

    tool = DifyTaskTool(
        base_url="http://127.0.0.1:8000",
        mode=DelegationMode.DELEGATE_TASK,
        integration_level=IntegrationLevel.ENFORCE,
    )
    result = await tool.submit_contract(
        contract_id="ctr-001",
        contract_text=...,
        vendor_id="demo-vendor-001",
    )
    # result.to_dify_output() yields the governance state the Dify
    # workflow node renders (task_run_id, plan_id, audit_url,
    # route_url, delegation, error).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from orchestra.integrations.delegation import (
    DelegationMode,
    IntegrationLevel,
    governance_state_for,
)


@dataclass
class DifyTaskToolResult:
    task_run_id: str
    state: str
    plan_id: str | None
    audit_url: str
    route_url: str
    error: str | None
    delegation: dict[str, Any] = field(default_factory=dict)
    # M4 — track how many retries the host has attempted. The
    # contract tells the host whether to retry (delegate-task) or to
    # always back off (observe-only / delegate-node).
    attempt: int = 0
    duration_ms: int = 0

    def to_dify_output(self) -> dict[str, Any]:
        """Shape we hand back to the Dify workflow node.

        The ``governance`` block carries the delegation contract so
        the Dify UI can render "who owns retry / cancel" without
        extra round-trips.
        """
        return {
            "task_run_id": self.task_run_id,
            "state": self.state,
            "plan_id": self.plan_id,
            "audit_url": self.audit_url,
            "route_url": self.route_url,
            "error": self.error or "",
            "attempt": self.attempt,
            "duration_ms": self.duration_ms,
            "governance": self.delegation,
        }


class DifyTaskTool:
    """Dify-side client for the Orchestra Task Tool.

    The same class serves the three delegation modes. The constructor
    takes a ``mode`` and ``integration_level`` so the platform plugin
    surfaces the right tooltip to the user.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        timeout_s: float = 30.0,
        *,
        mode: DelegationMode = DelegationMode.DELEGATE_TASK,
        integration_level: IntegrationLevel = IntegrationLevel.ENFORCE,
        # M4 — maximum number of polls for delegate-task before the
        # host gives up. delegate-node and observe-only do not poll
        # (the host drives the workflow).
        max_polls: int = 60,
        poll_interval_s: float = 1.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self._base, timeout=timeout_s)
        self._mode = mode
        self._level = integration_level
        self._max_polls = max_polls
        self._poll_interval_s = poll_interval_s

    @property
    def mode(self) -> DelegationMode:
        return self._mode

    @property
    def integration_level(self) -> IntegrationLevel:
        return self._level

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # M4 — submit + poll (delegate-task only) / submit (others)
    # ------------------------------------------------------------------

    async def submit_contract(
        self,
        *,
        contract_id: str,
        contract_text: str,
        vendor_id: str,
        budget_usd: float = 1.0,
    ) -> DifyTaskToolResult:
        """Submit a contract review.

        In ``delegate-task`` mode the call also polls until the task
        reaches a terminal state, because the host delegates the
        entire lifecycle. In the other two modes the call returns
        the initial state and the host drives the workflow.
        """
        t0 = time.monotonic()
        r = await self._client.post(
            "/tasks",
            json={
                "contract_id": contract_id,
                "contract_text": contract_text,
                "vendor_id": vendor_id,
                "budget_usd": budget_usd,
            },
        )
        r.raise_for_status()
        data = r.json()
        task_run_id = data["task_run_id"]
        initial_state = data["state"]

        if self._mode != DelegationMode.DELEGATE_TASK:
            # The host drives polling / workflow. Return the initial
            # state immediately.
            return self._make_result(
                task_run_id=task_run_id,
                state=initial_state,
                plan_id=data.get("plan_id"),
                error=data.get("error"),
                duration_ms=int((time.monotonic() - t0) * 1000),
                attempt=0,
            )

        # delegate-task: poll until the task reaches a terminal state.
        # Terminal states: succeeded, failed, cancelled. The host's
        # retry_owner is "orchestra" so the host never re-issues
        # /tasks on its own.
        terminal = {"succeeded", "failed", "cancelled"}
        for attempt in range(1, self._max_polls + 1):
            await asyncio_sleep(self._poll_interval_s)
            r2 = await self._client.get(f"/tasks/{task_run_id}")
            r2.raise_for_status()
            data2 = r2.json()
            state = data2.get("state", initial_state)
            if state in terminal:
                return self._make_result(
                    task_run_id=task_run_id,
                    state=state,
                    plan_id=data2.get("plan_id"),
                    error=data2.get("error"),
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    attempt=attempt,
                )
        # Timed out polling; the host's retry_owner is orchestra, so
        # the host surfaces "still running, see audit_url" instead of
        # re-issuing.
        return self._make_result(
            task_run_id=task_run_id,
            state="still-running",
            plan_id=None,
            error=f"task did not finish within {self._max_polls * self._poll_interval_s:.0f}s",
            duration_ms=int((time.monotonic() - t0) * 1000),
            attempt=self._max_polls,
        )

    # ------------------------------------------------------------------
    # Approval + status
    # ------------------------------------------------------------------

    async def get_state(self, task_run_id: str) -> dict[str, Any]:
        r = await self._client.get(f"/tasks/{task_run_id}")
        r.raise_for_status()
        return r.json()

    async def approve(
        self,
        task_run_id: str,
        decided_by: str = "dify",
        rationale: str = "",
    ) -> None:
        r = await self._client.post(
            f"/tasks/{task_run_id}/approve",
            json={"decided_by": decided_by, "rationale": rationale},
        )
        r.raise_for_status()

    async def reject(
        self,
        task_run_id: str,
        decided_by: str = "dify",
        rationale: str = "",
    ) -> None:
        r = await self._client.post(
            f"/tasks/{task_run_id}/reject",
            json={"decided_by": decided_by, "rationale": rationale},
        )
        r.raise_for_status()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _make_result(
        self,
        *,
        task_run_id: str,
        state: str,
        plan_id: str | None,
        error: str | None,
        duration_ms: int,
        attempt: int,
    ) -> DifyTaskToolResult:
        governance = governance_state_for(
            mode=self._mode,
            task_state=state,
            plan_id=plan_id,
            audit_url=f"{self._base}/tasks/{task_run_id}/events",
            route_url=f"{self._base}/tasks/{task_run_id}/grants",
            error=error,
        )
        return DifyTaskToolResult(
            task_run_id=task_run_id,
            state=state,
            plan_id=plan_id,
            audit_url=governance["audit_url"],
            route_url=governance["route_url"],
            error=error,
            delegation=governance["delegation"],
            attempt=attempt,
            duration_ms=duration_ms,
        )


# A tiny sleep helper so the test suite can monkey-patch it.
async def asyncio_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
