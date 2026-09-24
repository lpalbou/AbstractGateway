"""`/api/gateway/network…`: network exposure + reachable addresses (contract `gateway_network_v1`).

    GET  /network?lookup_public=0|1     status: configured vs effective, auth, addresses, warnings
    POST /network {mode?, port?, acknowledge_internet?, allowed_origins?, trust_proxy?}
                                         store a change      400 / 409 refused
    POST /network/restart                restart to apply (host_control)     409 when it cannot apply

GET is visibility for every authenticated principal (the addresses are what
a client needs to connect another device); `lookup_public=1` makes ONE
outbound HTTPS call and is admin-only. Both POSTs are admin-only: a policy
row in `security/authorization.py` AND `_require_admin_principal` here.
The semantics live in `abstractgateway.network_exposure`.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .. import network_exposure as ne
from .gateway import _host_control_actor, _principal_from_request, _require_admin_principal

router = APIRouter(prefix="/gateway", tags=["network"])


class NetworkChangeRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"mode": "localhost"},
                {"mode": "lan", "port": 8080},
                {"mode": "internet", "acknowledge_internet": True},
                {"allowed_origins": ["https://gateway.example.com"], "trust_proxy": True},
            ]
        },
    )

    mode: Optional[str] = Field(default=None, description="localhost | lan | internet; omitted keeps the stored mode.")
    port: Optional[int] = Field(default=None, description="Listening port (1-65535); omitted keeps the stored port.")
    acknowledge_internet: bool = Field(
        default=False,
        description="Required (true) for mode=internet: the caller has shown the warnings (no TLS here, port forwarding is the user's).",
    )
    allowed_origins: Optional[List[str]] = Field(
        default=None,
        description="Browser origins allowed besides this machine's own (scheme://host[:port], no path, no trailing "
        "slash; '*' only on purpose). Replaces the stored list; [] clears it. Applies to the next request.",
    )
    trust_proxy: Optional[StrictBool] = Field(
        default=None,
        description="Take the client address from X-Forwarded-For (only behind your own proxy). Applies to the next request.",
    )


class NetworkRestartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    force: bool = Field(default=False, description="Restart even when no change is pending.")


def _data_dir():
    from ..users import gateway_data_dir_from_env

    return gateway_data_dir_from_env()


def _is_admin(request: Request) -> bool:
    try:
        return bool(_principal_from_request(request).is_admin())
    except HTTPException:
        raise
    except Exception:
        return False


@router.get("/network")
async def network_status(
    request: Request,
    lookup_public: bool = Query(False, description="Also look up the WAN address (one outbound HTTPS call; admin; internet mode only)."),
) -> Dict[str, Any]:
    """Who can reach this gateway (configured vs effective), with every address a client can use."""
    _principal_from_request(request)
    admin = _is_admin(request)
    if lookup_public and not admin:
        raise HTTPException(status_code=403, detail="Admin principal required for the WAN address lookup")
    return await asyncio.to_thread(ne.network_status, _data_dir(), lookup_public=bool(lookup_public), is_admin=admin)


@router.post("/network")
async def network_change(request: Request, req: NetworkChangeRequest) -> Any:
    """Store a network change. Admin only. Mode/port apply at the next start;
    allowed_origins/trust_proxy apply to the next request.

    400 with `errors[]` for invalid origins; 409 with `refused_reason` + `fix`
    when the mode's auth requirement is not met, or `internet` lacks
    `acknowledge_internet: true`; nothing is stored on either. Every attempt
    lands in the audit log (the security middleware's line for this request)
    with `setting_change` = the fields changed (from/to) or the refusal."""
    principal = _require_admin_principal(request)
    actor = _host_control_actor(principal)
    status, body = await asyncio.to_thread(
        ne.apply_network_change,
        _data_dir(),
        mode=req.mode,
        port=req.port,
        acknowledge_internet=bool(req.acknowledge_internet),
        allowed_origins=req.allowed_origins,
        trust_proxy=req.trust_proxy,
        actor=actor,
    )
    request.state.audit_detail = {
        "setting_change": {
            "setting": "network",
            "actor": actor,
            "ok": status == 200,
            "changed": body.get("changed") if status == 200 else None,
            "refused": None if status == 200 else {"reason_code": body.get("reason_code"), "reason": body.get("refused_reason")},
        }
    }
    if status != 200:
        return JSONResponse(status_code=status, content=body)
    return body


@router.post("/network/restart")
async def network_restart(request: Request, req: Optional[NetworkRestartRequest] = None) -> Any:
    """Restart this gateway so the stored exposure applies. Admin only.

    Refused (409, nothing restarted) when a restart cannot apply the setting:
    `serve --host/--port` pins the bind, the mode's auth is not met, no
    change is pending (unless `force`), or this process cannot relaunch
    itself (the reason names the manual restart)."""
    principal = _require_admin_principal(request)
    status = await asyncio.to_thread(ne.network_status, _data_dir(), is_admin=True)
    story = status["restart"]
    refuse = None
    if not story.get("applies"):
        refuse = story.get("reason")
    elif not status.get("restart_required") and not (req and req.force):
        refuse = "nothing to apply: the running bind already matches the setting (send force: true to restart anyway)"
    elif not story.get("available"):
        refuse = f"this process cannot restart itself ({story.get('unavailable_reason')}); {story.get('how')}"
    if refuse:
        return JSONResponse(
            status_code=409,
            content={"ok": False, "restart": False, "refused_reason": refuse, "restart_story": story},
        )
    from .. import host_control

    try:
        res = host_control.request_restart(
            by=_host_control_actor(principal),
            reason=f"apply network exposure '{status['configured']['mode']}' on port {status['configured']['port']}",
        )
    except host_control.HostControlError as e:
        return JSONResponse(status_code=409, content={"ok": False, "restart": False, "refused_reason": str(e), "restart_story": story})
    out = dict(res)
    out["next"] = {
        "mode": status["configured"]["mode"],
        "port": status["configured"]["port"],
        "reconnect_url": f"http://127.0.0.1:{status['configured']['port']}",
    }
    return out
