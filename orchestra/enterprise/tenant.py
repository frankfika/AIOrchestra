"""M6 ENT-001 — Tenant context + RBAC.

A :class:`TenantContext` is the *active tenant* for the current
operation. Every read and write through the multi-tenant
:class:`orchestra.enterprise.isolation.IsolatingEventStore` is
filtered by the active tenant; cross-tenant access is refused at
the storage layer, not at the API layer.

Roles are simple for M6: ``admin``, ``operator``, ``auditor``,
``developer``. RBAC is checked at the call site (e.g. the API
endpoint), not at the storage layer — the storage layer only
enforces tenant isolation.
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from orchestra.core.errors import OrchestraError


class TenantRole(str, Enum):
    ADMIN = "admin"            # full read/write on this tenant
    OPERATOR = "operator"      # can drive tasks but not change manifests
    AUDITOR = "auditor"        # read-only access to events + receipts
    DEVELOPER = "developer"    # can submit tasks; cannot approve


class TenantAccessDenied(OrchestraError):
    """The active tenant is not allowed to perform the operation."""


@dataclass
class Tenant:
    tenant_id: str
    name: str
    plan: str = "default"
    # Free-form quotas. M6 does not enforce; the production swap
    # plugs in a real quota manager.
    quotas: dict[str, Any] = field(default_factory=dict)


@dataclass
class TenantContext:
    """The active tenant + caller identity for a request.

    One ``TenantContext`` is created per request and threaded
    through the storage / API / Coordinator layers via the
    ``current_tenant`` :mod:`contextvars` slot. The context is
    also responsible for the caller's role so RBAC checks can run
    at the call site.
    """

    tenant: Tenant
    caller_id: str
    role: TenantRole

    def require_role(self, *allowed: TenantRole) -> None:
        if self.role not in allowed:
            raise TenantAccessDenied(
                f"role {self.role.value!r} not in {sorted(r.value for r in allowed)}"
            )


# The active tenant for the current asyncio task. Use
# ``current_tenant.set(...)`` in API middleware; the storage layer
# reads it via ``current_tenant.get()``.
current_tenant: contextvars.ContextVar[TenantContext | None] = contextvars.ContextVar(
    "orchestra_tenant", default=None,
)


def set_active(ctx: TenantContext) -> contextvars.Token:
    return current_tenant.set(ctx)


def get_active() -> TenantContext:
    ctx = current_tenant.get()
    if ctx is None:
        raise TenantAccessDenied(
            "no active tenant; call set_active(TenantContext(...)) first"
        )
    return ctx


def reset_active(token: contextvars.Token) -> None:
    current_tenant.reset(token)
