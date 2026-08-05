"""M4 INT-AH-001 — AgenticHub Adapter package.

The :class:`AgenticHubTaskTool` mirrors the Dify Task Tool semantics
under AgenticHub's wire format. The three delegation modes share the
same :class:`orchestra.integrations.delegation.DelegationMode` enum
so a host platform cannot accidentally re-interpret the ownership
contract when swapping one adapter for the other.
"""
from orchestra.agentichub.client import AgenticHubResult, AgenticHubTaskTool

__all__ = ["AgenticHubResult", "AgenticHubTaskTool"]
