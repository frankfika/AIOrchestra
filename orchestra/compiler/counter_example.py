"""M1 CMP-003 — Counter-example path generation.

When the Trust Compiler rejects a Plan, it must produce a
*counter-example*: the precise path the Compiler walked when it
discovered the violation, with the offending value at each step.

This module is the *renderer*; the checkers (CMP-002) populate
the :class:`CompileError.data_path` field, and this module
turns the path into a human-readable string for the audit
timeline and the UI.
"""
from __future__ import annotations

from orchestra.compiler.errors import CompileError


def render_counter_example(error: CompileError) -> str:
    """Render a :class:`CompileError` as a human-readable counter-example.

    The format is intentionally simple so the audit timeline can
    embed it in a single line:

        invariant 1: node=public_research, data path: ingest_contract -> extract_facts_local -> public_research
        reason: data classified restricted would flow to public-model capability public.openai-compat
    """
    if not error.data_path:
        return (
            f"invariant {error.invariant} ({error.kind.value}): "
            f"node={error.node_id}; {error.reason}"
        )
    path_str = " -> ".join(error.data_path)
    return (
        f"invariant {error.invariant} ({error.kind.value}): "
        f"path=[{path_str}], node={error.node_id}; {error.reason}"
    )
