"""Browser apps routes: `/api/gateway/apps/*` and the sign-in handover.

Install, update, launch, stop and Node.js install are admin actions (they run
software on the gateway host); installs are also subject to the host's
`allow_engine_install` setting, which with no stored choice is on for a
loopback gateway AND for a caller on the gateway machine itself whatever the
bind (loopback peer or one of this host's own addresses, no proxy headers:
security/same_machine.py); a remote caller needs the explicit setting. Any
signed-in principal may list the apps and open a running one: opening mints a
browser session for THAT principal, never for someone else.

The handover route `/apps/handover/{code}` sits outside `/api/gateway` on
purpose: the browser reaches it by plain navigation (no bearer header), and
the one-time code minted by `POST /api/gateway/apps/{id}/open` is its only
credential. Cookies are scoped to a host, not to a port, so the gateway can
set the app's own `<app>_gateway_{url,session,csrf}` cookies on the host the
browser used and then redirect to the app on that same host: the app's server
finds a valid gateway session and the app opens already connected. The
gateway token never reaches the browser or the URL.

Apps started OUTSIDE the gateway (the dev stack, npx, a global install) are
listed too (`source: "external"`, `managed: false`, apps_manager
`detect_external_apps`) and open through the same handover: the app's server
reads the same cookies whoever started it. Stop answers 409
`started_outside_gateway` for them.

Install (mission LL): ONE job that installs the browser app AND, for an app
with a terminal version that has a prebuilt download for this computer (Code),
the terminal app too (the job's two `parts`); it starts nothing. The desktop
app (the Assistant, `kind: "desktop"`, apps_desktop.py) installs into the
gateway's own Python through the same route, and `/launch` opens it on the
gateway computer's screen for a caller at that computer only.

Terminal apps (mission Y): each app row carries `interfaces[]` (kind "web"
and, for Code, kind "tui"). `POST /{id}/install-tui` installs a prebuilt
terminal app (admin, same "allow engine install" rule). `POST /{id}/launch-tui`
(admin) opens it in a new terminal window ON THE GATEWAY MACHINE, only when
the request comes from that machine (loopback socket peer, loopback Host, no
proxy headers); from anywhere else it answers 409 `not_on_gateway_machine`
with the command to copy. The window's launcher holds a one-time code that
`POST /apps/tui-handover` (outside /api/gateway, loopback peers only) trades
for a loopback-only bearer token acting as the caller.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, Optional
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from ..apps_desktop import is_desktop_app
from ..apps_manager import (
    HANDOVER_TTL_S,
    AppsError,
    NotOnGatewayMachine,
    get_apps_manager,
    handover_path,
    spec_for,
    tui_command,
    tui_for,
)

router = APIRouter(prefix="/gateway/apps", tags=["apps"])
handover_router = APIRouter(tags=["apps"])


def _principal(request: Request):
    from .gateway import _principal_from_request

    return _principal_from_request(request)


def _admin(request: Request):
    from .gateway import _require_admin_principal

    return _require_admin_principal(request)


def _error(exc: AppsError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.payload())


class AppInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Optional[str] = Field(default=None, max_length=64, description="Exact npm version (default: latest).")
    launch: bool = Field(default=False, description="Start the app when the install succeeds (and keep it enabled). The console's Install does not: it only installs.")
    with_terminal: bool = Field(
        default=True,
        description="Also install the app's terminal version, in the same job, when it has one with a prebuilt download for this computer (Code). False: the browser app only.",
    )


class AppUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Optional[str] = Field(default=None, max_length=64)


class AppOpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    remember: bool = Field(default=True, description="Keep the app signed in for 30 days (else until the browser closes).")
    path: Optional[str] = Field(
        default=None,
        max_length=2048,
        description='Where inside the app to land, e.g. "/#new" (Entity\'s creation form). A path on the app\'s own origin only: "//host", a full address or a backslash is refused (400 invalid_app_path).',
    )


def _job_payload(job, created: bool) -> Dict[str, Any]:
    return {"ok": True, "job": job.to_dict(), "created": bool(created)}


class TuiHandoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=16, max_length=200)


_PROXY_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip")
# A desktop hand-over session is "remembered": the built-in remember lifetime
# of a console sign-in (30 days). No environment variable is read for it.
DESKTOP_SESSION_TTL_S = 30 * 24 * 60 * 60


def _peer_is_loopback(request: Request) -> bool:
    import ipaddress

    host = str(getattr(request.client, "host", "") or "") if request.client else ""
    if host.startswith("::ffff:"):
        host = host[len("::ffff:"):]
    try:
        return bool(ipaddress.ip_address(host).is_loopback)
    except ValueError:
        return False


def _caller_on_gateway_machine(request: Request) -> bool:
    """The request comes from a browser (or tray) on the gateway machine
    itself: loopback socket peer, loopback Host header, and no proxy in
    between (a reverse proxy on the same host would make a remote visitor
    look local by peer alone)."""
    if not _peer_is_loopback(request):
        return False
    if any(request.headers.get(h) for h in _PROXY_HEADERS):
        return False
    return _is_loopback_name(_request_hostname(request))


def _browser_gateway_url(request: Request) -> str:
    """The gateway's address as this caller reaches it (what a command to
    copy must name). Falls back to the manager's own URL without a Host."""
    host = str(request.headers.get("host") or "").strip()
    if not host:
        return get_apps_manager().resolve_gateway_url()
    scheme = "https" if str(request.headers.get("x-forwarded-proto") or request.url.scheme).lower() == "https" else "http"
    return f"{scheme}://{host}"


