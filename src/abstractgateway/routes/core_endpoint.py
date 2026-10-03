"""Admin-only control of the host's OpenAI-compatible serving endpoint."""
from __future__ import annotations

import asyncio
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool

from ..core_endpoint import change_settings, read_settings
from ..users import gateway_data_dir_from_env
from .apps import _browser_gateway_url
from .gateway import _require_admin_principal

router = APIRouter(prefix="/gateway/admin/core-endpoint", tags=["core-endpoint"])


class EndpointChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: Optional[StrictBool] = None
    access: Optional[Literal["token", "open"]] = None


def _status(request, settings):
    return {"enabled": settings.enabled, "access": settings.access,
            "token_present": bool(settings.token),
            "base_url": _browser_gateway_url(request).rstrip("/") + "/core/v1"}


def _response(body):
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


@router.get("")
async def status(request: Request):
    _require_admin_principal(request)
    settings = await asyncio.to_thread(read_settings, gateway_data_dir_from_env())
    return _response(_status(request, settings))


@router.post("")
async def change(request: Request, body: EndpointChange):
    _require_admin_principal(request)
    try:
        settings = await asyncio.to_thread(change_settings, gateway_data_dir_from_env(),
                                          enabled=body.enabled, access=body.access)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    request.state.audit_detail = {"setting_change": {"setting": "core_endpoint", "enabled": settings.enabled, "access": settings.access}}
    return _response(_status(request, settings))


@router.post("/token/reveal")
async def reveal(request: Request):
    _require_admin_principal(request)
    settings = await asyncio.to_thread(read_settings, gateway_data_dir_from_env())
    if not settings.token:
        raise HTTPException(status_code=404, detail="Generate a Core endpoint token first")
    request.state.audit_detail = {"core_endpoint_token_action": "reveal"}
    return _response({"token": settings.token})


@router.post("/token/rotate")
async def rotate(request: Request):
    _require_admin_principal(request)
    settings = await asyncio.to_thread(change_settings, gateway_data_dir_from_env(), rotate=True)
    request.state.audit_detail = {"core_endpoint_token_action": "rotate"}
    return _response({"token": settings.token, **_status(request, settings)})
