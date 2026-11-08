"""`PUT /api/gateway/host/keep-awake`: keep the Mac awake while the gateway runs (backlog 1149).

    GET  /host/runner                       carries `keep_awake: {value, source, held, platform_supported}`
    PUT  /host/keep-awake {enabled}         200 the same `keep_awake` shape, read back after the change

Admin-only (a policy row in `security/authorization.py` AND `_require_admin_principal` here).
The change is stored (`host.keep_awake`, runtime_config) and applied LIVE: off releases the
idle-sleep assertion at once, on holds it again — no restart. A `serve --keep-awake` flag pins
the live state for its run: the stored value then changes but applies at the next start
(`source: "flag"`, `stored_value`). Off macOS nothing is held; the setting still stores.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .. import keep_awake
from ..work_pools import off_loop
from .gateway import _host_control_actor, _require_admin_principal

router = APIRouter(prefix="/gateway", tags=["host"])


class KeepAwakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"enabled": True}, {"enabled": False}]})

    enabled: StrictBool = Field(description="true: the Mac does not idle-sleep while the gateway runs; false: it may.")


def _data_dir():
    from ..users import gateway_data_dir_from_env

    return gateway_data_dir_from_env()


def _change(data_dir: Any, enabled: bool, actor: str) -> Dict[str, Any]:
    from ..runtime_config import KEEP_AWAKE_KEY, resolve_keep_awake, write_runtime_config

    write_runtime_config(data_dir, {KEEP_AWAKE_KEY: bool(enabled)}, actor=actor)
    # Live: what is in effect for THIS run (a launch flag wins for its run).
    keep_awake.apply(bool(resolve_keep_awake(data_dir)["value"]))
    return keep_awake.status(data_dir)


@router.put("/host/keep-awake")
async def keep_awake_change(request: Request, req: KeepAwakeRequest) -> Dict[str, Any]:
    """Store the setting and apply it now; the body is the read-back."""
    from ..runtime_config import RuntimeConfigError, RuntimeConfigStoreCorrupt

    principal = _require_admin_principal(request)
    actor = _host_control_actor(principal)
    request.state.audit_detail = {
        "setting_change": {"setting": "host.keep_awake", "actor": actor, "ok": False, "to": bool(req.enabled)}
    }
    try:
        status = await off_loop("light", _change, _data_dir(), bool(req.enabled), actor)
    except RuntimeConfigStoreCorrupt as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except RuntimeConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    request.state.audit_detail["setting_change"].update({"ok": True, "held": bool(status.get("held"))})
    return {"keep_awake": status}