def _same_machine(request: Request) -> bool:
    """The install rule's "person at the gateway machine" (loopback OR this
    host's own address as peer, no proxy headers). Wider than
    `_caller_on_gateway_machine`, which also wants a loopback Host because a
    terminal launcher must reach the gateway on loopback."""
    from ..security.same_machine import request_is_from_this_machine

    return request_is_from_this_machine(request)


def _caller(request: Request, principal) -> Dict[str, Any]:
    return {
        "local": _caller_on_gateway_machine(request),
        "same_machine": _same_machine(request),
        "admin": bool(principal.is_admin()),
        "gateway_url": _browser_gateway_url(request),
    }


@router.get("")
def apps_overview(request: Request, latest: bool = True) -> Dict[str, Any]:
    """Node.js runtime + one row per app (installed, running, url, actions,
    interfaces). The terminal entries depend on WHO asks from WHERE."""
    principal = _principal(request)
    return get_apps_manager().overview(check_latest=bool(latest), caller=_caller(request, principal))


@router.post("/runtime/install")
def apps_runtime_install(request: Request):
    _admin(request)
    m = get_apps_manager()
    try:
        node = m.node_status(refresh=True)
        if node["available"]:
            return {"ok": True, "job": None, "created": False, "message": node["message"], "runtime": {"node": node}}
        job, created = m.start_node_install(same_machine=_same_machine(request))
    except AppsError as exc:
        return _error(exc)
    return _job_payload(job, created)


@router.get("/jobs")
def apps_jobs(request: Request) -> Dict[str, Any]:
    _principal(request)
    return {"ok": True, "jobs": [j.to_dict() for j in reversed(get_apps_manager().jobs.list())]}


@router.get("/jobs/{job_id}")
def apps_job(request: Request, job_id: str):
    _principal(request)
    job = get_apps_manager().jobs.get(job_id)
    if job is None:
        return JSONResponse(status_code=404, content={"ok": False, "reason": "unknown_job", "message": f"No apps job '{job_id}' (jobs live until the gateway restarts)."})
    return {"ok": True, "job": job.to_dict()}


@router.post("/jobs/{job_id}/cancel")
def apps_job_cancel(request: Request, job_id: str):
    _admin(request)
    job = get_apps_manager().jobs.get(job_id)
    if job is None:
        return JSONResponse(status_code=404, content={"ok": False, "reason": "unknown_job", "message": f"No apps job '{job_id}'."})
    job.cancel_event.set()
    return {"ok": True, "job": job.to_dict()}


@router.post("/{app_id}/install")
def apps_install(request: Request, app_id: str, payload: Optional[AppInstallRequest] = None):
    _admin(request)
    body = payload or AppInstallRequest()
    try:
        job, created = get_apps_manager().start_install(app_id, version=body.version, launch=body.launch, same_machine=_same_machine(request), with_terminal=body.with_terminal)
    except AppsError as exc:
        return _error(exc)
    return _job_payload(job, created)


