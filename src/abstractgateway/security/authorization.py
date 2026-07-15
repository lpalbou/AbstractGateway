from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Optional

from .principal import GatewayPrincipal


@dataclass(frozen=True)
class GatewayAuthorizationDecision:
    allowed: bool
    reason: str = ""


@dataclass(frozen=True)
class GatewayRouteAuthorizationRequirement:
    resource: str
    action: str
    reason_code: str = "admin_required"
    required_role: str = "admin"
    admin_required: bool = True

    def public_dict(self) -> dict[str, str]:
        return {
            "resource": self.resource,
            "action": self.action,
            "reason_code": self.reason_code,
            "required_role": self.required_role,
        }


@dataclass(frozen=True)
class GatewayRoutePolicy:
    """Route-family authorization rule.

    The Gateway has many endpoint families. Keeping high-risk route checks in
    one table makes the admin/user split auditable and prevents each route from
    inventing a local `if admin` branch.
    """

    resource: str
    reason_code: str
    prefixes: tuple[str, ...] = ()
    exact: tuple[str, ...] = ()
    pattern: str = ""
    methods: tuple[str, ...] = ()
    admin_required: bool = True
    required_role: str = "admin"

    def matches(self, path: str, method: str) -> bool:
        p = str(path or "").rstrip("/") or "/"
        m = str(method or "GET").upper()
        if self.methods and m not in {str(x).upper() for x in self.methods}:
            return False
        if p in self.exact:
            return True
        for prefix in self.prefixes:
            prefix0 = prefix.rstrip("/") or "/"
            if p == prefix0 or p.startswith(prefix0 + "/"):
                return True
        if self.pattern and re.search(self.pattern, p):
            return True
        return False

    def requirement(self, method: str) -> GatewayRouteAuthorizationRequirement:
        action = "write" if str(method or "GET").upper() in {"POST", "PUT", "PATCH", "DELETE"} else "read"
        return GatewayRouteAuthorizationRequirement(
            resource=self.resource,
            action=action,
            reason_code=self.reason_code,
            required_role=self.required_role,
            admin_required=self.admin_required,
        )


GATEWAY_ROUTE_POLICIES: tuple[GatewayRoutePolicy, ...] = (
    GatewayRoutePolicy(resource="admin", reason_code="admin_required", prefixes=("/api/gateway/admin",)),
    GatewayRoutePolicy(resource="audit", reason_code="admin_required", prefixes=("/api/gateway/audit",)),
    GatewayRoutePolicy(resource="processes", reason_code="admin_required", prefixes=("/api/gateway/processes",)),
    GatewayRoutePolicy(resource="backlog", reason_code="admin_required", prefixes=("/api/gateway/backlog",)),
    GatewayRoutePolicy(resource="triage", reason_code="admin_required", prefixes=("/api/gateway/triage",)),
    GatewayRoutePolicy(resource="reports", reason_code="admin_required", prefixes=("/api/gateway/reports",)),
    # Bridges currently read process/global configuration and provider credentials.
    # Keep them operator-only until per-principal bridge config exists.
    GatewayRoutePolicy(resource="email", reason_code="admin_required", prefixes=("/api/gateway/email",)),
    # Host and model residency are operator surfaces. Discovery routes remain user-visible.
    GatewayRoutePolicy(resource="host", reason_code="admin_required", prefixes=("/api/gateway/host",)),
    GatewayRoutePolicy(
        resource="models",
        reason_code="admin_required",
        exact=("/api/gateway/models/loaded", "/api/gateway/models/load", "/api/gateway/models/unload"),
    ),
    # Server filesystem helpers are not an OS sandbox. Browser uploads remain available.
    GatewayRoutePolicy(resource="workspace", reason_code="admin_required", prefixes=("/api/gateway/files",)),
    GatewayRoutePolicy(resource="workspace", reason_code="admin_required", exact=("/api/gateway/artifacts/import", "/api/gateway/attachments/ingest")),
    GatewayRoutePolicy(
        resource="workspace",
        reason_code="admin_required",
        pattern=r"^/api/gateway/runs/[^/]+/artifacts/[^/]+/export$",
    ),
    # Core prompt-cache/bloc control planes affect process-local model state.
    GatewayRoutePolicy(
        resource="blocs",
        reason_code="admin_required",
        prefixes=("/api/gateway/blocs",),
        methods=("POST", "PUT", "PATCH", "DELETE"),
    ),
    GatewayRoutePolicy(
        resource="prompt_cache",
        reason_code="admin_required",
        prefixes=("/api/gateway/prompt_cache",),
        methods=("POST", "PUT", "PATCH", "DELETE"),
    ),
    # Entity MUTATION routes are operator surfaces (config-object plan, N1 —
    # signed 2026-07-11). GW-H makes entities non-admin principals; without
    # these rows any authenticated user could reembed or re-mind another
    # entity. Interaction surfaces (chat/visit/meet/summon) and every GET
    # stay user-level; the write-classed auth probe stays user-level by
    # design (it answers "would the doors accept me?" for ANY principal).
    #   - state:            sleep/wake/pause (operator lifecycle verbs)
    #   - reembed:          vector-index rebuild (critical mind operation)
    #   - tool-policy:      capability grants (the operator's word per phase)
    #   - prompt:           operator overlay on the composed head
    #   - substrate:        the mind substrate (provider/model)
    #   - loop/start|stop:  own-time lifecycle (start = spend; stop kept in
    #                       the same family — repeatedly stopping an entity's
    #                       own time is meddling with its life)
    #   - workspace/mounts: NOT in the signed N1 list, added by the same rule
    #                       that already admin-gates /api/gateway/files — a
    #                       mount whitelists a HOST directory into the
    #                       workspace read path (host-filesystem exposure).
    GatewayRoutePolicy(
        resource="entities",
        reason_code="admin_required",
        pattern=r"^/api/gateway/entities/[^/]+/(state|reembed|tool-policy|prompt|substrate|personal-grant|maintenance-window|loop/start|loop/stop|workspace/mounts)$",
        methods=("POST", "PUT", "PATCH", "DELETE"),
    ),
    # Spark TEMPLATE mutations (operator directive 2026-07-13: create/edit
    # versioned templates). A template seeds EVERY entity summoned from it,
    # so authoring it is an admin act (same class as re-minding one). GET
    # (view/list/versions) stays user-level. POST /entities/templates
    # (create) + PUT /entities/templates/{id} (edit) are the mutations; the
    # {id} form is the specific-template pattern, the bare create is exact.
    GatewayRoutePolicy(
        resource="entities",
        reason_code="admin_required",
        exact=("/api/gateway/entities/templates",),
        methods=("POST",),
    ),
    GatewayRoutePolicy(
        resource="entities",
        reason_code="admin_required",
        pattern=r"^/api/gateway/entities/templates/[^/]+$",
        methods=("POST", "PUT", "PATCH", "DELETE"),
    ),
)


