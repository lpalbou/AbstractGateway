"""Visitor-scoped tool choices and durable approval decisions for entity conversations."""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from ..entity_access import entity_name_guard, entity_plane_resolver
from ..entity_session_tools import (SessionToolPolicy, decide, install_policy, normalize_policy,
                                    require_owner, session_state, tool_options)
from ..entity_visits import VisitRefused

router = APIRouter(prefix="/gateway", tags=["session-tools"],
                   dependencies=[Depends(entity_plane_resolver), Depends(entity_name_guard)])


PolicyRequest = SessionToolPolicy


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    tool_policy: Optional[PolicyRequest] = None


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    wait_key: str = Field(min_length=1, max_length=512)
    approved: StrictBool


def _host():
    from .entities import _visit_host

    return _visit_host()


def _refused(exc: Exception):
    if isinstance(exc, VisitRefused):
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail="Entity not found.") from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


@router.get("/automations/defaults")
def automation_defaults() -> dict:
    from abstractruntime.session_history import HISTORY_REPLAY_MAX_TOKENS

    return {"growing_max_tokens": HISTORY_REPLAY_MAX_TOKENS}


@router.get("/tool-policy/capabilities")
def conversation_tool_capabilities() -> dict:
    from abstractruntime.core.tool_scope import resolve_tool_scope
    from ..live_deltas import ABSTRACTRUNTIME_FLOOR

    return {"supported": callable(resolve_tool_scope), "modes": ["off", "auto", "approve"],
            "runtime_floor": ABSTRACTRUNTIME_FLOOR}


@router.get("/entities/{name}/session-tools")
def entity_session_tool_options(name: str) -> dict:
    try:
        return {"supported": True, "transport": "durable-visit", "visit_routes": True,
                "modes": ["off", "auto", "approve"],
                "items": tool_options(_host()._registry, name)}
    except (VisitRefused, KeyError, ValueError) as exc:
        return _refused(exc)


@router.post("/entities/{name}/sessions", deprecated=True)
def open_entity_session(name: str, req: SessionRequest) -> dict:
    """Compatibility alias; new clients use the existing visit/open route."""
    from .entities import OpenVisitRequest, open_visit

    return open_visit(name, OpenVisitRequest(session_id=req.session_id,
                      tool_policy=req.tool_policy or PolicyRequest()))


@router.get("/entities/{name}/visit/{run_id}/state")
def entity_session_state(name: str, run_id: str) -> dict:
    try:
        return session_state(_host(), name, run_id)
    except (VisitRefused, KeyError, ValueError) as exc:
        return _refused(exc)


@router.put("/entities/{name}/visit/{run_id}/tool-policy")
def set_entity_session_tool_policy(name: str, run_id: str, req: PolicyRequest) -> dict:
    from abstractruntime.identity.visit_workflow import VISITOR_WAIT_KEY

    host = _host()
    try:
        er, _run, _wf, _manifest = host._load_visit(name, run_id, need_spec=False)
        lease = host._acquire_lease(er)
        try:
            run = er.run_store.load(run_id)
            old = require_owner(run)
            wait = getattr(run, "waiting", None)
            if str(getattr(run.status, "value", run.status)) != "waiting" or wait is None or wait.wait_key != VISITOR_WAIT_KEY:
                raise VisitRefused(409, "Finish the current reply before changing tools.", code="visit_busy")
            policy = normalize_policy(req.modes, options=tool_options(host._registry, name), owner=old["owner"])
            install_policy(run.vars, policy)
            er.run_store.save(run)
        finally:
            if lease is not None:
                lease.release()
        return session_state(host, name, run_id)
    except (VisitRefused, KeyError, ValueError) as exc:
        return _refused(exc)


@router.post("/entities/{name}/visit/{run_id}/decision")
def entity_session_decision(name: str, run_id: str, req: DecisionRequest) -> dict:
    try:
        return decide(_host(), name, run_id, wait_key=req.wait_key, approved=req.approved)
    except (VisitRefused, KeyError, ValueError) as exc:
        return _refused(exc)


@router.post("/entities/{name}/visit/{run_id}/end", deprecated=True)
def end_entity_session(name: str, run_id: str) -> dict:
    """Compatibility alias; share close's cancellation and queue admission."""
    from .entities import VisitCloseRequest, visit_close

    # Preserve the alias's owner check, including visits opened by older clients.
    try:
        _er, run, _wf, _manifest = _host()._load_visit(name, run_id, need_spec=False)
        require_owner(run)
    except (VisitRefused, KeyError, ValueError) as exc:
        return _refused(exc)
    return visit_close(name, run_id, VisitCloseRequest(closed_by="pause"))