@router.post("/{app_id}/update")
def apps_update(request: Request, app_id: str, payload: Optional[AppUpdateRequest] = None):
    _admin(request)
    body = payload or AppUpdateRequest()
    try:
        job, created = get_apps_manager().start_install(app_id, version=body.version, update=True, same_machine=_same_machine(request))
    except AppsError as exc:
        return _error(exc)
    return _job_payload(job, created)


@router.post("/{app_id}/install-tui")
def apps_install_tui(request: Request, app_id: str):
    """Install (or update) an app's prebuilt terminal app: a job like the
    browser installs. 409 `toolchain_required` + `command` when only a source
    build exists for this computer."""
    _admin(request)
    try:
        job, created = get_apps_manager().start_tui_install(app_id, same_machine=_same_machine(request))
    except AppsError as exc:
        return _error(exc)
    return _job_payload(job, created)


@router.post("/{app_id}/launch-tui")
async def apps_launch_tui(request: Request, app_id: str):
    """Open the app's terminal version in a new terminal window on the
    gateway machine, signed in as the caller. Refused (409, with the command
    to copy) unless the request comes from the gateway machine itself."""
    principal = _admin(request)
    m = get_apps_manager()
    try:
        tui = tui_for(app_id)
    except AppsError as exc:
        return _error(exc)
    gateway_url = _browser_gateway_url(request)
    if not _caller_on_gateway_machine(request):
        return _error(
            NotOnGatewayMachine(
                f"{tui.name} can only open in a terminal on the gateway's own screen, and this browser is on another computer.",
                hint=f"Copy the command and run it where {tui.name}'s terminal app is installed; sign in there once with `{tui.binary} login`.",
                extra={"command": tui_command(tui, gateway_url), "signin_command": f"{tui.binary} login {tui.gateway_flag} {gateway_url} --token <your token>"},
            )
        )
    try:
        return await asyncio.to_thread(m.launch_tui, app_id, principal=principal, gateway_url=gateway_url)
    except AppsError as exc:
        return _error(exc)


@router.post("/{app_id}/tui-command")
def apps_tui_command(request: Request, app_id: str):
    """Terminal parity for launch-tui (`abstractgateway apps tui-command`):
    the same one-use, self-deleting launcher with a single-use handover code,
    but no window is opened; the caller runs `signin_command` in a terminal on
    this machine. Same machine-only rule as launch-tui."""
    principal = _admin(request)
    try:
        tui = tui_for(app_id)
    except AppsError as exc:
        return _error(exc)
    gateway_url = _browser_gateway_url(request)
    if not _caller_on_gateway_machine(request):
        return _error(
            NotOnGatewayMachine(
                f"A one-time sign-in for {tui.name}'s terminal app can only be made for the gateway machine itself.",
                hint=f"On another computer run the command below and sign in there once with `{tui.binary} login`.",
                extra={"command": tui_command(tui, gateway_url), "signin_command": f"{tui.binary} login {tui.gateway_flag} {gateway_url} --token <your token>"},
            )
        )
    try:
        return get_apps_manager().tui_signin_command(app_id, principal=principal, gateway_url=gateway_url)
    except AppsError as exc:
        return _error(exc)


@router.post("/{app_id}/launch")
async def apps_launch(request: Request, app_id: str):
    """Start a browser app; for a desktop app (the Assistant), open it on the
    gateway computer's screen — only for a caller at that computer (409
    `not_on_gateway_machine` otherwise), never twice."""
    _admin(request)
    m = get_apps_manager()
    if is_desktop_app(app_id):
        try:
            return await asyncio.to_thread(
                m.launch_desktop, str(app_id).strip().lower(), same_machine=_same_machine(request),
                principal=_principal(request),
            )
        except AppsError as exc:
            return _error(exc)
    try:
        row = await asyncio.to_thread(m.launch, app_id)
    except AppsError as exc:
        return _error(exc)
    return {"ok": True, "app": row}


