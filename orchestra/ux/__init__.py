"""M3 UX-001 / UX-002 — Demo Console.

A real-backend-driven HTML console that surfaces three role views of a
running Task:

  * **Business**: submit a contract review; see status + result.
  * **Platform**: Route Preview (which capabilities the Router picked +
    the binding rationale) and Permission View (Node Grants + Audience).
  * **Security / Audit**: Audit Timeline (every AuditEvent with
    projected-digest visibility on egress) + Receipt list.

The console is intentionally minimal — plain HTML + a few CSS rules
plus f-string templates. The goal is to prove the API surface is
honest, not to ship a polished SPA.
"""
from orchestra.ux.router import build_ux_router

__all__ = ["build_ux_router"]
