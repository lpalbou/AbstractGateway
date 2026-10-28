"""First-run onboarding and password sign-in (0.18.0).

    GET  /api/gateway/session/onboarding             public: is this gateway unclaimed? needs the setup code?
    POST /api/gateway/session/onboarding             public: set the admin password, sign in (claims the gateway)
    PUT  /api/gateway/me/password                    any signed-in person: set or change my password
    PUT  /api/gateway/admin/accounts/{id}/password   admin: set another account's password
    DELETE /api/gateway/admin/accounts/{id}/password admin: remove it (token sign-in only)

`POST /api/gateway/session/login` (routes/gateway.py) takes `{user_id, password}` as well as
`{user_id, token}`; its password branch is `password_sign_in` here.

The unclaimed window (first_run.py "Onboarding") is the risk this module guards: the form is
answered only for a browser request (an Origin header the gateway accepts; the security
middleware refuses any other Origin before this code runs), it is rate limited per address and
for the whole gateway (passwords.ONBOARDING_LIMITER), and unless the request comes straight
from this machine (a loopback socket peer, no proxy forwarding headers) it needs the six-word
setup code the local CLI prints. A claim is logged with the client address (warning log line,
the audit line's `onboarding` detail, and `claimed_from` in `<data>/auth/onboarding.json`).
"""

from __future__ import annotations

import ipaddress
import logging
import threading
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ..passwords import (
    MIN_LENGTH,
    ONBOARDING_LIMITER,
    PASSWORD_LIMITER,
    PASSWORD_RULE,
    burn_equivalent_time,
    hash_password,
    limit_message,
    password_problem,
    verify_password,
)
from ..users import GatewayUserRegistry, gateway_data_dir_from_env, gateway_user_auth_enabled
from ..work_pools import off_loop

logger = logging.getLogger("abstractgateway.onboarding")

router = APIRouter(prefix="/gateway", tags=["sign-in"])

# A request carrying any of these came through a proxy (tailscale serve/funnel, nginx, ...):
# its socket peer is the proxy, so "loopback" says nothing about who is asking.
FORWARDING_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip", "forwarded")

_CLAIM_LOCK = threading.Lock()


def _peer(request: Request) -> str:
    return str(getattr(getattr(request, "client", None), "host", "") or "")


def _is_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(str(ip).strip("[]")).is_loopback
    except ValueError:
        return str(ip).strip().lower() == "localhost"