@router.post("/{app_id}/stop")
async def apps_stop(request: Request, app_id: str):
    _admin(request)
    m = get_apps_manager()
    try:
        row = await asyncio.to_thread(m.stop, app_id)
    except AppsError as exc:
        return _error(exc)
    return {"ok": True, "app": row}


@router.get("/{app_id}/logs")
def apps_logs(request: Request, app_id: str, tail: int = 200):
    _admin(request)
    m = get_apps_manager()
    try:
        spec = spec_for(app_id)
    except AppsError as exc:
        return _error(exc)
    return {"ok": True, "app_id": spec.id, "path": str(m.app_log_path(spec.id)), "lines": m.app_log_tail(spec.id, max(1, min(5000, int(tail))))}


def _request_hostname(request: Request) -> str:
    host = str(request.headers.get("host") or "").strip()
    if not host:
        return "127.0.0.1"
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _is_loopback_name(host: str) -> bool:
    h = host.strip("[]").lower()
    if h in {"localhost", "::1"} or h.endswith(".localhost"):
        return True
    return h.startswith("127.")


@router.post("/{app_id}/open")
def apps_open(request: Request, app_id: str, payload: Optional[AppOpenRequest] = None):
    """Mint a one-time sign-in link for a RUNNING app. The console opens
    `open_url` (relative to this gateway) in a new tab; the browser lands in
    the app already signed in as the caller, at `path` inside the app when
    one is given (same-app paths only: `handover_path`)."""
    principal = _principal(request)
    m = get_apps_manager()
    if is_desktop_app(app_id):
        return JSONResponse(
            status_code=409,
            content={"ok": False, "reason": "desktop_app", "message": "The Assistant is a desktop app, not a browser app: it opens on the gateway's computer.", "hint": f"POST /api/gateway/apps/{str(app_id).strip().lower()}/launch opens it there."},
        )
    try:
        spec = spec_for(app_id)
        handover_path(None if payload is None else payload.path)  # 400 before anything else
        row = m.app_row(spec)
    except AppsError as exc:
        return _error(exc)
    if not row["running"]:
        return JSONResponse(
            status_code=409,
            content={"ok": False, "reason": "not_running", "message": f"{spec.name} is not running.", "hint": "Start it first (Launch)."},
        )
    host = _request_hostname(request)
    if row.get("source") == "external":
        # Started outside the gateway: where it listens is its own business.
        # From a browser on another address, it must answer on that address.
        if not _is_loopback_name(host) and not _port_answers(host, int(row["port"])):
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "reason": "app_loopback_only",
                    "message": f"{spec.name} was started outside the gateway and listens on this machine only (port {row['port']}); a browser on another machine cannot reach it.",
                    "hint": "Open it from the gateway machine, or restart it so it listens on all addresses, behind your own access control.",
                },
            )
    elif _is_loopback_name(m.bind_host) and not _is_loopback_name(host):
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "reason": "app_loopback_only",
                "message": f"{spec.name} listens on this gateway machine only ({m.bind_host}:{row['port']}); a browser on another machine cannot reach it.",
                "hint": "Open it from the gateway machine, or set Where apps listen to 0.0.0.0 (behind your own access control: Apps settings, or `abstractgateway apps config set host 0.0.0.0`) and relaunch the app.",
            },
        )
    try:
        code = m.mint_handover(spec.id, principal, host=host, path=None if payload is None else payload.path)
    except AppsError as exc:
        return _error(exc)
    remember = True if payload is None else bool(payload.remember)
    return {
        "ok": True,
        "app_id": spec.id,
        "open_url": f"/apps/handover/{code}" + ("" if remember else "?remember=0"),
        "app_url": f"{request.url.scheme}://{host}:{row['port']}/",
        "expires_in_s": int(HANDOVER_TTL_S),
    }


