"""The OpenAI API page's doors (contract `gateway_openai_api_v1`).

    GET  /gateway/openai-api                    status for every signed-in account (an admin sees the access settings)
    GET  /gateway/openai-api/logs?limit=        recent requests (an admin: all; anyone else: their own)
    GET  /gateway/openai-api/logs/{request_id}  one request with its recorded request and response (redacted)
    POST /gateway/admin/core-endpoint {enabled?, access?, reach?, open_account?}   admin: applies immediately
    POST /gateway/admin/core-endpoint/restart   admin: ends open requests, keeps the settings
    POST /gateway/admin/core-endpoint/check     admin: plain setup checks
    GET  /gateway/admin/core-endpoint           admin: the same status (0.12.0 door)
    POST /gateway/admin/core-endpoint/token/rotate   admin: a new internal endpoint key (deprecated)

    GET    /gateway/me/openai-keys                       my named API keys (label, created, last used, fingerprint)
    POST   /gateway/me/openai-keys {label}               a new named key, answered once
    DELETE /gateway/me/openai-keys/{fingerprint}         revoke one (immediate)
    GET    /gateway/admin/accounts/{id}/openai-keys      admin: an account's keys
    DELETE /gateway/admin/accounts/{id}/openai-keys/{fp} admin: revoke one

A caller's API key is one of their named API keys (round 16, `abstractgateway.openai_keys`:
valid at /v1 only); their gateway token is still accepted at /v1 for compatibility. The
semantics live in `abstractgateway.core_endpoint`. No route answers a stored key or token: a
new key is answered once, by the request that makes it.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

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
    open_account: Optional[str] = Field(default=None, min_length=1, max_length=200)


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
        if settings.open_account == ce.GUEST_ACCOUNT:
            text = f"Without a key, {who} can use your local models as Guest (models only). Cloud providers still need a key."
        else:
            text = f"Without a key, {who} can use this API as {settings.open_account}, with that account's models and providers."
        out.append({"id": "open", "tone": "warn", "text": text})
    if settings.reach == "anywhere":
        out.append({"id": "anywhere", "tone": "warn",
                    "text": "Reachable from the internet through your proxy or tunnel. Keep your key private."})
    listener = str((network.get("effective") or {}).get("mode") or "unknown")
    if settings.reach in {"network", "tailnet", "anywhere"} and listener == "localhost":
        out.append({"id": "listener", "tone": "info",
                    "text": "The gateway listens on this computer only. Other devices reach it after you change "
                            "Network, or through a proxy here (tailscale serve)."})
    return out


def _key(principal) -> Dict[str, Any]:
    """The caller's keys, described (never a key): `named_keys` true = this account makes named API
    keys (GET/POST /me/openai-keys), which is what the page offers; false = the operator's own
    token, not an account. `fingerprint` is the first 12 hex digits of the SHA-256 of the gateway
    token (still accepted at /v1 for compatibility)."""
    from ..users import GatewayUserRegistry

    own = principal.source == "user-registry"
    rec = GatewayUserRegistry().get_user(str(principal.user_id), tenant_id=str(principal.tenant_id or "default")) if own else None
    return {"own_token": own, "user_id": principal.user_id,
            "named_keys": bool(own and rec is not None and rec.principal_kind != "entity"),
            "fingerprint": (rec.token_fingerprint if rec is not None else principal.token_fingerprint) or None,
            # May this account call /v1 at all (its Accounts switch)?
            "allowed": bool(rec.openai_api_allowed()) if rec is not None else True}


def _status(request: Request, settings: ce.EndpointSettings, data_dir, *, admin: bool,
            listed: Optional[List[str]] = None) -> Dict[str, Any]:
    """The page's data. Everyone: running or not, the base URL, their own key, the docs. An admin
    also gets the access settings (authentication, who can connect, who Open mode runs as)."""
    principal = _principal_from_request(request)
    base = _browser_gateway_url(request).rstrip("/")
    out: Dict[str, Any] = {
        "schema": SCHEMA,
        "role": "admin" if admin else "user",
        "writable": admin,
        "enabled": settings.enabled,
        "running": settings.enabled,
        "base_url": base + ce.BASE_PATH,
        "key": _key(principal),
        "docs": {"openai_api": DOCS_URL, "abstractcore": CORE_DOCS_URL},
        # tested: checked with the official openai SDK; served: AbstractCore answers when an engine
        # for it is set up; not_yet: refused with a standard 400 or not served (404).
        "support": ce.SUPPORT,
        # The model the snippets use: the gateway's default text model when
        # /v1/models lists it, else the first listed text model; null when the
        # API is stopped or lists none.
        "example_model": _example_model(listed),
    }
    if not admin:
        return out
    network = _network(data_dir)
    tailscale = network.get("tailscale")
    out.update({
        "access": settings.access,
        "reach": settings.reach,
        "legacy_base_url": base + ce.LEGACY_PREFIX + ce.BASE_PATH,
        "token_present": bool(settings.token),
        "reach_options": _reach_options(data_dir, settings, tailscale),
        "open_account": settings.open_account,
        "open_account_options": ce.open_account_options(settings),
        "warnings": _warnings(settings, network),
        "listener": {"mode": (network.get("effective") or {}).get("mode"),
                     "label": (network.get("effective") or {}).get("label")},
        "tailscale": tailscale,
        "open_requests": ce.inflight_count(),
    })
    return out


def _example_model(listed: Optional[List[str]]) -> Optional[str]:
    from ..provider_defaults import _gateway_capability_text_default

    if not listed:
        return None
    provider, model, _source = _gateway_capability_text_default()
    default = f"{provider}/{model}" if provider and model else None
    return default if default in listed else listed[0]


async def _listed(settings: ce.EndpointSettings) -> List[str]:
    if not settings.enabled:
        return []
    try:
        return await asyncio.wait_for(ce.listed_text_models(settings), timeout=10)
    except Exception:  # noqa: BLE001 - the snippet then shows provider/model
        return []


def _response(body):
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


@user_router.get("")
async def openai_api_status(request: Request):
    principal = _principal_from_request(request)
    data_dir = gateway_data_dir_from_env()
    settings = await asyncio.to_thread(ce.read_settings, data_dir)
    body = await asyncio.to_thread(_status, request, settings, data_dir, admin=bool(principal.is_admin()),
                                   listed=await _listed(settings))
    return _response(body)


@user_router.get("/logs")
async def openai_api_logs(request: Request, limit: int = Query(default=50, ge=1, le=500)):
    principal = _principal_from_request(request)
    own = None if principal.is_admin() else str(principal.user_id)
    rows = await asyncio.to_thread(ce.recent_requests, limit=limit, user_id=own,
                                   tenant_id=str(principal.tenant_id or "default"))
    return _response({"schema": SCHEMA, "rows": rows, "scope": "all" if own is None else "own"})


@user_router.get("/logs/{request_id}")
async def openai_api_log_record(request: Request, request_id: str):
    """One request with what was recorded (keys and tokens removed when written). Anyone but an
    admin reads only their own: someone else's id answers 404, like an unknown one."""
    principal = _principal_from_request(request)
    own = None if principal.is_admin() else str(principal.user_id)
    row = await asyncio.to_thread(ce.request_record, request_id[:200], user_id=own,
                                  tenant_id=str(principal.tenant_id or "default"))
    if row is None:
        raise HTTPException(status_code=404, detail="There is no such request in the log.")
    return _response({"schema": SCHEMA, "row": row})


