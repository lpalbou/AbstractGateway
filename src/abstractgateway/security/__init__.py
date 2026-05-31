from .authorization import GatewayAuthorizationError, require_gateway_authorization
from .principal import GatewayPrincipal, current_gateway_principal, local_admin_principal

_GATEWAY_SECURITY_EXPORTS = {
    "GatewayAuthPolicy",
    "GatewaySecurityMiddleware",
    "load_gateway_auth_policy_from_env",
}
_SESSION_EXPORTS = {
    "GatewaySessionStore",
    "gateway_csrf_cookie_name",
    "gateway_csrf_header_name",
    "gateway_session_cookie_name",
    "gateway_session_header_name",
}

__all__ = [
    "GatewayAuthorizationError",
    "GatewayAuthPolicy",
    "GatewayPrincipal",
    "GatewaySecurityMiddleware",
    "GatewaySessionStore",
    "gateway_csrf_cookie_name",
    "gateway_csrf_header_name",
    "current_gateway_principal",
    "gateway_session_cookie_name",
    "gateway_session_header_name",
    "load_gateway_auth_policy_from_env",
    "local_admin_principal",
    "require_gateway_authorization",
]


def __getattr__(name: str):
    if name in _GATEWAY_SECURITY_EXPORTS:
        from . import gateway_security

        value = getattr(gateway_security, name)
        globals()[name] = value
        return value
    if name in _SESSION_EXPORTS:
        from . import sessions

        value = getattr(sessions, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