class GatewayAuthorizationError(PermissionError):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


def _scope_allows(scopes: tuple[str, ...], *, resource: str, action: str) -> bool:
    resource0 = _norm(resource)
    action0 = _norm(action)
    candidates = {
        "*",
        resource0,
        f"{resource0}:*",
        f"{resource0}:{action0}",
        f"{resource0}.{action0}",
    }
    if action0:
        candidates.add(action0)
    return any(_norm(scope) in candidates for scope in scopes)


def authorize_gateway_principal(
    principal: Optional[GatewayPrincipal],
    *,
    resource: str,
    action: str,
    target_tenant_id: Optional[str] = None,
    admin_required: bool = False,
) -> GatewayAuthorizationDecision:
    if principal is None:
        return GatewayAuthorizationDecision(False, "Gateway principal unavailable")
    if principal.is_admin():
        return GatewayAuthorizationDecision(True)
    if target_tenant_id is not None and _norm(target_tenant_id) != _norm(principal.tenant_id):
        return GatewayAuthorizationDecision(False, "Cross-tenant access denied")
    if admin_required:
        return GatewayAuthorizationDecision(False, "Admin principal required")
    if _scope_allows(tuple(principal.scopes or ()), resource=resource, action=action):
        return GatewayAuthorizationDecision(True)
    return GatewayAuthorizationDecision(False, "Gateway action is not authorized")


def require_gateway_authorization(
    principal: Optional[GatewayPrincipal],
    *,
    resource: str,
    action: str,
    target_tenant_id: Optional[str] = None,
    admin_required: bool = False,
) -> GatewayPrincipal:
    decision = authorize_gateway_principal(
        principal,
        resource=resource,
        action=action,
        target_tenant_id=target_tenant_id,
        admin_required=admin_required,
    )
    if not decision.allowed:
        raise GatewayAuthorizationError(decision.reason or "Gateway action is not authorized")
    assert principal is not None
    return principal


def gateway_route_authorization_requirement(
    path: str,
    method: str,
) -> Optional[GatewayRouteAuthorizationRequirement]:
    for policy in GATEWAY_ROUTE_POLICIES:
        if policy.matches(path, method):
            return policy.requirement(method)
    return None