def client_address(request: Request) -> str:
    """The client's address for limits and logs: X-Forwarded-For from a proxy on this machine
    (or from any peer when proxies are trusted), else the socket peer -- the security
    middleware's rule (gateway_security._client_ip)."""
    peer = _peer(request) or "unknown"
    xff = str(request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if xff:
        trusted = _is_loopback(peer)
        if not trusted:
            try:
                from ..network_exposure import live_reverse_proxy

                trusted = bool(live_reverse_proxy().trust_proxy)
            except Exception:  # noqa: BLE001
                trusted = False
        if trusted:
            return xff
    return peer


def setup_code_requirement(request: Request) -> Optional[str]:
    """None when the request comes straight from this machine (loopback socket peer, no
    forwarding headers); otherwise why the setup code is needed: `proxied` | `remote`.

    Deliberately stricter than "network mode internet or proxy trust on": a fresh gateway behind
    a container's port mapping or on a shared LAN looks private to itself and is still reachable
    by strangers. The person who installed it has the code (installer summary, `abstractgateway
    claim --code`)."""
    if any(request.headers.get(h) for h in FORWARDING_HEADERS):
        return "proxied"
    if not _is_loopback(_peer(request)):
        return "remote"
    return None


_REQUIREMENT_TEXT = {
    "proxied": "This page reached the gateway through a proxy, so the gateway can't tell who is asking.",
    "remote": "This browser is not on the gateway's own machine.",
}


def _refuse(status: int, reason_code: str, message: str, **extra: Any) -> HTTPException:
    detail: Dict[str, Any] = {"reason_code": reason_code, "message": message}
    detail.update(extra)
    headers = {"Retry-After": str(int(extra["retry_after_s"]))} if "retry_after_s" in extra else None
    return HTTPException(status_code=status, detail=detail, headers=headers)


def _audit(request: Request, key: str, value: Dict[str, Any]) -> None:
    try:
        detail = getattr(request.state, "audit_detail", None)
        detail = dict(detail) if isinstance(detail, dict) else {}
        detail[key] = value
        request.state.audit_detail = detail
    except Exception:  # noqa: BLE001 - auditing never breaks a request
        pass


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------


@router.get(
    "/session/onboarding",
    summary="Is this gateway waiting for its first admin password?",
    description="Public. `{unclaimed, setup_code_required, setup_code_reason, account, password_rule, "
    "password_min_length}`. `unclaimed` true = a fresh data dir whose admin has no password and no one has "
    "signed in as admin yet: the console shows \"Set up your gateway\". `setup_code_required` true = the form "
    "also needs the six-word setup code (`abstractgateway claim --code` on the gateway's machine), because this "
    "request did not come straight from that machine. `account` is the admin account id while unclaimed, else null.",
)
async def session_onboarding_status(request: Request) -> Dict[str, Any]:
    data_dir = gateway_data_dir_from_env()
    from ..first_run import onboarding_unclaimed

    rec = await off_loop("light", onboarding_unclaimed, data_dir) if gateway_user_auth_enabled() else None
    reason = setup_code_requirement(request)
    return {
        "unclaimed": rec is not None,
        "setup_code_required": bool(rec is not None and reason is not None),
        "setup_code_reason": _REQUIREMENT_TEXT.get(reason or "") if rec is not None else None,
        "account": str(rec.get("user_id") or "admin") if rec is not None else None,
        "password_rule": PASSWORD_RULE,
        "password_min_length": MIN_LENGTH,
        "password_sign_in": bool(gateway_user_auth_enabled()),
    }


class OnboardingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: Optional[str] = Field(default=None, max_length=200, description="The admin's display name (default: the account id).")
    password: str = Field(..., min_length=1, max_length=1024)
    password_confirm: str = Field(..., min_length=1, max_length=1024)
    setup_code: Optional[str] = Field(default=None, max_length=400, description="The six-word setup code, when required.")
    remember: bool = False


def _claim_unlocked(data_dir: Any, *, display_name: Optional[str], password: str, client_ip: str) -> Any:
    from ..first_run import close_onboarding, onboarding_unclaimed

    with _CLAIM_LOCK:
        rec = onboarding_unclaimed(data_dir)
        if rec is None:
            return None
        registry = GatewayUserRegistry()
        user = registry.set_password(
            user_id=str(rec.get("user_id") or "admin"),
            tenant_id=str(rec.get("tenant_id") or "default"),
            password_hash=hash_password(password),
            display_name=display_name if display_name and display_name.strip() else None,
        )
        close_onboarding(data_dir, via="onboarding", client_ip=client_ip, user_id=user.user_id, tenant_id=user.tenant_id)
        return user


@router.post(
    "/session/onboarding",
    summary="Set up a fresh gateway: choose the admin password and sign in",
    description="Public, browser only (an accepted Origin header), while `GET /session/onboarding` says "
    "`unclaimed`. Body `{display_name?, password, password_confirm, setup_code?, remember}`. Sets the admin's "
    "password, signs this browser in as admin (the same session as `/session/login`), and closes the "
    "onboarding for good (claim links pending then are deleted). Refusals: 409 `already_claimed`; 400 "
    "`password_mismatch` / `password_too_weak`; 403 `setup_code_required` / `setup_code_refused`; 429 "
    "`too_many_attempts` (5 refused setup codes per address, 20 per gateway, per 15 minutes) with `retry_after_s`.",
)
async def session_onboarding_claim(request: Request, response: Response, payload: OnboardingRequest) -> Dict[str, Any]:
    from ..first_run import first_run_state, onboarding_unclaimed, setup_code_matches
    from .gateway import _issue_gateway_browser_session

    if not gateway_user_auth_enabled():
        raise _refuse(409, "onboarding_requires_user_auth", "This gateway runs without user accounts, so it has no admin password to set.")
    if not str(request.headers.get("origin") or "").strip():
        raise _refuse(403, "onboarding_browser_only", "Set up the gateway from its console page in a browser.")
    ip = client_address(request)
    locked = ONBOARDING_LIMITER.check(ip=ip)
    if locked:
        raise _refuse(429, "too_many_attempts", limit_message(*locked), retry_after_s=locked[1])
    data_dir = gateway_data_dir_from_env()
    rec = await off_loop("light", onboarding_unclaimed, data_dir)
    if rec is None:
        raise _refuse(409, "already_claimed", "This gateway is already set up. Sign in with your account and password.")
    account = str(rec.get("user_id") or "admin")
    if payload.password != payload.password_confirm:
        raise _refuse(400, "password_mismatch", "The two passwords are different.", field="password_confirm")
    problem = password_problem(payload.password, account=account)
    if problem:
        raise _refuse(400, "password_too_weak", problem, field="password")
    reason = setup_code_requirement(request)
    if reason is not None:
        if not str(payload.setup_code or "").strip():
            raise _refuse(
                403, "setup_code_required",
                f"{_REQUIREMENT_TEXT[reason]} Enter the setup code: run `abstractgateway claim --code` on the gateway's machine.",
                field="setup_code",
            )
        if not await off_loop("light", setup_code_matches, data_dir, payload.setup_code):
            ONBOARDING_LIMITER.failure(ip=ip)
            logger.warning("onboarding: wrong setup code from %s", ip)
            _audit(request, "onboarding", {"refused": "setup_code", "client_ip": ip})
            raise _refuse(403, "setup_code_refused", "That setup code is not right. Check the six words and try again.", field="setup_code")
    user = await off_loop(
        "light", _claim_unlocked, data_dir, display_name=payload.display_name, password=payload.password, client_ip=ip
    )
    if user is None:
        raise _refuse(409, "already_claimed", "This gateway was just set up from another browser. Sign in instead.")
    ONBOARDING_LIMITER.success(ip=ip)
    logger.warning("gateway claimed: %s/%s set the admin password through the console onboarding from %s", user.tenant_id, user.user_id, ip)
    _audit(request, "onboarding", {"claimed": True, "account": f"{user.tenant_id}/{user.user_id}", "client_ip": ip,
                                   "setup_code": reason is not None})
    out = _issue_gateway_browser_session(request, response, user.to_principal(), remember=bool(payload.remember), via="onboarding")
    out["claimed"] = True
    out["first_run"] = first_run_state(data_dir)
    return out


# ---------------------------------------------------------------------------
# Password sign-in (the password branch of POST /session/login)
# ---------------------------------------------------------------------------

PASSWORD_REFUSED = "That account and password don't match."


def _check_password(request: Request, *, account_key: str, password: str, password_hash: str) -> None:
    """Verify under the attempt limits; raises 429 / 401. A success clears the counters."""
    ip = client_address(request)
    locked = PASSWORD_LIMITER.check(ip=ip, account=account_key)
    if locked:
        raise _refuse(429, "too_many_attempts", limit_message(*locked), retry_after_s=locked[1])
    ok = verify_password(password, password_hash) if password_hash else (burn_equivalent_time(password) or False)
    if not ok:
        PASSWORD_LIMITER.failure(ip=ip, account=account_key)
        raise _refuse(401, "password_refused", PASSWORD_REFUSED)
    PASSWORD_LIMITER.success(ip=ip, account=account_key)


def password_sign_in_principal(request: Request, *, account: str, password: str, tenant_id: str = "default") -> Any:
    """The principal `{account, password}` names, or an HTTPException (401 for every unknown /
    wrong / unusable case alike, 429 when locked). Runs in a worker thread (scrypt)."""
    registry = GatewayUserRegistry()
    rec = registry.find_sign_in_account(account, tenant_id=tenant_id)
    key = f"{tenant_id}:{rec.user_id if rec is not None else str(account or '').strip().lower()}"
    usable = rec is not None and rec.enabled and not rec.archived and bool(rec.password_hash)
    _check_password(request, account_key=key, password=password, password_hash=rec.password_hash if usable else "")
    return rec.to_principal()


# ---------------------------------------------------------------------------
# Setting passwords
# ---------------------------------------------------------------------------


class MyPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: Optional[str] = Field(default=None, max_length=1024, description="Required when the account already has a password.")
    new_password: str = Field(..., min_length=1, max_length=1024)
    new_password_confirm: Optional[str] = Field(default=None, max_length=1024)


@router.put(
    "/me/password",
    tags=["accounts"],
    summary="Set or change my password",
    description="Any signed-in person account. Body `{current_password?, new_password, new_password_confirm?}`; "
    "`current_password` is required once the account has a password (wrong: 401 `password_refused`, under the "
    "sign-in attempt limits). Every other browser session of the account ends; signed in from a browser, this "
    "one gets a fresh session, so you stay signed in. Entities have no password (404).",
)
async def me_set_password(request: Request, response: Response, payload: MyPasswordRequest) -> Dict[str, Any]:
    from .gateway import _issue_gateway_browser_session, _principal_from_request
    from ..security.sessions import gateway_session_id_from_cookie_header

    principal = _principal_from_request(request)
    registry = GatewayUserRegistry()
    rec = registry.get_user(str(principal.user_id), tenant_id=str(principal.tenant_id or "default"))
    if rec is None or rec.principal_kind == "entity" or principal.source != "user-registry":
        raise _refuse(404, "no_own_password", "This account has no password of its own: it signs in with the gateway's admin token.")
    if payload.new_password_confirm is not None and payload.new_password_confirm != payload.new_password:
        raise _refuse(400, "password_mismatch", "The two passwords are different.", field="new_password_confirm")
    problem = password_problem(payload.new_password, account=rec.user_id)
    if problem:
        raise _refuse(400, "password_too_weak", problem, field="new_password")
    if rec.password_hash:
        if not payload.current_password:
            raise _refuse(400, "current_password_required", "Enter your current password.", field="current_password")
        await off_loop("light", lambda: _check_password(
            request, account_key=f"{rec.tenant_id}:{rec.user_id}", password=str(payload.current_password), password_hash=rec.password_hash))
    updated = await off_loop("light", lambda: registry.set_password(
        user_id=rec.user_id, tenant_id=rec.tenant_id, password_hash=hash_password(payload.new_password)))
    _audit(request, "password", {"account": f"{rec.tenant_id}/{rec.user_id}", "changed": bool(rec.password_hash), "set": True})
    out: Dict[str, Any] = {"ok": True, "user": updated.public_dict()}
    if gateway_session_id_from_cookie_header(request.headers.get("cookie")):
        out["session"] = _issue_gateway_browser_session(request, response, updated.to_principal(), remember=False, via="password_change")
    else:
        _close_onboarding_for(updated, request, via="password_set")
    return out


class AdminPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(..., min_length=1, max_length=1024)


def _admin_target(request: Request, account_id: str, tenant_id: str) -> Any:
    from .gateway import _require_admin_principal

    _require_admin_principal(request)
    rec = GatewayUserRegistry().get_user(account_id, tenant_id=tenant_id)
    if rec is None:
        raise _refuse(404, "account_not_found", f"There is no account named {account_id!r} on this gateway.")
    if rec.principal_kind == "entity":
        raise _refuse(409, "entity_no_password", f"{rec.user_id} is an entity: entities sign in through their door, never with a password.")
    if rec.archived:
        raise _refuse(409, "account_archived", f"{rec.user_id} is archived: unarchive the account first.")
    return rec


def _close_onboarding_for(user: Any, request: Request, *, via: str) -> None:
    if "admin" not in {str(r).strip().lower() for r in user.roles}:
        return
    try:
        from ..first_run import close_onboarding

        if close_onboarding(gateway_data_dir_from_env(), via=via, client_ip=client_address(request),
                            user_id=user.user_id, tenant_id=user.tenant_id):
            logger.warning("gateway claimed: %s/%s (%s) from %s", user.tenant_id, user.user_id, via, client_address(request))
    except Exception:  # noqa: BLE001
        logger.exception("could not close the onboarding window")


@router.put(
    "/admin/accounts/{account_id}/password",
    tags=["accounts"],
    summary="Set an account's password",
    description="Admin only. Body `{password}` (the strength rule of `GET /session/onboarding` "
    "`password_rule`). The account then signs in with its account id and this password; its token keeps "
    "working for apps and automation. Every browser session of the account ends. Entities (409 "
    "`entity_no_password`) and archived accounts (409) have none.",
)
async def admin_set_password(
    request: Request, response: Response, account_id: str, payload: AdminPasswordRequest, tenant_id: str = "default"
) -> Dict[str, Any]:
    from .gateway import _issue_gateway_browser_session, _principal_from_request, _same_account

    rec = _admin_target(request, account_id, tenant_id)
    problem = password_problem(payload.password, account=rec.user_id)
    if problem:
        raise _refuse(400, "password_too_weak", problem, field="password")
    updated = await off_loop("light", lambda: GatewayUserRegistry().set_password(
        user_id=rec.user_id, tenant_id=rec.tenant_id, password_hash=hash_password(payload.password)))
    _audit(request, "password", {"account": f"{rec.tenant_id}/{rec.user_id}", "set": True, "by_admin": True})
    out: Dict[str, Any] = {"ok": True, "user": updated.public_dict()}
    admin = _principal_from_request(request)
    from ..security.sessions import gateway_session_id_from_cookie_header

    if _same_account(admin, user_id=rec.user_id, tenant_id=rec.tenant_id) and gateway_session_id_from_cookie_header(request.headers.get("cookie")):
        out["session"] = _issue_gateway_browser_session(request, response, updated.to_principal(), remember=False, via="password_change")
    else:
        _close_onboarding_for(updated, request, via="password_set")
    return out


@router.delete(
    "/admin/accounts/{account_id}/password",
    tags=["accounts"],
    summary="Remove an account's password (token sign-in only)",
    description="Admin only. The account signs in with its token again; its browser sessions end.",
)
async def admin_clear_password(request: Request, account_id: str, tenant_id: str = "default") -> Dict[str, Any]:
    rec = _admin_target(request, account_id, tenant_id)
    updated = await off_loop("light", lambda: GatewayUserRegistry().set_password(
        user_id=rec.user_id, tenant_id=rec.tenant_id, password_hash=""))
    _audit(request, "password", {"account": f"{rec.tenant_id}/{rec.user_id}", "removed": True})
    return {"ok": True, "user": updated.public_dict()}
