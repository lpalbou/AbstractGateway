"""`/api/gateway/sessions/{session_id}/tool-policy`: a conversation's standing tool decision.

GET reads it, PUT replaces it, POST merges into it, DELETE resets it (contract v1,
`session_tool_policy.py`). The runtime consults it BEFORE it decides whether a tool batch waits
for approval: a covered call runs with no wait and no interaction, a denied call is refused with
no wait, and each decision is an `abstract.tool_policy_decision` record in the run's ledger.

Scope: the caller's own plane (multi-user mode selects a data root per principal), so another
account's session is simply a different, empty row. The gateway clamps: approve selectors expand
to the tools `GET /discovery/tools` serves the caller as enabled; the rest is listed in `clamped`.
Every change is audited (`session.tool_policy_set` / `session.tool_policy_reset`).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException, Request

from ..session_tool_policy import (
    PolicyRequestError,
    build_policy,
    merge_requests,
    normalize_request,
    session_tool_policy_store,
    split_client,
)
from ..work_pools import off_loop
from .session_archive import _audit, _note

router = APIRouter(prefix="/gateway", tags=["sessions"])

_SESSION_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")


def _sid(session_id: str) -> str:
    sid = str(session_id or "").strip()
    if not _SESSION_ID.fullmatch(sid):
        raise HTTPException(status_code=422, detail={"reason_code": "invalid_request",
                                                     "message": "session_id must be 1-256 characters [A-Za-z0-9_.:-]"})
    return sid


def _store() -> Any:
    from ..service import get_gateway_service

    return session_tool_policy_store(Path(get_gateway_service().host.data_dir))


def _invalid(exc: Exception) -> HTTPException:
    return HTTPException(status_code=422, detail={"reason_code": "invalid_request", "message": str(exc)})


def _conflict(exc: Any) -> HTTPException:
    return HTTPException(status_code=409, detail={"reason_code": "revision_conflict", "message": str(exc),
                                                  "revision": getattr(exc, "actual", None)})


async def _write(request: Request, session_id: str, body: Any, *, merge: bool) -> Dict[str, Any]:
    from abstractruntime.storage.session_tool_policy import SessionToolPolicyConflict

    from .gateway import _principal_from_request, discovery_tools

    principal = _principal_from_request(request)
    sid = _sid(session_id)
    try:
        client, if_revision = split_client(body)
        req = normalize_request(body)
    except PolicyRequestError as exc:
        raise _invalid(exc) from None
    store = _store()
    if merge:
        current = await off_loop("light", store.get, sid)
        req = merge_requests((current or {}).get("request"), req)
    # The caller's authorized tools: the same rows (and the same per-account gates) discovery serves.
    inventory = await discovery_tools()
    if inventory.get("error"):
        raise HTTPException(status_code=503, detail={"reason_code": "tool_inventory_unavailable", "message": str(inventory["error"])})
    set_by = {"tenant_id": principal.tenant_id, "user_id": principal.user_id, "client": client}
    policy = build_policy(req, inventory.get("items") or [], set_by=set_by)
    try:
        stored = await off_loop("light", lambda: store.put(sid, policy, if_revision=if_revision))
    except SessionToolPolicyConflict as exc:
        raise _conflict(exc) from None
    _note(request, sid, "tool_policy_set")
    _audit("session.tool_policy_set", session_id=sid, actor=principal.user_id, changed=True)
    return {"session_id": sid, "policy": stored}


@router.get("/sessions/{session_id}/tool-policy", summary="A conversation's tool policy")
async def get_session_tool_policy(request: Request, session_id: str) -> Dict[str, Any]:
    from .gateway import _principal_from_request

    _principal_from_request(request)
    sid = _sid(session_id)
    store = _store()
    policy = await off_loop("light", store.get, sid)
    return {"session_id": sid, "policy": policy, "revision": await off_loop("light", store.revision, sid)}


@router.put(
    "/sessions/{session_id}/tool-policy",
    summary="Replace a conversation's tool policy",
    description=(
        "Body `{approve: {tools, kinds, all, max_risk_rank}, deny: {tools, kinds}, ask, client, if_revision}`. "
        "Approve selectors expand to the tools the gateway authorizes for the caller (`GET /discovery/tools`, enabled "
        "rows); anything else is listed in `clamped`. Applies from the next tool batch of every run of the session."
    ),
)
async def put_session_tool_policy(request: Request, session_id: str, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    return await _write(request, session_id, body, merge=False)


@router.post("/sessions/{session_id}/tool-policy", summary="Add to a conversation's tool policy (merge)")
async def post_session_tool_policy(request: Request, session_id: str, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    return await _write(request, session_id, body, merge=True)


@router.delete("/sessions/{session_id}/tool-policy", summary="Reset a conversation's tool policy")
async def delete_session_tool_policy(request: Request, session_id: str, if_revision: Optional[int] = None) -> Dict[str, Any]:
    from abstractruntime.storage.session_tool_policy import SessionToolPolicyConflict

    from .gateway import _principal_from_request

    principal = _principal_from_request(request)
    sid = _sid(session_id)
    store = _store()
    actor = {"tenant_id": principal.tenant_id, "user_id": principal.user_id}
    try:
        revision = await off_loop("light", lambda: store.delete(sid, if_revision=if_revision, actor=actor))
    except SessionToolPolicyConflict as exc:
        raise _conflict(exc) from None
    _note(request, sid, "tool_policy_reset")
    _audit("session.tool_policy_reset", session_id=sid, actor=principal.user_id, changed=True)
    return {"session_id": sid, "policy": None, "revision": revision}
