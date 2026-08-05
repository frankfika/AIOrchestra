"""LIT-001: fixed Contract Review Task Template.

P0 ships exactly one Task Template: ``contract-review-v1``. The plan
(§0.1.1 P0 row) fixes the topology: sequential nodes, limited fan-out
(one place where two adapters may serve the same node), one pre-approved
Fallback (per node), and one human approval point.

The DAG below is the *only* DAG the demo runs. Any new template would be
a new M0+ deliverable and an ADR.
"""
from orchestra.templates.contract_review import (
    CONTRACT_REVIEW_TEMPLATE,
    build_contract_review_plan,
    get_default_purpose,
)

__all__ = [
    "CONTRACT_REVIEW_TEMPLATE",
    "build_contract_review_plan",
    "get_default_purpose",
]
