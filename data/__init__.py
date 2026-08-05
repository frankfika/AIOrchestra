"""Sample contract corpus for the P0 demo.

All entries are synthetic. Per the P0 Gate, no real Restricted data is
allowed in any baseline.
"""
from data.samples.contracts import (
    SAMPLE_CONTRACTS,
    get_contract,
    list_contracts,
)

__all__ = ["SAMPLE_CONTRACTS", "get_contract", "list_contracts"]
