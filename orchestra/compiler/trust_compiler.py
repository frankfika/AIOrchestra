"""M1 CMP-001/002/003 — Trust Compiler (orchestrator).

The :class:`TrustCompiler` runs the four phases of compilation:

  1. :class:`Parser` — TaskTemplate + Contract → CandidateGraph
  2. :class:`Normalizer` — CandidateGraph → NormalizedGraph
  3. :class:`TypeChecker` — NormalizedGraph → list[CompileError]
  4. :class:`InformationFlowChecker` — NormalizedGraph + bindings → errors
  5. :class:`EffectChecker` — NormalizedGraph + bindings → errors
  6. :class:`DelegationChecker` — NormalizedGraph + bindings → errors

The Compiler returns a :class:`CompileResult`. If any phase
produces errors, the result is ``ok=False`` and the
``errors`` list carries the failing invariant + a
counter-example path (CMP-003).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orchestra.compiler.delegation_checker import DelegationChecker
from orchestra.compiler.errors import CompileError
from orchestra.compiler.effect_checker import EffectChecker
from orchestra.compiler.info_flow import InformationFlowChecker
from orchestra.compiler.normalizer import Normalizer
from orchestra.compiler.parser import Parser
from orchestra.compiler.type_checker import TypeChecker
from orchestra.core.schema import (
    CapabilityManifest,
    SecurityLabel,
    TaskContract,
    TaskTemplate,
)


@dataclass
class CompileResult:
    ok: bool
    errors: list[CompileError] = field(default_factory=list)
    graph: Any = None  # NormalizedGraph when ok; None otherwise

    def first_error(self) -> CompileError | None:
        return self.errors[0] if self.errors else None


class TrustCompiler:
    def __init__(
        self,
        manifests_by_id: dict[str, CapabilityManifest],
    ) -> None:
        self._manifests = manifests_by_id
        self._parser = Parser()
        self._normalizer = Normalizer()
        self._type_checker = TypeChecker()
        self._info_flow = InformationFlowChecker(manifests_by_id)
        self._effect = EffectChecker(manifests_by_id)
        self._delegation = DelegationChecker(manifests_by_id)

    def compile(
        self,
        template: TaskTemplate,
        contract: TaskContract,
        initial_label: SecurityLabel,
        node_capability_bindings: dict[str, str],
    ) -> CompileResult:
        all_errors: list[CompileError] = []
        try:
            candidate = self._parser.parse(template, contract)
        except Exception as e:  # noqa: BLE001
            return CompileResult(ok=False, errors=[
                CompileError(
                    kind=CompileErrorKind(kind="unknown-node"),
                    node_id="*",
                    reason=f"parse failed: {type(e).__name__}: {e}",
                )
            ])
        graph = self._normalizer.normalize(candidate)
        # Phase 3: type checker
        all_errors += self._type_checker.check(graph)
        # Phase 4: information flow
        all_errors += self._info_flow.check(graph, initial_label, node_capability_bindings)
        # Phase 5: effect checker
        all_errors += self._effect.check(graph, node_capability_bindings)
        # Phase 6: delegation checker
        all_errors += self._delegation.check(
            graph, contract.purpose.code, node_capability_bindings
        )
        if all_errors:
            return CompileResult(ok=False, errors=all_errors)
        return CompileResult(ok=True, graph=graph, errors=[])
