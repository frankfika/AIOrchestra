"""Dify Task Tool reference client.

This is the *Python* side of the integration. A real Dify plugin would
wrap this in the Dify plugin SDK; the contract is the HTTP call shape.

Usage from a Dify workflow node:

    tool = DifyTaskTool(base_url="http://127.0.0.1:8000")
    result = await tool.submit_contract(
        contract_id="ctr-001",
        contract_text=...,
        vendor_id="demo-vendor-001",
    )
    # The result contains:
    #   - task_run_id  (string)
    #   - audit_url    (deep link into the timeline)
    #   - route_url    (deep link into the routing decisions)
    #   - state        (initial state)

The approval step happens out-of-band (a human uses the Orchestra UI to
approve/reject). Dify would poll ``GET /tasks/{id}`` until the state is
``SUCCEEDED`` or ``CANCELLED``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class DifyTaskToolResult:
    task_run_id: str
    state: str
    plan_id: str | None
    audit_url: str
    route_url: str
    error: str | None

    def to_dify_output(self) -> dict[str, Any]:
        """Shape we hand back to the Dify workflow node."""
        return {
            "task_run_id": self.task_run_id,
            "state": self.state,
            "plan_id": self.plan_id,
            "audit_url": self.audit_url,
            "route_url": self.route_url,
            "error": self.error or "",
        }


class DifyTaskTool:
    def __init__(self, base_url: str = "http://127.0.0.1:8000", timeout_s: float = 30.0) -> None:
        self._base = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self._base, timeout=timeout_s)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def submit_contract(
        self,
        *,
        contract_id: str,
        contract_text: str,
        vendor_id: str,
        budget_usd: float = 1.0,
    ) -> DifyTaskToolResult:
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
        return DifyTaskToolResult(
            task_run_id=task_run_id,
            state=data["state"],
            plan_id=data.get("plan_id"),
            audit_url=f"{self._base}/tasks/{task_run_id}/events",
            route_url=f"{self._base}/tasks/{task_run_id}/grants",
            error=data.get("error"),
        )

    async def get_state(self, task_run_id: str) -> dict[str, Any]:
        r = await self._client.get(f"/tasks/{task_run_id}")
        r.raise_for_status()
        return r.json()

    async def approve(
        self, task_run_id: str, decided_by: str = "dify", rationale: str = ""
    ) -> None:
        r = await self._client.post(
            f"/tasks/{task_run_id}/approve",
            json={"decided_by": decided_by, "rationale": rationale},
        )
        r.raise_for_status()
