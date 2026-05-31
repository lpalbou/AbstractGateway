from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass
from typing import Any, Optional


_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_.@-]+")


@dataclass(frozen=True)
class GatewayPrincipal:
    """Authenticated Gateway principal used for request-scoped routing."""

    user_id: str
    tenant_id: str = "default"
    roles: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    token_fingerprint: str = ""
    runtime_id: str = ""
    source: str = "local"

    def is_admin(self) -> bool:
        return "admin" in set(self.roles)

    def public_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "roles": list(self.roles),
            "scopes": list(self.scopes),
            "token_fingerprint": self.token_fingerprint,
            "runtime_id": self.runtime_id or self.user_id,
            "source": self.source,
            "admin": self.is_admin(),
        }


_current_principal: contextvars.ContextVar[Optional[GatewayPrincipal]] = contextvars.ContextVar(
    "abstractgateway_current_principal",
    default=None,
)


def current_gateway_principal() -> Optional[GatewayPrincipal]:
    return _current_principal.get()


def set_current_gateway_principal(principal: Optional[GatewayPrincipal]) -> contextvars.Token:
    return _current_principal.set(principal)


def reset_current_gateway_principal(token: contextvars.Token) -> None:
    _current_principal.reset(token)


def safe_principal_component(value: Any, *, default: str = "default") -> str:
    fallback = "default" if default is None else str(default)
    raw = str(value or "").strip()
    if not raw:
        raw = fallback
    cleaned = _SAFE_COMPONENT_RE.sub("_", raw).strip("._")
    return cleaned or fallback


def local_admin_principal(*, token_fingerprint: str = "") -> GatewayPrincipal:
    return GatewayPrincipal(
        user_id="local-admin",
        tenant_id="local",
        roles=("admin", "user"),
        scopes=("*",),
        token_fingerprint=token_fingerprint,
        runtime_id="local-admin",
        source="legacy-token",
    )


def local_readonly_principal() -> GatewayPrincipal:
    """Loopback development read principal.

    This principal is intentionally not an admin. It exists only for the
    dev-read-no-auth escape hatch so read probes do not accidentally inherit
    operator privileges.
    """

    return GatewayPrincipal(
        user_id="loopback-readonly",
        tenant_id="local",
        roles=("readonly",),
        scopes=("gateway:read",),
        runtime_id="loopback-readonly",
        source="loopback-dev-read",
    )
