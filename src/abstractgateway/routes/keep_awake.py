"""`GET /api/gateway/host/keep-awake`: is this gateway keeping its Mac awake for remote access? (`gateway_keep_awake_v1`)

Visibility for every authenticated principal, like `/host/runner`: it says
whether the machine stays reachable, nothing more. The setting is written
through the one settings door, `POST /api/gateway/admin/runtime-config
{"keep_awake": true|false|null}` (admin), which re-checks at once and returns
this payload as `keep_awake_status`. The semantics live in
`abstractgateway.keep_awake`.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Request

from .. import keep_awake
from ..work_pools import off_loop
from .gateway import _principal_from_request

router = APIRouter(prefix="/gateway", tags=["host"])


@router.get("/host/keep-awake")
async def host_keep_awake(request: Request) -> Dict[str, Any]:
    """Whether this Mac is kept awake for remote access, and why (state, line, detail, setting, remote_access)."""
    _principal_from_request(request)
    return await off_loop("light", keep_awake.controller().status)
