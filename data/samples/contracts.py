"""Synthetic contract corpus.

The shapes are deliberately rich enough to exercise the extractor's
regexes (vendor name, buyer, amount, payment terms, dates, jurisdiction,
termination clause) without leaking anything real.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SampleContract:
    contract_id: str
    vendor_id: str
    title: str
    body: str


SAMPLE_CONTRACTS: list[SampleContract] = [
    SampleContract(
        contract_id="ctr-001",
        vendor_id="demo-vendor-001",
        title="Cloud logistics SaaS subscription — Acme",
        body=(
            "供应商：Acme Cloud Logistics Co., Ltd.\n"
            "采购方：Helios Procurement Group\n"
            "合同金额：RMB 8,600,000.00\n"
            "付款条款：Net 30，发票入账后 30 日内电汇至指定账户。\n"
            "生效日期：2026-01-15\n"
            "到期日期：2027-01-14\n"
            "管辖：适用法律为香港特别行政区法律，争议提交香港国际仲裁中心。\n"
            "终止条款：任一方可在对方发生重大违约且经书面通知 30 日内未补正时终止合同。\n"
        ),
    ),
    SampleContract(
        contract_id="ctr-002",
        vendor_id="demo-vendor-002",
        title="Industrial parts supply — Helios",
        body=(
            "Supplier: Helios Industrial Group\n"
            "Buyer: Stellar Manufacturing Ltd.\n"
            "Total Amount: USD 4,200,000\n"
            "Payment Terms: Net 45, irrevocable L/C at sight.\n"
            "Effective Date: 2026-04-01\n"
            "Expiration Date: 2028-03-31\n"
            "Governing Law: Singapore. Disputes to SIAC.\n"
            "Termination: Either party may terminate for material breach with 60 days written cure.\n"
        ),
    ),
    SampleContract(
        contract_id="ctr-003",
        vendor_id="demo-vendor-001",
        title="Cloud logistics pilot (small) — Acme",
        body=(
            "供应商：Acme Cloud Logistics Co., Ltd.\n"
            "采购方：内部创新业务部\n"
            "合同金额：RMB 380,000\n"
            "付款条款：Net 15。\n"
            "生效日期：2026-06-01\n"
            "到期日期：2026-12-31\n"
            "适用法律：中国大陆法律。\n"
            "终止条款：任何一方可提前 14 日书面通知终止。\n"
        ),
    ),
]


def get_contract(contract_id: str) -> SampleContract:
    for c in SAMPLE_CONTRACTS:
        if c.contract_id == contract_id:
            return c
    raise KeyError(f"unknown contract_id {contract_id!r}")


def list_contracts() -> list[dict[str, str]]:
    return [
        {"contract_id": c.contract_id, "vendor_id": c.vendor_id, "title": c.title}
        for c in SAMPLE_CONTRACTS
    ]
