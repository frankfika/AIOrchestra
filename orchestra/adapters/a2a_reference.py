"""A2A-style reference agent Adapter.

The in-repo A2A Reference Agent implements the A2A wire format
(``/.well-known/agent.json`` Agent Card + ``/a2a/v1`` JSON-RPC task
endpoint). The Adapter sends a ``tasks/send`` JSON-RPC request, polls
for completion, and returns the agent's structured artefact.

A real public A2A agent (e.g. a partner's industry-data agent) would slot
in here with no change to the Adapter — only the endpoint URL.
"""
from __future__ import annotations

from typing import Any

import httpx

from orchestra.adapters.base import AdapterRequest, AdapterResult


class A2AReferenceAdapter:
    name = "a2a.reference-agent"

    def __init__(self, endpoint: str = "http://127.0.0.1:8103/a2a/v1") -> None:
        self._endpoint = endpoint
        self._card_url = endpoint.rsplit("/a2a/v1", 1)[0] + "/.well-known/agent.json"

    async def _card(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(self._card_url)
            r.raise_for_status()
            return r.json()

    async def invoke(self, request: AdapterRequest) -> AdapterResult:
        query = request.inputs.get("query", "")
        if not query:
            raise RuntimeError("A2AReferenceAdapter requires inputs['query'] (non-empty str)")
        await self._card()  # warm / verify reachable
        # JSON-RPC 2.0 tasks/send
        rpc_body = {
            "jsonrpc": "2.0",
            "id": request.grant.node_run_id,
            "method": "tasks/send",
            "params": {
                "id": request.grant.node_run_id,
                "message": {
                    "role": "user",
                    "parts": [
                        {
                            "type": "text",
                            "text": query,
                            "metadata": {"view": request.data_view.name},
                        }
                    ],
                },
            },
        }
        async with httpx.AsyncClient(timeout=request.timeout_ms / 1000.0) as client:
            r = await client.post(self._endpoint, json=rpc_body)
        if r.status_code != 200:
            raise RuntimeError(f"a2a agent returned {r.status_code}: {r.text[:200]}")
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"a2a agent error: {data['error']}")
        result = data.get("result", {})
        artefacts = result.get("artifacts", [])
        merged: dict[str, Any] = {}
        for a in artefacts:
            for part in a.get("parts", []):
                if part.get("type") == "data":
                    merged.update(part.get("data", {}))
        return AdapterResult(
            outputs={
                # The downstream Coordinator and merge code read from
                # ``research`` regardless of which Adapter was picked, so
                # the A2A Reference Agent exposes its artefact under
                # both ``research`` and ``a2a_artefact`` for symmetry.
                "research": {"a2a_artefact": merged, "task_state": result.get("status", {}).get("state")},
                "a2a_artefact": merged,
                "task_state": result.get("status", {}).get("state"),
            },
            raw=data,
            metadata={"endpoint": self._endpoint, "card": self._card_url},
        )
