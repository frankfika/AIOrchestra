"""OpenAI-compatible public-model Adapter (real HTTP protocol).

P0 ships an in-repo mock that speaks the OpenAI Chat Completions wire
format. The Adapter here is the *real* OpenAI client shape: it posts a
``{"model", "messages", ...}`` body to ``/v1/chat/completions`` and
expects an OpenAI-shaped response. Swapping the in-repo mock for a real
public OpenAI-compatible endpoint is a single string change.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from orchestra.adapters.base import AdapterRequest, AdapterResult


class OpenAICompatAdapter:
    name = "public.openai-compat"

    def __init__(self, endpoint: str = "http://127.0.0.1:8102/v1/chat/completions") -> None:
        self._endpoint = endpoint
        self._model = "demo-public-model"

    async def invoke(self, request: AdapterRequest) -> AdapterResult:
        facts = request.inputs.get("facts", {})
        query = request.inputs.get("query", "")
        # Build a strict, schema-bounded message: the public model must
        # only see the projected Fact Set, never the raw contract. This
        # is the demo's stand-in for "Schema Projection + Egress PEP".
        if not isinstance(facts, dict):
            raise RuntimeError("OpenAICompatAdapter requires inputs['facts'] (dict)")
        system = (
            "You are a public research assistant. You may only answer using "
            "the facts provided. Do not invent details. Respond in JSON "
            "matching the requested schema."
        )
        user_payload = {
            "task": request.purpose.code,
            "query": query,
            "facts": facts,
            "view": request.data_view.name,
        }
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            "temperature": 0.0,
            "max_tokens": 256,
            "response_format": {"type": "json_object"},
        }
        async with httpx.AsyncClient(timeout=request.timeout_ms / 1000.0) as client:
            r = await client.post(self._endpoint, json=body)
        if r.status_code != 200:
            raise RuntimeError(
                f"openai-compat returned {r.status_code}: {r.text[:200]}"
            )
        data = r.json()
        try:
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content) if isinstance(content, str) else content
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise RuntimeError(f"openai-compat response shape invalid: {e}")
        return AdapterResult(
            outputs={"research": parsed},
            raw=data,
            metadata={
                "endpoint": self._endpoint,
                "model": self._model,
                "usage": data.get("usage", {}),
            },
        )
