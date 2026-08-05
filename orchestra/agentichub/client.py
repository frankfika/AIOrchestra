"""M4 INT-AH-001 — AgenticHub MCP/API Adapter.

The AgenticHub Adapter mirrors the Dify Task Tool client but speaks
the AgenticHub wire format (HTTP + JSON-RPC 2.0). The contract is the
same: three delegation modes, identical governance state payload, and
an explicit ``integration_level`` so the host knows how strongly
Orchestra governs the call.

Wire shape (MCP-over-HTTP variant):

  * ``POST {AGENTICHUB_BASE_URL}/api/v1/orchestra/submit``  (JSON body)
  * ``GET  {AGENTICHUB_BASE_URL}/api/v1/orchestra/tasks/{id}``
  * ``POST {AGENTICHUB_BASE_URL}/api/v1/orchestra/tasks/{id}/decide``
  * ``GET  {AGENTICHUB_BASE_URL}/api/v1/orchestra/tasks/{id}/events``
  * ``GET  {AGENTICHUB_BASE_URL}/api/v1/orchestra/tasks/{id}/grants``

The Adapter never depends on AgenticHub's internal SDK. The host
platform translates the SDK call into one of the above HTTP calls
and surfaces the governance state to the user.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from orchestra.integrations.delegation import (
    DelegationMode,
    IntegrationLevel,
    governance_state_for,
)


@dataclass
class AgenticHubResult:
    task_run_id: str
    state: str
    plan_id: Optional[str]
    audit_url: str
    route_url: str
    error: Optional[str]
    delegation: dict[str, Any] = field(default_factory=dict)
    attempt: int = 0
    duration_ms: int = 0

    def to_agentichub_output(self) -> dict[str, Any]:
        """Shape handed to AgenticHub's tool node renderer.

        ``governance`` mirrors the Dify shape so a platform that
        supports both integrations can render the same UI.
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


class AgenticHubTaskTool:
    """AgenticHub-side client for the Orchestra Task Tool.

    Identical semantics to :class:`orchestra.dify.task_tool.DifyTaskTool`
    but speaks AgenticHub's HTTP shape. The two adapters intentionally
    share the same :class:`DelegationMode` enum so a platform that
    swaps one for the other cannot accidentally re-interpret the
    ownership contract.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        timeout_s: float = 30.0,
        *,
        mode: DelegationMode = DelegationMode.DELEGATE_TASK,
        integration_level: IntegrationLevel = IntegrationLevel.ENFORCE,
        max_polls: int = 60,
        poll_interval_s: float = 1.0,
    ) -> None:
        # The AgenticHub Adapter prefixes its paths with
        # ``/api/v1/orchestra`` so the same Orchestra server can serve
        # Dify and AgenticHub on the same port without collisions.
        self._base = base_url.rstrip("/")
        self._prefix = "/api/v1/orchestra"
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

    async def submit_contract(
        self,
        *,
        contract_id: str,
        contract_text: str,
        vendor_id: str,
        budget_usd: float = 1.0,
    ) -> AgenticHubResult:
        t0 = time.monotonic()
        r = await self._client.post(
            f"{self._prefix}/submit",
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
            return self._make_result(
                task_run_id=task_run_id,
                state=initial_state,
                plan_id=data.get("plan_id"),
                error=data.get("error"),
                duration_ms=int((time.monotonic() - t0) * 1000),
                attempt=0,
            )

        # delegate-task: poll until terminal.
        terminal = {"succeeded", "failed", "cancelled"}
        for attempt in range(1, self._max_polls + 1):
            await asyncio.sleep(self._poll_interval_s)
            r2 = await self._client.get(f"{self._prefix}/tasks/{task_run_id}")
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
        return self._make_result(
            task_run_id=task_run_id,
            state="still-running",
            plan_id=None,
            error=f"task did not finish within {self._max_polls * self._poll_interval_s:.0f}s",
            duration_ms=int((time.monotonic() - t0) * 1000),
            attempt=self._max_polls,
        )

    async def get_state(self, task_run_id: str) -> dict[str, Any]:
        r = await self._client.get(f"{self._prefix}/tasks/{task_run_id}")
        r.raise_for_status()
        return r.json()

    async def approve(
        self,
        task_run_id: str,
        decided_by: str = "agentichub",
        rationale: str = "",
    ) -> None:
        await self._decide(task_run_id, decision="approve", decided_by=decided_by, rationale=rationale)

    async def reject(
        self,
        task_run_id: str,
        decided_by: str = "agentichub",
        rationale: str = "",
    ) -> None:
        await self._decide(task_run_id, decision="reject", decided_by=decided_by, rationale=rationale)

    async def _decide(
        self, task_run_id: str, *, decision: str, decided_by: str, rationale: str
    ) -> None:
        r = await self._client.post(
            f"{self._prefix}/tasks/{task_run_id}/decide",
            json={"decision": decision, "decided_by": decided_by, "rationale": rationale},
        )
        r.raise_for_status()

    def _make_result(
        self,
        *,
        task_run_id: str,
        state: str,
        plan_id: str | None,
        error: str | None,
        duration_ms: int,
        attempt: int,
    ) -> AgenticHubResult:
        governance = governance_state_for(
            mode=self._mode,
            task_state=state,
            plan_id=plan_id,
            audit_url=f"{self._base}{self._prefix}/tasks/{task_run_id}/events",
            route_url=f"{self._base}{self._prefix}/tasks/{task_run_id}/grants",
            error=error,
        )
        return AgenticHubResult(
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
