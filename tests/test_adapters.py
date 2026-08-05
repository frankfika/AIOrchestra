"""Adapter server tests — start the four servers in-process and call
them over real HTTP."""
from __future__ import annotations

import time
import uuid

import httpx
import pytest

from orchestra.adapters.servers import (
    start_local_model_server,
    start_openai_mock_server,
    start_a2a_reference_server,
    start_mock_sink_server,
)


@pytest.fixture(scope="module")
def local_server():
    info = start_local_model_server()
    yield info


@pytest.fixture(scope="module")
def openai_server():
    info = start_openai_mock_server()
    yield info


@pytest.fixture(scope="module")
def a2a_server():
    info = start_a2a_reference_server()
    yield info


@pytest.fixture(scope="module")
def sink_server():
    info = start_mock_sink_server()
    yield info


def test_local_extractor_returns_facts(local_server):
    print(f"DEBUG local_server endpoint={local_server['endpoint']}")
    r = httpx.post(
        local_server["endpoint"],
        json={"text": "供应商：Acme\n采购方：Beta\n合同金额：RMB 1,000,000\n付款条款：Net 30\n生效日期：2026-01-01\n到期日期：2026-12-31\n管辖：中国大陆"},
        timeout=5.0,
    )
    if r.status_code != 200:
        raise AssertionError(f"status={r.status_code}, body={r.text[:500]}")
    data = r.json()
    assert data["fact_count"] >= 6
    assert data["facts"]["vendor_name"] == "Acme"


def test_openai_compat_returns_structured_json(openai_server):
    body = {
        "model": "demo",
        "messages": [
            {"role": "system", "content": "you are a public research assistant"},
            {
                "role": "user",
                "content": '{"task":"contract-review","query":"industry classification","facts":{"vendor_id":"demo-vendor-001"}}',
            },
        ],
        "response_format": {"type": "json_object"},
    }
    r = httpx.post(openai_server["endpoint"], json=body, timeout=5.0)
    assert r.status_code == 200
    data = r.json()
    assert "choices" in data
    import json as _json

    parsed = _json.loads(data["choices"][0]["message"]["content"])
    assert parsed["vendor_id"] == "demo-vendor-001"
    assert "public_summary" in parsed


def test_a2a_agent_card_and_rpc(a2a_server):
    card = httpx.get(a2a_server["card"], timeout=5.0)
    assert card.status_code == 200
    assert card.json()["name"] == "Orchestra A2A Reference Agent"
    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "tasks/send",
        "params": {
            "id": "1",
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": "what is the industry classification?"}],
            },
        },
    }
    r = httpx.post(a2a_server["endpoint"], json=payload, timeout=5.0)
    assert r.status_code == 200
    data = r.json()
    assert "result" in data
    assert data["result"]["status"]["state"] == "completed"
    assert "industry classification" in str(data["result"])


def test_mock_sink_records_writes(sink_server):
    body = {"grant_id": "g1", "purpose": "contract-review", "data": {"a": 1}, "decided_by": "tester"}
    r = httpx.post(sink_server["endpoint"], json=body, timeout=5.0)
    assert r.status_code == 200
    assert "record_id" in r.json()
    log = httpx.get(sink_server["log"], timeout=5.0).json()
    assert log["count"] >= 1
