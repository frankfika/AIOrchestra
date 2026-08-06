"""HTTP servers backing the four P0 Adapters.

These are the *real* server-side implementations. The Adapter code in
:mod:`orchestra.adapters` is the *client*; here we provide the *service*.
The Contract Review demo boots all four with :func:`start_all_servers`.

The servers do not share state with the Coordinator: each one is its own
process-shaped FastAPI app. The deterministic local extractor has zero
external state; the openai-mock has a small in-memory catalogue of
synthetic public registries; the A2A agent has a fixed set of approved
public sources; the mock sink keeps an in-memory log keyed by record id.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import uuid
from typing import Any

import uvicorn
from fastapi import Body, FastAPI, HTTPException
from pydantic import BaseModel

from orchestra.adapters.local_model import extract_facts_locally

# ---------------------------------------------------------------------------
# Local Model server (deterministic extractor)
# ---------------------------------------------------------------------------


class ExtractRequest(BaseModel):
    text: str
    view: dict[str, Any] | None = None


class ChatReq(BaseModel):
    model: str
    messages: list[dict[str, Any]]
    temperature: float = 0.0
    max_tokens: int = 256
    response_format: dict[str, Any] | None = None


class SinkReq(BaseModel):
    grant_id: str
    purpose: str
    data: dict[str, Any]
    decided_by: str = "human"


def _build_local_model_app() -> FastAPI:
    app = FastAPI(title="Orchestra Local Model (deterministic extractor)")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/extract")
    def extract(body: ExtractRequest = Body(...)) -> dict[str, Any]:
        facts = extract_facts_locally(body.text)
        return {
            "model": "deterministic-extractor-v1",
            "fact_count": len(facts),
            "facts": facts,
        }

    return app


# ---------------------------------------------------------------------------
# OpenAI-compatible mock server
# ---------------------------------------------------------------------------

_PUBLIC_REGISTRIES = {
    "demo-vendor-001": {
        "vendor_name": "Acme Cloud Logistics Co., Ltd.",
        "jurisdiction": "Hong Kong SAR",
        "incorporation_year": 2014,
        "public_listing": False,
        "regulatory_actions": [],
        "source": "synthetic public registry (P0 demo)",
    },
    "demo-vendor-002": {
        "vendor_name": "Helios Industrial Group",
        "jurisdiction": "Singapore",
        "incorporation_year": 2008,
        "public_listing": True,
        "regulatory_actions": [
            {"year": 2021, "type": "minor filing delay", "severity": "low"}
        ],
        "source": "synthetic public registry (P0 demo)",
    },
}


def _build_openai_app() -> FastAPI:
    app = FastAPI(title="Orchestra OpenAI-compatible mock")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    def chat(body: ChatReq = Body(...)) -> dict[str, Any]:
        # The mock is intentionally dumb: it picks the vendor id from the
        # user message and returns a structured public-research result.
        user_msg = next(
            (m for m in reversed(body.messages) if m.get("role") == "user"), None
        )
        if user_msg is None:
            raise HTTPException(400, "no user message")
        try:
            payload = json.loads(user_msg["content"])
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"user content not JSON: {e}")
        facts = payload.get("facts", {})
        query = payload.get("query", "")
        vendor_id = facts.get("vendor_id") or "demo-vendor-001"
        registry = _PUBLIC_REGISTRIES.get(vendor_id, _PUBLIC_REGISTRIES["demo-vendor-001"])
        answer = {
            "vendor_id": vendor_id,
            "public_summary": registry,
            "query_addressed": query,
            "sources": [
                {
                    "type": "synthetic-public-registry",
                    "url": f"https://demo.invalid/registries/{vendor_id}",
                    "version": "2026-08-01",
                }
            ],
            "caveat": "All entries are synthetic for the P0 demo. Do not use in production.",
        }
        return {
            "id": "chatcmpl-" + uuid.uuid4().hex[:10],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(answer, ensure_ascii=False)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": sum(len(m.get("content", "")) for m in body.messages) // 4,
                "completion_tokens": 120,
                "total_tokens": 0,
            },
        }

    return app


# ---------------------------------------------------------------------------
# A2A Reference server
# ---------------------------------------------------------------------------

_A2A_KNOWN_INDUSTRY_QUERIES = {
    "industry classification": {
        "industry": "Cloud logistics & supply chain SaaS",
        "naics": "541512",
        "size_band": "SMB-to-mid-market",
    },
    "market size": {
        "global_tam_usd_2025": 18_400_000_000,
        "cagr_2025_2030": 0.114,
        "source": "synthetic industry report (P0 demo)",
    },
}


def _build_a2a_app() -> FastAPI:
    app = FastAPI(title="Orchestra A2A Reference Agent")

    @app.get("/.well-known/agent.json")
    def card() -> dict[str, Any]:
        return {
            "name": "Orchestra A2A Reference Agent",
            "version": "0.1.0",
            "url": "http://127.0.0.1:8103/a2a/v1",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "skills": [
                {"id": "industry-research", "name": "Industry Research"},
            ],
            "defaultInputModes": ["text"],
            "defaultOutputModes": ["data"],
        }

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/a2a/v1")
    def rpc(payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("jsonrpc") != "2.0":
            return {
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "error": {"code": -32600, "message": "invalid jsonrpc"},
            }
        method = payload.get("method")
        if method != "tasks/send":
            return {
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "error": {"code": -32601, "message": f"unknown method {method!r}"},
            }
        params = payload.get("params", {})
        text = ""
        for part in params.get("message", {}).get("parts", []):
            if part.get("type") == "text":
                text = part.get("text", "")
                break
        artefact_data: dict[str, Any] = {}
        for key, val in _A2A_KNOWN_INDUSTRY_QUERIES.items():
            if key in text.lower():
                artefact_data[key] = val
        if not artefact_data:
            artefact_data = {"note": "no industry-research match", "echo": text[:200]}
        return {
            "jsonrpc": "2.0",
            "id": payload.get("id"),
            "result": {
                "id": params.get("id"),
                "status": {"state": "completed"},
                "artifacts": [
                    {
                        "parts": [{"type": "data", "data": artefact_data}],
                    }
                ],
            },
        }

    return app


# ---------------------------------------------------------------------------
# Mock Procurement Sink
# ---------------------------------------------------------------------------

_SINK_LOG: list[dict[str, Any]] = []


def _build_sink_app() -> FastAPI:
    app = FastAPI(title="Orchestra Mock Procurement Sink")

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "size": len(_SINK_LOG)}

    @app.get("/sink/log")
    def log() -> dict[str, Any]:
        return {"count": len(_SINK_LOG), "items": _SINK_LOG}

    @app.post("/sink")
    def sink(body: SinkReq = Body(...)) -> dict[str, Any]:
        record_id = "rec-" + uuid.uuid4().hex[:10]
        _SINK_LOG.append(
            {
                "record_id": record_id,
                "grant_id": body.grant_id,
                "purpose": body.purpose,
                "data": body.data,
                "decided_by": body.decided_by,
                "ts": time.time(),
            }
        )
        return {"record_id": record_id, "stored": True}

    return app


# ---------------------------------------------------------------------------
# Server start helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_app(app: FastAPI, host: str, port: int) -> uvicorn.Server:
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server


def _wait_for_health(url: str, timeout_s: float = 10.0) -> None:
    import httpx

    deadline = time.time() + timeout_s
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=1.0)
            if r.status_code == 200:
                return
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(0.1)
    raise RuntimeError(f"server at {url} not ready: {last_err}")


def start_local_model_server(host: str = "127.0.0.1", port: int | None = None) -> dict[str, Any]:
    port = port or _free_port()
    server = _start_app(_build_local_model_app(), host, port)
    _wait_for_health(f"http://{host}:{port}/healthz")
    return {"host": host, "port": port, "endpoint": f"http://{host}:{port}/v1/extract", "server": server}


def start_openai_mock_server(host: str = "127.0.0.1", port: int | None = None) -> dict[str, Any]:
    port = port or _free_port()
    server = _start_app(_build_openai_app(), host, port)
    _wait_for_health(f"http://{host}:{port}/healthz")
    return {
        "host": host,
        "port": port,
        "endpoint": f"http://{host}:{port}/v1/chat/completions",
        "server": server,
    }


def start_a2a_reference_server(host: str = "127.0.0.1", port: int | None = None) -> dict[str, Any]:
    port = port or _free_port()
    server = _start_app(_build_a2a_app(), host, port)
    _wait_for_health(f"http://{host}:{port}/healthz")
    return {
        "host": host,
        "port": port,
        "endpoint": f"http://{host}:{port}/a2a/v1",
        "card": f"http://{host}:{port}/.well-known/agent.json",
        "server": server,
    }


def start_mock_sink_server(host: str = "127.0.0.1", port: int | None = None) -> dict[str, Any]:
    port = port or _free_port()
    server = _start_app(_build_sink_app(), host, port)
    _wait_for_health(f"http://{host}:{port}/healthz")
    return {
        "host": host,
        "port": port,
        "endpoint": f"http://{host}:{port}/sink",
        "log": f"http://{host}:{port}/sink/log",
        "server": server,
    }


def start_all_servers() -> dict[str, Any]:
    """Start the four reference servers on free ports; return a dict of
    endpoints keyed by capability name.

    Each entry contains a running uvicorn server, a host, a port, and the
    canonical endpoint URL. The Coordinator uses these to talk to the
    adapters. Pass the returned dict into
    :func:`orchestra.registry.bootstrap.load_default_manifests_with_endpoints`
    to re-pin the manifest endpoints to the actual ports.
    """
    local = start_local_model_server()
    oa = start_openai_mock_server()
    a2a = start_a2a_reference_server()
    sink = start_mock_sink_server()
    return {
        "local.contract-extractor": local,
        "public.openai-compat": oa,
        "a2a.reference-agent": a2a,
        "sink.mock-procurement": sink,
    }
