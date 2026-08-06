"""LIT-003: three reference Adapters.

The Adapter interface is small. Each Adapter knows its own protocol
(OpenAI-compatible, A2A, in-process) but exposes the same shape to the
Coordinator. P0 has:

- :class:`LocalModelAdapter`: a real deterministic contract-fact extractor
  served over HTTP at ``http://127.0.0.1:8101/v1/extract``.
- :class:`OpenAICompatAdapter`: an HTTP client for any OpenAI-compatible
  Chat Completions endpoint (we use the in-repo ``openai-mock`` server at
  ``http://127.0.0.1:8102``).
- :class:`A2AReferenceAdapter`: an HTTP client for an A2A-style agent
  served at ``http://127.0.0.1:8103``.
- :class:`MockSinkAdapter`: writes to a local in-memory log served at
  ``http://127.0.0.1:8104``.

All four are real HTTP services — the demo spins them up with
``uvicorn`` and the Coordinator hits them with ``httpx``. The "model"
behind the Local adapter is a deterministic regex+heuristic extractor on
the contract text, not a real LLM, because we have no GPU in the demo
sandbox; the *adapter contract* is real, the *model* is a stand-in.
"""
from orchestra.adapters.a2a_reference import A2AReferenceAdapter
from orchestra.adapters.base import (
    Adapter,
    AdapterError as AdapterErrorBase,
    AdapterRequest,
    AdapterResult,
)
from orchestra.adapters.local_model import LocalModelAdapter
from orchestra.adapters.mock_sink import MockSinkAdapter
from orchestra.adapters.openai_compat import OpenAICompatAdapter
from orchestra.adapters.servers import (
    start_a2a_reference_server,
    start_local_model_server,
    start_mock_sink_server,
    start_openai_mock_server,
)

__all__ = [
    "Adapter",
    "AdapterRequest",
    "AdapterResult",
    "AdapterErrorBase",
    "LocalModelAdapter",
    "OpenAICompatAdapter",
    "A2AReferenceAdapter",
    "MockSinkAdapter",
    "start_local_model_server",
    "start_openai_mock_server",
    start_a2a_reference_server.__name__,
    "start_mock_sink_server",
]
