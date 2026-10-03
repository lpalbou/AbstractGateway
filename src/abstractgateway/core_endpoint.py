"""Gateway-owned exposure of Core's serving routes on the Gateway listener.

Configuration and credentials belong to this host; inference and provider
credential policy belong to Core (framework ADR-0033, Core ADR-0004).
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field, replace
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import tempfile

from starlette.requests import Request
from starlette.responses import JSONResponse

from abstractruntime.integrations.abstractcore.server_facade import serve_core_request

from . import network_exposure as ne
from .runtime_config import resolve_network_setting, store_lock
from .users import gateway_data_dir_from_env


@dataclass(frozen=True)
class EndpointSettings:
    enabled: bool = False
    access: str = "token"
    token: str = field(default="", repr=False)


def read_settings(data_dir: Path) -> EndpointSettings:
    path = Path(data_dir) / "config" / "core_endpoint.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return EndpointSettings()
    if (not isinstance(raw, dict) or type(raw.get("enabled")) is not bool
            or not isinstance(raw.get("access"), str)
            or raw.get("access") not in {"token", "open"}
            or not isinstance(raw.get("token"), str)
            or (raw["enabled"] and not raw["token"].strip())):
        raise ValueError("Invalid Core endpoint settings; restore config/core_endpoint.json")
    return EndpointSettings(raw["enabled"], raw["access"], raw["token"])


def change_settings(data_dir: Path, *, enabled=None, access=None, rotate=False) -> EndpointSettings:
    with store_lock(data_dir):
        current = read_settings(data_dir)
        updated = replace(current, enabled=current.enabled if enabled is None else enabled,
                          access=current.access if access is None else access)
        if access == "open" and resolve_network_setting(data_dir)["mode"] == "internet":
            raise ValueError("Internet mode requires a token. Choose token access or change Network to Local network.")
        if rotate or (updated.enabled and not updated.token):
            updated = replace(updated, token="ac_" + secrets.token_urlsafe(32))
        path = Path(data_dir) / "config" / "core_endpoint.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".core_endpoint-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(asdict(updated), stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return updated


_LOCAL_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16", "100.64.0.0/10", "::1/128", "fc00::/7", "fe80::/10",
))


def open_access_allowed(scope: dict, data_dir: Path) -> bool:
    # Open applies only to direct local/LAN/VPN peers. A reverse proxy can
    # hide a public caller behind a loopback socket, so it always needs a key.
    if resolve_network_setting(data_dir)["mode"] == "internet" or ne.live_reverse_proxy(data_dir).trust_proxy:
        return False
    effective = ne.effective_bind(data_dir)
    if effective.get("mode") == "internet":
        return False
    host = effective.get("bind_host")
    if host and host not in {"0.0.0.0", "::", "localhost"}:
        try:
            if not any(ipaddress.ip_address(host) in network for network in _LOCAL_NETWORKS):
                return False
        except ValueError:
            return False
    if any(k.lower() in {b"forwarded", b"x-forwarded-for", b"x-forwarded-host", b"x-forwarded-proto"}
           for k, _ in scope.get("headers", [])):
        return False
    try:
        peer = ipaddress.ip_address((scope.get("client") or ("", 0))[0])
        if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped:
            peer = peer.ipv4_mapped
        return any(peer in network for network in _LOCAL_NETWORKS)
    except (KeyError, ValueError, TypeError):
        return False


SERVING_ROUTES = {
    "/v1/models": "GET",
    "/v1/chat/completions": "POST",
    "/v1/responses": "POST",
    "/v1/embeddings": "POST",
    "/v1/audio/speech": "POST",
    "/v1/audio/transcriptions": "POST",
    "/v1/audio/translations": "POST",
    "/v1/images/generations": "POST",
    "/v1/images/edits": "POST",
    "/v1/images/variations": "POST",
}


class CoreEndpoint:
    """Direct ASGI delegation preserves streaming and cancellation."""

    async def __call__(self, scope, receive, send):
        path = str(scope.get("path", ""))
        root = str(scope.get("root_path", ""))
        if root and path.startswith(root):
            path = path[len(root):]
        if SERVING_ROUTES.get(path) != scope.get("method"):
            return await JSONResponse({"detail": "Not found"}, status_code=404)(scope, receive, send)
        data_dir = gateway_data_dir_from_env()
        try:
            settings = await asyncio.to_thread(read_settings, data_dir)
            if not settings.enabled:
                return await JSONResponse({"detail": "Core endpoint is disabled"}, status_code=404)(scope, receive, send)
            allow_open = settings.access == "open" and await asyncio.to_thread(open_access_allowed, scope, data_dir)
        except (OSError, ValueError):
            return await JSONResponse({"detail": "Core endpoint settings are unavailable"}, status_code=503)(scope, receive, send)
        authorization = Request(scope).headers.get("authorization")
        scheme, _, credential = (authorization or "").partition(" ")
        valid_token = scheme.lower() == "bearer" and hmac.compare_digest(credential.encode(), settings.token.encode())
        if (authorization is not None and not valid_token) or (authorization is None and not allow_open):
            return await JSONResponse({"detail": "Core endpoint token required"}, status_code=401,
                                      headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
        # Core's security middleware inspects /v1 paths; strip the mount prefix.
        core_scope = dict(scope, path=path, raw_path=path.encode(), root_path="")
        await serve_core_request(core_scope, receive, send, token=settings.token, allow_unauthenticated=allow_open)
