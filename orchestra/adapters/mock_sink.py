"""Mock Procurement Sink — only writes to in-memory log served over HTTP.

Per the P0 Gate, human-approved writes must go *only* to this sink, never
to a real ERP / email / payment system. The sink stores nothing durable
on the agent side; the receipt is what proves the write.
"""
from __future__ import annotations

import httpx

from orchestra.adapters.base import AdapterRequest, AdapterResult


class MockSinkAdapter:
    name = "sink.mock-procurement"

    def __init__(self, endpoint: str = "http://127.0.0.1:8104/sink") -> None:
        self._endpoint = endpoint

    async def invoke(self, request: AdapterRequest) -> AdapterResult:
        body = {
            "grant_id": request.grant.grant_id,
            "purpose": request.purpose.code,
            "data": request.inputs,
            "decided_by": request.metadata.get("decided_by", "human"),
        }
        async with httpx.AsyncClient(timeout=request.timeout_ms / 1000.0) as client:
            r = await client.post(self._endpoint, json=body)
        if r.status_code != 200:
            raise RuntimeError(f"mock sink returned {r.status_code}: {r.text[:200]}")
        data = r.json()
        return AdapterResult(
            outputs={"written": True, "sink_record_id": data["record_id"]},
            raw=data,
            metadata={"endpoint": self._endpoint},
        )
