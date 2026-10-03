"""The OpenAI API page's doors (contract `gateway_openai_api_v1`).

    GET  /gateway/openai-api                    status for every signed-in account
    GET  /gateway/openai-api/logs?limit=        recent requests (an admin: all; anyone else: their own)
    POST /gateway/admin/core-endpoint {enabled?, access?, reach?}   admin: applies immediately
    POST /gateway/admin/core-endpoint/restart   admin: ends open requests, keeps the settings
    POST /gateway/admin/core-endpoint/check     admin: plain setup checks
    GET  /gateway/admin/core-endpoint           admin: the same status (0.12.0 door)
    POST /gateway/admin/core-endpoint/token/*   admin: the 0.12.0 endpoint key (deprecated)

A caller's API key is their own gateway token; the semantics live in
`abstractgateway.core_endpoint`.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool

from .. import core_endpoint as ce
from .. import network_exposure as ne
from ..users import gateway_data_dir_from_env
from .apps import _browser_gateway_url
from .gateway import _principal_from_request, _require_admin_principal

router = APIRouter(prefix="/gateway/admin/core-endpoint", tags=["openai-api"])
user_router = APIRouter(prefix="/gateway/openai-api", tags=["openai-api"])

SCHEMA = "gateway_openai_api_v1"
DOCS_URL = "https://github.com/lpalbou/abstractgateway/blob/main/docs/openai-api.md"
CORE_DOCS_URL = "https://github.com/lpalbou/AbstractCore/blob/main/docs/server.md"


class EndpointChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: Optional[StrictBool] = None
    access: Optional[Literal["token", "open"]] = None
    reach: Optional[Literal["machine", "network", "tailnet", "anywhere"]] = None


def _network(data_dir) -> Dict[str, Any]:
    return ne.network_status(data_dir, is_admin=False)


def _reach_options(data_dir, settings: ce.EndpointSettings, tailscale) -> List[Dict[str, Any]]:
    anywhere = ce.anywhere_allowed(data_dir)
    out = []
    for rid in ce.REACH_MODES:
        row: Dict[str, Any] = {"id": rid, "label": ce.REACH_LABELS[rid], "selected": settings.reach == rid,
                               "available": True}
        if rid == "tailnet":
            # Offered only when this device is on a tailnet (or already chosen).
            row["shown"] = bool(tailscale) or settings.reach == "tailnet"
        if rid == "anywhere":
            if anywhere:
                row.update(available=False, reason=anywhere)
            elif settings.access == "open":
                row.update(available=False, reason="Anywhere needs a key: choose Protected first.")
        out.append(row)
    return out


def _warnings(settings: ce.EndpointSettings, network: Dict[str, Any]) -> List[Dict[str, str]]:
    """What the current choice means, in plain words (tone: warn | info)."""
    out: List[Dict[str, str]] = []
    if settings.access == "open":
        who = {"machine": "any program on this machine", "network": "any device on your network",
               "tailnet": "any device on your network or tailnet"}.get(settings.reach, "anyone who reaches it")
        out.append({"id": "open", "tone": "warn",
                    "text": f"Without a key, {who} can use your local models. Cloud providers still need a key."})
    if settings.reach == "anywhere":
        out.append({"id": "anywhere", "tone": "warn",
                    "text": "Reachable from the internet through your proxy or tunnel. Keep your key private."})
    listener = str((network.get("effective") or {}).get("mode") or "unknown")
    if settings.reach in {"network", "tailnet", "anywhere"} and listener == "localhost":
        out.append({"id": "listener", "tone": "info",
                    "text": "The gateway listens on this computer only. Other devices reach it after you change "
                            "Network, or through a proxy here (tailscale serve)."})
    return out


def _status(request: Request, settings: ce.EndpointSettings, data_dir, *, admin: bool) -> Dict[str, Any]:
    principal = _principal_from_request(request)
    network = _network(data_dir)
    tailscale = network.get("tailscale")
    base = _browser_gateway_url(request).rstrip("/")
    return {
        "schema": SCHEMA,
        "writable": admin,
        "enabled": settings.enabled,
        "running": settings.enabled,
        "access": settings.access,
        "reach": settings.reach,
        "base_url": base + ce.BASE_PATH,
        "legacy_base_url": base + ce.LEGACY_PREFIX + ce.BASE_PATH,
        "token_present": bool(settings.token),
        "reach_options": _reach_options(data_dir, settings, tailscale),
        "warnings": _warnings(settings, network),
        "listener": {"mode": (network.get("effective") or {}).get("mode"),
                     "label": (network.get("effective") or {}).get("label")},
        "tailscale": tailscale,
        "open_requests": ce.inflight_count(),
        # The key is the caller's own gateway token: rotatable by its owner
        # only when the account has a token of its own (not the operator token).
        "key": {"own_token": principal.source == "user-registry", "user_id": principal.user_id},
        "docs": {"openai_api": DOCS_URL, "abstractcore": CORE_DOCS_URL},
        # The gateway's default text model (provider/model) for the snippets, or null.
        "example_model": _example_model(),
    }


def _example_model() -> Optional[str]:
    from ..provider_defaults import _gateway_capability_text_default

    provider, model, _source = _gateway_capability_text_default()
    return f"{provider}/{model}" if provider and model else None


def _response(body):
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


@user_router.get("")
async def openai_api_status(request: Request):
    principal = _principal_from_request(request)
    data_dir = gateway_data_dir_from_env()
    settings = await asyncio.to_thread(ce.read_settings, data_dir)
    body = await asyncio.to_thread(_status, request, settings, data_dir, admin=bool(principal.is_admin()))
    return _response(body)


@user_router.get("/logs")
async def openai_api_logs(request: Request, limit: int = Query(default=50, ge=1, le=500)):
    principal = _principal_from_request(request)
    own = None if principal.is_admin() else str(principal.user_id)
    rows = await asyncio.to_thread(ce.recent_requests, limit=limit, user_id=own,
                                   tenant_id=str(principal.tenant_id or "default"))
    return _response({"schema": SCHEMA, "rows": rows, "scope": "all" if own is None else "own"})


@router.get("")
async def status(request: Request):
    _require_admin_principal(request)
    data_dir = gateway_data_dir_from_env()
    settings = await asyncio.to_thread(ce.read_settings, data_dir)
    return _response(await asyncio.to_thread(_status, request, settings, data_dir, admin=True))


@router.post("")
async def change(request: Request, body: EndpointChange):
    _require_admin_principal(request)
    data_dir = gateway_data_dir_from_env()
    try:
        settings = await asyncio.to_thread(ce.change_settings, data_dir,
                                           enabled=body.enabled, access=body.access, reach=body.reach)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    ended = ce.end_inflight_requests() if body.enabled is False else 0
    request.state.audit_detail = {"setting_change": {"setting": "core_endpoint", "enabled": settings.enabled,
                                                     "access": settings.access, "reach": settings.reach}}
    out = await asyncio.to_thread(_status, request, settings, data_dir, admin=True)
    out["ended_requests"] = ended
    return _response(out)


@router.post("/restart")
async def restart(request: Request):
    _require_admin_principal(request)
    data_dir = gateway_data_dir_from_env()
    settings = await asyncio.to_thread(ce.read_settings, data_dir)
    if not settings.enabled:
        raise HTTPException(status_code=409, detail="The OpenAI API is stopped: turn on Endpoint first.")
    ended = ce.end_inflight_requests()
    request.state.audit_detail = {"core_endpoint_action": "restart", "ended_requests": ended}
    out = await asyncio.to_thread(_status, request, settings, data_dir, admin=True)
    out["ended_requests"] = ended
    return _response(out)


@router.post("/check")
async def check(request: Request):
    _require_admin_principal(request)
    data_dir = gateway_data_dir_from_env()
    network = await asyncio.to_thread(_network, data_dir)
    try:
        checks = await ce.check_setup(data_dir, network=network)
    except asyncio.TimeoutError:
        checks = [{"id": "models", "ok": False, "text": "Listing models took longer than 20 s."}]
    return _response({"schema": SCHEMA, "checks": checks, "ok": all(c.get("ok") is not False for c in checks)})


@router.post("/token/reveal")
async def reveal(request: Request):
    _require_admin_principal(request)
    settings = await asyncio.to_thread(ce.read_settings, gateway_data_dir_from_env())
    if not settings.token:
        raise HTTPException(status_code=404, detail="Generate a Core endpoint token first")
    request.state.audit_detail = {"core_endpoint_token_action": "reveal"}
    return _response({"token": settings.token})


@router.post("/token/rotate")
async def rotate(request: Request):
    _require_admin_principal(request)
    data_dir = gateway_data_dir_from_env()
    settings = await asyncio.to_thread(ce.change_settings, data_dir, rotate=True)
    request.state.audit_detail = {"core_endpoint_token_action": "rotate"}
    return _response({"token": settings.token, **(await asyncio.to_thread(_status, request, settings, data_dir, admin=True))})
