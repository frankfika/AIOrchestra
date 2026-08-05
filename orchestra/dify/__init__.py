"""LIT-005: Dify Task Tool reference entry.

The P0 Gate says: "Dify Task Tool 能提交任务并返回 Route/Audit 深链接".
This module is the *reference implementation* a Dify plugin author would
copy to expose Orchestra as a Dify Task Tool.

Wire shape (HTTP):

- ``POST {ORCHESTRA_BASE_URL}/tasks``  (forwarded; same body as the
  public API)
- ``GET  {ORCHESTRA_BASE_URL}/tasks/{task_run_id}/events``  (audit
  timeline; Dify can render this as a deep link)

We don't depend on the Dify SDK in P0 — Dify's Task Tool contract is a
plain HTTP call. The :class:`DifyTaskTool` class is a thin client that
the demo uses, and that a Dify plugin author would translate to a
``provider.py`` with the equivalent HTTP tool spec.
"""
from orchestra.dify.task_tool import DifyTaskTool, DifyTaskToolResult

__all__ = ["DifyTaskTool", "DifyTaskToolResult"]