def _port_answers(host: str, port: int, timeout: float = 0.5) -> bool:
    """The app answers on host:port, where host is the name the browser used
    for this gateway. Only ever tried against THIS machine's own addresses
    (the Host header is the caller's to write: never a connect elsewhere)."""
    import socket

    from ..security.same_machine import own_addresses

    name = host.strip("[]")
    try:
        infos = socket.getaddrinfo(name, int(port), type=socket.SOCK_STREAM)
    except OSError:
        return False
    own = own_addresses()
    addrs = [info[4][0].split("%", 1)[0] for info in infos]
    if not addrs or any(a not in own for a in addrs):
        return False
    try:
        with socket.create_connection((addrs[0], int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _handover_error(status: int, title: str, message: str) -> HTMLResponse:
    from html import escape

    body = (
        "<!doctype html><meta charset=utf-8><title>" + escape(title) + "</title>"
        "<body style=\"font:15px/1.5 -apple-system,system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem\">"
        f"<h1 style=\"font-size:1.3rem\">{escape(title)}</h1><p>{escape(message)}</p>"
        "<p><a href=\"/console\">Back to the console</a></p></body>"
    )
    return HTMLResponse(body, status_code=status, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@handover_router.get("/apps/handover/{code}", include_in_schema=False)
def apps_handover(request: Request, code: str, remember: str = "1"):
    """Trade a one-time code for the app's sign-in cookies, then redirect."""
    from ..security.sessions import GatewaySessionStore, gateway_session_ttl_s

    m = get_apps_manager()
    rec = m.redeem_handover_target(code)
    if rec is None:
        return _handover_error(410, "This app link has expired", "App links work once and for two minutes. Open the app again from the console.")
    app_id, principal, host, app_path = rec
    if _request_hostname(request) != host:
        return _handover_error(400, "This app link belongs to another address", f"Open it on {host}, where it was created.")
    spec = spec_for(app_id)
    row = m.app_row(spec)
    if not row["running"] or not row["port"]:
        return _handover_error(409, f"{spec.name} is not running", "Start it from the console, then open it again.")
    persist = str(remember).strip() not in {"0", "false", "no"}
    ttl = gateway_session_ttl_s()
    if persist:
        try:
            ttl = int(os.getenv("ABSTRACTGATEWAY_REMEMBER_SESSION_TTL_S") or (30 * 24 * 60 * 60))
        except ValueError:
            ttl = 30 * 24 * 60 * 60
        ttl = max(60, min(90 * 24 * 60 * 60, ttl))
    try:
        session_value, csrf_token, _record = GatewaySessionStore().create_session(principal, ttl_s=ttl)
    except Exception as exc:  # noqa: BLE001
        return _handover_error(403, "Sign-in could not be handed to the app", f"{type(exc).__name__}: {exc}")
    # An app the gateway started got its gateway URL at launch (app state);
    # one started outside talks to this gateway at the gateway's own URL.
    gateway_url = m.resolve_gateway_url() if row.get("source") == "external" else str(m.app_state(spec.id).get("gateway_url") or m.resolve_gateway_url())
    scheme = "https" if str(request.headers.get("x-forwarded-proto") or request.url.scheme).lower() == "https" else "http"
    # The path was validated when the code was minted (apps_manager
    # `handover_path`) and is bound to the code; checked again here so no
    # stored value can ever point the browser off the app's origin.
    try:
        app_path = handover_path(app_path)
    except AppsError as exc:
        return _handover_error(400, "This app link is not valid", exc.message)
    target = f"{scheme}://{host}:{row['port']}{app_path}"
    resp = RedirectResponse(url=target, status_code=303)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    common: Dict[str, Any] = {"path": "/", "samesite": "lax", "secure": scheme == "https"}
    if persist:
        common["max_age"] = ttl
    prefix = spec.cookie_prefix
    # URL-encoded like the app servers write it (they decodeURIComponent every
    # cookie); a raw URL would be sent double-quoted (RFC 6265 quoting of "/").
    resp.set_cookie(f"{prefix}_gateway_url", quote(gateway_url, safe=""), httponly=True, **common)
    resp.set_cookie(f"{prefix}_gateway_session", session_value, httponly=True, **common)
    resp.set_cookie(f"{prefix}_gateway_csrf", csrf_token, httponly=False, **common)
    return resp


def _durable_session_principal(principal: Any) -> Any:
    """A static-token operator principal whose token is not one of the
    gateway's configured tokens (the tray's per-process loopback token) is
    the same operator: its session is bound to the configured operator token,
    so it stays valid after the tray or the gateway restarts. Other
    principals are unchanged."""
    if getattr(principal, "source", "") != "legacy-token":
        return principal
    from dataclasses import replace

    from ..security.gateway_security import load_gateway_auth_policy_from_env
    from ..security.sessions import legacy_token_fingerprints

    fps = legacy_token_fingerprints(tuple(load_gateway_auth_policy_from_env().tokens or ()))
    if not fps or str(getattr(principal, "token_fingerprint", "") or "") in fps:
        return principal
    return replace(principal, token_fingerprint=fps[0])


class DesktopHandoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str


@router.post("/desktop-handover")
def apps_desktop_handover(request: Request, payload: DesktopHandoverRequest):
    """Trade the desktop Assistant's one-time code for a remembered gateway
    session of the person who opened it (CONTRACTS A1 / A-3).

    Public (the Assistant has no credentials yet) but answered ONLY for a
    direct loopback caller: no proxy header and no app-server session header
    (403 otherwise). The code is single use and lives two minutes (410).
    200 -> {base_url, session_id, csrf_token, user_id, expires_at}."""
    from ..security.sessions import GatewaySessionStore, gateway_session_header_name

    headers = {"Cache-Control": "no-store"}
    relayed = any(request.headers.get(h) for h in _PROXY_HEADERS) or bool(request.headers.get(gateway_session_header_name()))
    if not _peer_is_loopback(request) or relayed:
        return JSONResponse(
            status_code=403,
            headers=headers,
            content={"ok": False, "reason": "loopback_only", "message": "Desktop sign-in codes work only for an app on the gateway machine itself."},
        )
    m = get_apps_manager()
    rec = m.redeem_desktop_handover(payload.code)
    if rec is None:
        return JSONResponse(
            status_code=410,
            headers=headers,
            content={"ok": False, "reason": "handover_expired", "message": "This sign-in code has expired or was already used.",
                     "hint": "Quit the Assistant and open it again from the gateway console or its menu bar icon."},
        )
    principal, base_url = rec
    try:
        principal = _durable_session_principal(principal)
        session_value, csrf_token, record = GatewaySessionStore().create_session(principal, ttl_s=DESKTOP_SESSION_TTL_S)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=403, headers=headers, content={"ok": False, "reason": "session_refused", "message": f"{type(exc).__name__}: {exc}"})
    return JSONResponse(
        headers=headers,
        content={
            "base_url": base_url,
            "session_id": session_value,
            "csrf_token": csrf_token,
            "user_id": getattr(principal, "user_id", None),
            "expires_at": getattr(record, "expires_at", None),
        },
    )


@handover_router.post("/apps/tui-handover", include_in_schema=False)
def apps_tui_handover(request: Request, payload: TuiHandoverRequest):
    """Trade a terminal launcher's one-time code for a terminal sign-in.

    Loopback socket peers only (no proxy headers): the launcher runs on this
    machine. The token it returns is accepted from loopback only, acts as the
    principal who clicked "Open in Terminal", and lives in this gateway
    process's memory only (security/gateway_security.py ephemeral tokens)."""
    headers = {"Cache-Control": "no-store"}
    if not _peer_is_loopback(request) or any(request.headers.get(h) for h in _PROXY_HEADERS):
        return JSONResponse(
            status_code=403,
            headers=headers,
            content={"ok": False, "reason": "loopback_only", "message": "Terminal sign-in codes work only on the gateway machine itself."},
        )
    m = get_apps_manager()
    rec = m.redeem_tui_handover(payload.code)
    if rec is None:
        return JSONResponse(
            status_code=410,
            headers=headers,
            content={"ok": False, "reason": "handover_expired", "message": "This sign-in code has expired or was already used.", "hint": "Open the app again from the gateway console (codes work once, for two minutes)."},
        )
    app_id, principal, gateway_url = rec
    tui = tui_for(app_id)
    token = m.issue_tui_token(app_id, principal)
    return JSONResponse(
        headers=headers,
        content={
            "ok": True,
            "app_id": app_id,
            "token": token,
            "token_env": tui.token_env,
            "url_env": tui.url_env or None,
            "gateway_url": gateway_url,
            "gateway_flag": tui.gateway_flag,
            "user": getattr(principal, "user_id", None),
        },
    )
