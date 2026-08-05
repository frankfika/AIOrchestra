"""Local contract fact extractor (real code, deterministic).

P0 doesn't ship a real LLM; instead, the "local model" is a deterministic
extractor that runs over the contract text and returns a structured Fact
Set. The point of P0 is to show that *this* node can run entirely on
premises with no egress, and that the next node only sees the projected
Fact Set, not the raw contract.

The extractor is a real HTTP service: the Adapter talks to it over
``POST /v1/extract``. See :func:`orchestra.adapters.servers.start_local_model_server`
for the server side.
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from orchestra.core.schema import DataView, NodeGrant, Purpose
from orchestra.adapters.base import AdapterRequest, AdapterResult


_FIELD_PATTERNS: dict[str, re.Pattern[str]] = {
    "vendor_name": re.compile(
        r"(?:供应商|Seller| Vendor| Supplier)[：:\s]*([^\n,，。；;]+)", re.IGNORECASE
    ),
    "buyer_name": re.compile(
        r"(?:采购方|Buyer| Purchaser| Customer)[：:\s]*([^\n,，。；;]+)", re.IGNORECASE
    ),
    "contract_amount": re.compile(
        r"(?:合同金额|总额|Total Amount| Contract Value)[：:\s]*"
        r"(?:RMB|CNY|USD|\$|¥|€)?\s*([0-9][0-9,.]*\s*(?:万|千|百|million|billion)?)",
        re.IGNORECASE,
    ),
    "payment_terms": re.compile(
        r"(?:付款条款|账期|Payment Terms| Net\s*\d+)[：:\s]*([^\n]{4,80})",
        re.IGNORECASE,
    ),
    "effective_date": re.compile(
        r"(?:生效日期|Effective Date|Start Date)[：:\s]*"
        r"(\d{4}[-/年.]\d{1,2}[-/月.]\d{1,2})",
        re.IGNORECASE,
    ),
    "expiration_date": re.compile(
        r"(?:到期日期|终止日期|Expiration|End Date)[：:\s]*"
        r"(\d{4}[-/年.]\d{1,2}[-/月.]\d{1,2})",
        re.IGNORECASE,
    ),
    "termination_clause": re.compile(
        r"(?:违约责任|终止条款|Termination)[：:\s]*"
        r"([^\n]{6,160})",
        re.IGNORECASE,
    ),
    "jurisdiction": re.compile(
        r"(?:管辖|适用法律|Governing Law|Jurisdiction)[：:\s]*([^\n,，。；;]+)",
        re.IGNORECASE,
    ),
}


def extract_facts_locally(contract_text: str) -> dict[str, Any]:
    """Run the deterministic extractor. Pure function, easy to test."""
    facts: dict[str, Any] = {}
    for name, pat in _FIELD_PATTERNS.items():
        m = pat.search(contract_text)
        if m:
            facts[name] = m.group(1).strip()
    facts["_length_chars"] = len(contract_text)
    facts["_word_count_zh"] = len(re.findall(r"[\u4e00-\u9fff]", contract_text))
    return facts


class LocalModelAdapter:
    """HTTP client to the local extractor service."""

    name = "local.contract-extractor"

    def __init__(self, endpoint: str = "http://127.0.0.1:8101/v1/extract") -> None:
        self._endpoint = endpoint

    async def invoke(self, request: AdapterRequest) -> AdapterResult:
        text = request.inputs.get("contract_text", "")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError(
                "LocalModelAdapter requires inputs['contract_text'] (non-empty str)"
            )
        async with httpx.AsyncClient(timeout=request.timeout_ms / 1000.0) as client:
            r = await client.post(
                self._endpoint,
                json={"text": text, "view": request.data_view.model_dump()},
            )
        if r.status_code != 200:
            raise RuntimeError(
                f"local extractor returned {r.status_code}: {r.text[:200]}"
            )
        data = r.json()
        # Server returns the *full* facts. The Adapter is responsible for
        # trimming to the data_view's allowlist.
        allowed = set(request.data_view.fields)
        if allowed:
            projected = {k: v for k, v in data["facts"].items() if k in allowed}
        else:
            projected = data["facts"]
        return AdapterResult(
            outputs={"facts": projected, "raw_fact_count": data["fact_count"]},
            raw=data,
            metadata={"endpoint": self._endpoint, "view": request.data_view.name},
        )
