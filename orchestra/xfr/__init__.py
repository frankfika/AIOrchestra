"""M3 XFR-001 — Schema Projection + Egress PEP.

A :class:`FieldProjector` applies a :class:`FieldManifest` to a
payload. The projection is *deterministic*: given the same
manifest + payload, the projected payload is byte-identical.

The Egress PEP (Policy Enforcement Point) wraps every
Adapter call to a public Adapter. It refuses to send any
field that is not in the manifest's ``allowed_fields``, refuses
to send a payload larger than ``byte_budget``, and records the
projected payload in the audit timeline as an
``io.sent`` event.

The dev plan §0.1.2 says the Egress PEP is "按版本化 Schema 进行
确定性字段投影并在出口执行策略；不承诺自由文本语义零泄漏". M3
ships the deterministic projection; the *semantic* guarantee
that the public Adapter does not infer sensitive content is
out of scope for the demo and is the responsibility of the
Adapter contract.
"""
from orchestra.xfr.egress_pep import EgressDenied, EgressPEP
from orchestra.xfr.projector import FieldProjector, ProjectionResult

__all__ = ["FieldProjector", "ProjectionResult", "EgressPEP", "EgressDenied"]
