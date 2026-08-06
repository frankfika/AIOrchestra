"""M1 — Trust Compiler (CMP-001/002/003), Resolver (RSL-001),
Binding Closure + Plan Signer (BND-001).

The Trust Compiler takes a :class:`TaskTemplate` + an
:class:`InitialInputs` and produces either:
  - a signed :class:`ExecutionPlan` (happy path), or
  - a structured :class:`CompileError` carrying the failing
    invariant, the offending node id, and a human-readable
    counter-example path.

The Compiler is the **single point that enforces invariants #1
(Restricted never reaches a public sink), #3 (capability must be
in eligible set), #5 (sub-agent authority narrowing), #6 (dynamic
nodes re-compile), #7 (high-risk effects need approval), #15
(multi-dimensional labels), #16 (Restricted model output
inherits), and #20 (delegation as parent intersection)** at
plan time. P0 enforces them at *route* time (after the Plan is
already bound); M1 enforces them at *compile* time, before any
Capability is bound, so the rejection is cheaper and the audit
trail is clean.
"""
from orchestra.compiler.binding_closure import BindingClosureChecker, ClosureResult
from orchestra.compiler.counter_example import render_counter_example
from orchestra.compiler.delegation_checker import DelegationChecker
from orchestra.compiler.effect_checker import EffectChecker
from orchestra.compiler.errors import CompileError, CompileErrorKind
from orchestra.compiler.info_flow import InformationFlowChecker
from orchestra.compiler.normalizer import Normalizer
from orchestra.compiler.parser import Parser
from orchestra.compiler.plan_signer import PlanSigner
from orchestra.compiler.resolver import Resolver, ResolverResult
from orchestra.compiler.trust_compiler import CompileResult, TrustCompiler
from orchestra.compiler.type_checker import TypeChecker

__all__ = [
    "CompileError",
    "CompileErrorKind",
    "Parser",
    "Normalizer",
    "TypeChecker",
    "InformationFlowChecker",
    "EffectChecker",
    "DelegationChecker",
    "render_counter_example",
    "TrustCompiler",
    "CompileResult",
    "Resolver",
    "ResolverResult",
    "BindingClosureChecker",
    "ClosureResult",
    "PlanSigner",
]
