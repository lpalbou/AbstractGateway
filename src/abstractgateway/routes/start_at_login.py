"""`/api/gateway/host/start-at-login`: start THIS gateway at login (contract `gateway_start_at_login_v1`).

    GET  /host/start-at-login                       {enabled, state, mechanism, can_change, reason, summary, ...}
    PUT  /host/start-at-login {enabled, replace_other?}
                                                     200 read-back · 409 refused · 500 did not read back

Both are admin-only (a policy row in `security/authorization.py` AND
`_require_admin_principal` here): the answer names files and programs on the
host, and the change registers a login item for the account running the
gateway. The registration itself is `abstractgateway.autostart` (LaunchAgent
/ systemd user unit, XDG autostart fallback / Windows Run value) — the same
switch the tray and `abstractgateway service enable|disable` flip.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .. import autostart
from .gateway import _host_control_actor, _require_admin_principal

router = APIRouter(prefix="/gateway", tags=["host"])


class StartAtLoginRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"enabled": True}, {"enabled": False}, {"enabled": True, "replace_other": True}]},
    )

    enabled: StrictBool = Field(description="true registers this gateway for the next login; false unregisters it.")
    replace_other: StrictBool = Field(
        default=False,
        description="Confirm replacing a registration that starts ANOTHER gateway (another data folder).",
    )


def _data_dir():
    from ..users import gateway_data_dir_from_env

    return gateway_data_dir_from_env()


@router.get("/host/start-at-login")
async def start_at_login_status(request: Request) -> Dict[str, Any]:
    """Would this gateway start at the next login, and can it be changed from here?"""
    _require_admin_principal(request)
    return await asyncio.to_thread(autostart.start_at_login_status, data_dir=_data_dir())


@router.put("/host/start-at-login")
async def start_at_login_change(request: Request, req: StartAtLoginRequest) -> Any:
    """Turn start-at-login on or off. Never starts or stops the running
    gateway; the body carries the read-back (`start_at_login`)."""
    principal = _require_admin_principal(request)
    actor = _host_control_actor(principal)
    status, body = await asyncio.to_thread(
        autostart.set_start_at_login,
        data_dir=_data_dir(),
        enabled=bool(req.enabled),
        replace_other=bool(req.replace_other),
        actor=actor,
    )
    request.state.audit_detail = {
        "setting_change": {
            "setting": "start_at_login",
            "actor": actor,
            "ok": bool(body.get("ok")),
            "to": bool(req.enabled),
            "changed": body.get("changed"),
            "refused": body.get("refused_reason") or body.get("error"),
        }
    }
    if status != 200:
        return JSONResponse(status_code=status, content=body)
    return body
