"""`/api/gateway/engines…`: local engine status, installs, start/stop (contract `gateway_engines_v2`).

    GET  /engines?probe=                       every engine row (+ install_allowed, active_job)
    GET  /engines/jobs                         install jobs, newest first
    GET  /engines/jobs/{job_id}                one job (`engine_install_job_v1`)          404
    POST /engines/jobs/{job_id}/continue {action?}  after needs_admin / needs_tools    404 / 409
    POST /engines/jobs/{job_id}/cancel                                                 404
    GET  /engines/{engine_id}?probe=           one engine row                             404
    POST /engines/{engine_id}/install {dry_run, force, location}   a job            403 / 404 / 409
    POST /engines/{engine_id}/start | /stop    Ollama / LM Studio servers                 409

Every POST is admin-only (policy row in `security/authorization.py` AND
`_require_admin_principal` here); install and continue also need the
runtime-config knob `allow_engine_install`, which with no stored choice is on
for a loopback bind AND for a caller on the gateway machine itself (loopback
or this host's own address, no proxy headers: security/same_machine.py). The job logic is
`abstractgateway.engines_install`; detection comes from AbstractCore through
`core_config`. Moved here from `routes/gateway.py`.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .. import engines_install as ei
from .gateway import _core_host_call, _engine_install_policy, _host_action_error, _principal_from_request, _require_admin_principal

router = APIRouter(prefix="/gateway", tags=["engines"])


class EngineInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"dry_run": False, "force": False, "location": "auto"}]})

    dry_run: bool = Field(default=False, description="Return the plan (steps, command preview, admin/tools needs) without running anything.")
    force: bool = Field(default=False, description="Install again even when the engine is already installed.")
    location: str = Field(
        default="auto",
        description="Apps only (Ollama, LM Studio on macOS): auto = /Applications when this account can write it, else ~/Applications; "
        "user = ~/Applications; system = /Applications (administrator prompt when not writable).",
    )


class EngineJobContinueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"action": "approve_admin"}, {"action": "install_tools"}, {}]})

    action: Optional[str] = Field(
        default=None,
        description="approve_admin (needs_admin: show the OS password dialog and run the stated command) | install_tools "
        "(needs_tools on macOS: open Apple's command-line tools installer) | recheck. Default: the job's first `continue_actions` entry.",
    )


def _job_error(exc: ei.JobStateError) -> Any:
    return _host_action_error(exc.status_code, "refused" if exc.status_code != 404 else "not_found", str(exc), reason=exc.reason)


def _is_admin(request: Request) -> bool:
    try:
        return bool(_principal_from_request(request).is_admin())
    except Exception:
        return False


async def _payload(request: Request, probe: bool) -> Any:
    from ..core_config import core_engine_inventory

    core = await _core_host_call(core_engine_inventory, probe=probe)
    if not isinstance(core, dict):
        return core  # a typed error response (501 / 503)
    registry = ei.default_registry()
    accel = (core.get("host") or {}).get("accelerator")
    installer = await asyncio.to_thread(ei.default_installer, accelerator=accel)
    policy = _engine_install_policy(request)
    out = await asyncio.to_thread(
        ei.engines_payload, installer, core, registry, install_allowed=bool(policy.get("value")), is_admin=_is_admin(request)
    )
    out["install_allowed"] = bool(policy.get("value"))
    out["install_policy"] = policy
    return out


@router.get("/engines")
async def engines_list(request: Request, probe: bool = Query(False, description="GET each local engine server once (short timeouts).")) -> Any:
    """Local inference engines: supported? installed? running? and the one Install action (contract `gateway_engines_v2`)."""
    return await _payload(request, probe)


@router.get("/engines/jobs")
async def engine_jobs_list() -> Any:
    """Engine install jobs in this gateway process, newest first."""
    return {"ok": True, "schema": "engine_install_jobs_v1", "jobs": ei.default_registry().list()}


@router.get("/engines/jobs/{job_id}")
async def engine_job_get(job_id: str) -> Any:
    """One install job: state, percent, bytes, plain-language message, full log in `details`."""
    job = ei.default_registry().get(job_id)
    if job is None:
        return _host_action_error(404, "not_found", f"no job {job_id}")
    return job.snapshot()


@router.post("/engines/jobs/{job_id}/continue")
async def engine_job_continue(job_id: str, request: Request, req: Optional[EngineJobContinueRequest] = None) -> Any:
    """Resume a job waiting in `needs_admin` / `needs_tools` (admin only).

    This is the ONLY way an administrator prompt is ever shown: the job first
    stops in `needs_admin` with the reason and exact command, and nothing
    elevated runs until this call."""
    _require_admin_principal(request)
    policy = _engine_install_policy(request)
    if not bool(policy.get("value")):
        return _host_action_error(403, "refused", "engine installs are disabled on this gateway (allow_engine_install is off)", reason="not_allowed", install_policy=policy)
    body = req or EngineJobContinueRequest()
    try:
        return await asyncio.to_thread(ei.default_registry().continue_job, job_id, body.action)
    except ei.JobStateError as exc:
        return _job_error(exc)


@router.post("/engines/jobs/{job_id}/cancel")
async def engine_job_cancel(job_id: str, request: Request) -> Any:
    """Cancel an install job (admin only): stops its download or process tree."""
    _require_admin_principal(request)
    snap = await asyncio.to_thread(ei.default_registry().cancel, job_id)
    if snap is None:
        return _host_action_error(404, "not_found", f"no job {job_id}")
    return snap


@router.get("/engines/{engine_id}")
async def engine_get(engine_id: str, request: Request, probe: bool = Query(False)) -> Any:
    """One engine row; 404 for an unknown engine id."""
    payload = await _payload(request, probe)
    if not isinstance(payload, dict):
        return payload
    for row in payload.get("engines") or []:
        if row.get("id") == engine_id:
            row = dict(row)
            row["install_allowed"] = payload.get("install_allowed")
            return row
    return _host_action_error(404, "refused", f"unknown engine {engine_id!r}; known: {', '.join(ei.ENGINE_IDS)}", reason="unknown_engine")


@router.post("/engines/{engine_id}/install")
async def engine_install_start(engine_id: str, request: Request, req: Optional[EngineInstallRequest] = None) -> Any:
    """Install a local engine on the GATEWAY HOST as a job (admin only).

    User-level first; a step that needs tools or an administrator pauses the
    job in `needs_tools` / `needs_admin` instead of failing or elevating.
    `dry_run: true` returns the plan and never needs `allow_engine_install`."""
    _require_admin_principal(request)
    body = req or EngineInstallRequest()
    if body.location not in {"auto", "user", "system"}:
        return _host_action_error(400, "refused", "location must be auto, user or system", reason="bad_location")
    if engine_id not in ei.ENGINE_TEXT:
        return _host_action_error(404, "refused", f"unknown engine {engine_id!r}; known: {', '.join(ei.ENGINE_IDS)}", reason="unknown_engine")
    if body.dry_run:
        installer = await asyncio.to_thread(ei.default_installer)
        return await asyncio.to_thread(ei.dry_run_payload, installer, engine_id, location=body.location)
    policy = _engine_install_policy(request)
    if not bool(policy.get("value")):
        bind = policy.get("bind_host")
        return _host_action_error(
            403,
            "refused",
            "engine installs are disabled on this gateway (allow_engine_install is off"
            + (f"; bound to {bind}" if bind else "")
            + "). An admin can enable them with "
            "`POST /api/gateway/admin/runtime-config {\"allow_engine_install\": true}`; a dry run is always allowed.",
            reason="not_allowed",
            install_policy=policy,
        )
    try:
        snap, joined = await asyncio.to_thread(ei.default_registry().start, engine_id, force=body.force, location=body.location)
    except ei.JobStateError as exc:
        return _job_error(exc)
    snap = dict(snap)
    snap["ok"] = True
    snap["joined"] = joined
    return snap


async def _server_action(engine_id: str, request: Request, verb: str) -> Any:
    _require_admin_principal(request)
    if engine_id not in ei.SERVER_ENGINES:
        return _host_action_error(409, "refused", f"{engine_id} is not a server engine; it runs inside the gateway when a model uses it", reason="not_a_server")
    installer = ei.default_registry().service_installer()
    fn = {
        ("ollama", "start"): installer.start_ollama,
        ("ollama", "stop"): installer.stop_ollama,
        ("lmstudio", "start"): installer.start_lmstudio,
        ("lmstudio", "stop"): installer.stop_lmstudio,
    }[(engine_id, verb)]
    try:
        result: Dict[str, Any] = await asyncio.to_thread(fn)
    except ei.InstallFailed as exc:
        return _host_action_error(502, "failed", exc.message, reason=exc.code)
    return {"ok": True, "engine": engine_id, "action": verb, **result}


@router.post("/engines/{engine_id}/start")
async def engine_start(engine_id: str, request: Request) -> Any:
    """Start a local engine server (Ollama app / `lms server start`) and wait until it answers."""
    return await _server_action(engine_id, request, "start")


@router.post("/engines/{engine_id}/stop")
async def engine_stop(engine_id: str, request: Request) -> Any:
    """Stop a local engine server (quits the Ollama app / `lms server stop`)."""
    return await _server_action(engine_id, request, "stop")
