"""FastAPI surface for the P0 demo.

Endpoints:

- ``POST /tasks``                 submit a Contract Review
- ``GET  /tasks/{id}``            get status + last node outputs
- ``GET  /tasks/{id}/events``     audit timeline (P0 evidence)
- ``GET  /tasks/{id}/receipts``   signed receipts
- ``GET  /tasks/{id}/grants``     issued Node Grants
- ``POST /tasks/{id}/approve``    resolve the human approval
- ``POST /tasks/{id}/reject``     ditto
- ``GET  /capabilities``          static manifest snapshot
- ``GET  /templates``             the fixed Task Template
- ``GET  /healthz``               liveness
- ``POST /benchmark/run``         run the 3-baseline benchmark

The app is a *thin* layer over the Coordinator. The Coordinator is the
source of truth; the API just translates HTTP ↔ Python.
"""
from orchestra.api.app import create_app, AppState, run_server

__all__ = ["create_app", "AppState", "run_server"]