@router.get("")
async def status(request: Request):
    _require_admin_principal(request)
    data_dir = gateway_data_dir_from_env()
    settings = await asyncio.to_thread(ce.read_settings, data_dir)
    return _response(await asyncio.to_thread(_status, request, settings, data_dir, admin=True, listed=await _listed(settings)))


@router.post("")
async def change(request: Request, body: EndpointChange):
    _require_admin_principal(request)
    data_dir = gateway_data_dir_from_env()
    try:
        settings = await asyncio.to_thread(ce.change_settings, data_dir, enabled=body.enabled, access=body.access,
                                           reach=body.reach, open_account=body.open_account)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    ended = ce.end_inflight_requests() if body.enabled is False else 0
    request.state.audit_detail = {"setting_change": {"setting": "core_endpoint", "enabled": settings.enabled,
                                                     "access": settings.access, "reach": settings.reach,
                                                     "open_account": settings.open_account}}
    out = await asyncio.to_thread(_status, request, settings, data_dir, admin=True, listed=await _listed(settings))
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
    out = await asyncio.to_thread(_status, request, settings, data_dir, admin=True, listed=await _listed(settings))
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


@router.post("/token/rotate")
async def rotate(request: Request):
    _require_admin_principal(request)
    data_dir = gateway_data_dir_from_env()
    settings = await asyncio.to_thread(ce.change_settings, data_dir, rotate=True)
    request.state.audit_detail = {"core_endpoint_token_action": "rotate"}
    return _response({"token": settings.token, **(await asyncio.to_thread(_status, request, settings, data_dir, admin=True))})


# ---- Named API keys for /v1 (round 16, backlog 1000; semantics in `abstractgateway.openai_keys`) ----

keys_router = APIRouter(prefix="/gateway", tags=["openai-api"])


class OpenAIKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(..., max_length=200, description="The key's name, e.g. the app that will use it.")


def _key_error(exc) -> HTTPException:
    return HTTPException(status_code=int(exc.status), detail={"reason_code": exc.code, "message": str(exc)})


def _own_account(request: Request):
    """The signed-in person's registry account, or 409: named keys belong to an account."""
    from ..openai_keys import NO_ACCOUNT
    from ..users import GatewayUserRegistry

    principal = _principal_from_request(request)
    rec = GatewayUserRegistry().get_user(str(principal.user_id), tenant_id=str(principal.tenant_id or "default"))
    if rec is None or principal.source != "user-registry":
        raise HTTPException(status_code=409, detail={"reason_code": "no_account", "message": NO_ACCOUNT})
    return principal, rec


def _note_key_change(request: Request, *, action: str, user_id: str, tenant_id: str, item: Dict[str, Any]) -> None:
    """The audit line says which key was made or revoked (name and fingerprint, never the key)."""
    detail = getattr(request.state, "audit_detail", None)
    detail = dict(detail) if isinstance(detail, dict) else {}
    detail["openai_key_change"] = {"action": action, "user_id": user_id, "tenant_id": tenant_id,
                                   "label": item.get("label"), "fingerprint": item.get("fingerprint")}
    request.state.audit_detail = detail


@keys_router.get("/me/openai-keys", summary="My named API keys for the OpenAI API")
async def my_openai_keys(request: Request):
    from .. import openai_keys as ok

    _principal, rec = await asyncio.to_thread(_own_account, request)
    keys = await asyncio.to_thread(ok.list_keys, rec.user_id, rec.tenant_id)
    return _response({"schema": SCHEMA, "account": rec.user_id, "keys": keys or []})


@keys_router.post("/me/openai-keys", summary="Make a named API key (answered once)",
                  description="`{label}` -> `{key, item}`. The key works at `/v1/*` only and is answered only here; "
                              "the gateway keeps its hash and fingerprint.")
async def my_openai_key_create(request: Request, body: OpenAIKeyCreate):
    from .. import openai_keys as ok
    from ..users import OpenAIKeyError

    principal, rec = await asyncio.to_thread(_own_account, request)
    try:
        out = await asyncio.to_thread(ok.create_key, rec.user_id, rec.tenant_id, body.label,
                                      created_by=str(principal.user_id))
    except OpenAIKeyError as exc:
        raise _key_error(exc) from None
    _note_key_change(request, action="create", user_id=rec.user_id, tenant_id=rec.tenant_id, item=out["item"])
    return _response({"schema": SCHEMA, **out})


@keys_router.delete("/me/openai-keys/{fingerprint}", summary="Revoke one of my named API keys (immediate)")
async def my_openai_key_revoke(request: Request, fingerprint: str):
    from .. import openai_keys as ok
    from ..users import OpenAIKeyError

    _principal, rec = await asyncio.to_thread(_own_account, request)
    try:
        gone = await asyncio.to_thread(ok.revoke_key, rec.user_id, rec.tenant_id, fingerprint)
    except OpenAIKeyError as exc:
        raise _key_error(exc) from None
    _note_key_change(request, action="revoke", user_id=rec.user_id, tenant_id=rec.tenant_id, item=gone)
    return _response({"schema": SCHEMA, "revoked": gone})


@keys_router.get("/admin/accounts/{account_id}/openai-keys", summary="An account's named API keys (admin)")
async def account_openai_keys(request: Request, account_id: str, tenant_id: str = Query(default="default")):
    from .. import openai_keys as ok

    _require_admin_principal(request)
    keys = await asyncio.to_thread(ok.list_keys, account_id, tenant_id)
    if keys is None:
        raise HTTPException(status_code=404, detail={"reason_code": "account_not_found",
                                                     "message": f"There is no account named {account_id!r} on this gateway."})
    return _response({"schema": SCHEMA, "account": account_id, "keys": keys})


@keys_router.delete("/admin/accounts/{account_id}/openai-keys/{fingerprint}",
                    summary="Revoke an account's named API key (admin, immediate)")
async def account_openai_key_revoke(request: Request, account_id: str, fingerprint: str,
                                    tenant_id: str = Query(default="default")):
    from .. import openai_keys as ok
    from ..users import OpenAIKeyError
    from .gateway import _audit_account_change

    _require_admin_principal(request)
    try:
        gone = await asyncio.to_thread(ok.revoke_key, account_id, tenant_id, fingerprint)
    except OpenAIKeyError as exc:
        raise _key_error(exc) from None
    _note_key_change(request, action="revoke", user_id=account_id, tenant_id=tenant_id, item=gone)
    _audit_account_change(request, user_id=account_id, tenant_id=tenant_id,
                          changes={"openai_key_revoked": gone.get("label")})
    return _response({"schema": SCHEMA, "revoked": gone})
