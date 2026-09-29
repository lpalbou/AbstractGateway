"""Per-user email over HTTP (framework backlog 0992 WP3), under `/api/gateway`.

Self routes — every signed-in human, on THEIR OWN plane (resolved from the authenticated
principal, never from a path or body id; entities are refused):

    GET    /me/email                     settings + status (never a secret)
    PUT    /me/email                     connect: test, then store (password in the body, never echoed)
    POST   /me/email/test                sign in to IMAP and SMTP with the stored account
    DELETE /me/email                     disconnect: credentials and cursor deleted
    PUT    /me/email/policy              {mode: allowlist|denylist, entries: [address | domain]}
    POST   /me/email/policy/check        {addresses} -> would they be allowed?
    PUT    /me/email/limits              {per_hour, per_day}
    PUT    /me/email/enabled             {enabled}   the user's own switch
    GET    /me/email/oauth/clients       which providers have a gateway OAuth client (no secrets)
    POST   /me/email/oauth/start         begin an OAuth2 sign-in (device code or loopback browser)
    POST   /me/email/oauth/poll          {flow_id}          -> pending | connected
    POST   /me/email/oauth/finish        {flow_id, wait_s}  wait up to 60 s for the approval
    POST   /me/email/oauth/cancel        {flow_id}
    GET    /me/notifications             events, channel availability, outbox summary
    PUT    /me/notifications             {email: {event: bool}}
    POST   /me/notifications/test        send one test notification now

Admin routes — status and the per-user switch only (D3: administrators never read mail):

    GET    /admin/users/{user_id}/email          configured / address / state / last error
    PUT    /admin/users/{user_id}/email          {enabled}   off = no watcher, no sending, no notifications
    GET    /admin/email/oauth-clients            bring-your-own OAuth clients (ids; secret_set only)
    PUT    /admin/email/oauth-clients/{provider} {client_id, client_secret?, tenant?}

Sign-in page (public, see mail/recovery.py):

    GET    /session/recovery                     {available}
    POST   /session/recovery/request             {user_id, purpose: sign_in | reset_token} -> constant answer
    POST   /session/recovery/redeem              {user_id, purpose, code} -> session (+ new token)

Legacy aliases of the retired process-wide `/email/*` routes (admin only, the CALLING admin's
own account; removed one minor later): GET /email/accounts, GET /email/messages,
GET /email/messages/{uid}, POST /email/send.

Errors carry `{"detail": {reason_code, message, cause, fix, retryable}}`: 400 invalid input,
403 entity / admin-only, 404 no account, 409 turned off / credentials missing, 422 the mail
server refused (wrong password, TLS, unreachable ...), 429 send limit.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..mail.core_mail import EmailError, EmailInvalidMessage, OutgoingMessage, SearchCriteria, evaluate, parse_recipients

from ..mail import accounts as mail_accounts
from ..mail.accounts import EmailPlane, EmailPrincipalRefused
from ..security.principal import GatewayPrincipal
from .gateway import _issue_gateway_browser_session, _off_the_event_loop, _principal_from_request, _require_admin_principal

router = APIRouter(prefix="/gateway", tags=["email"])

_STATUS_BY_CODE = {
    "email_invalid_settings": 400,
    "email_invalid_message": 400,
    "email_policy_refused": 400,
    "email_not_configured": 404,
    "email_message_not_found": 404,
    "email_attachment_not_found": 404,
    "email_disabled": 409,
    "email_secret_unavailable": 409,
    "email_rate_limited": 429,
}


def _error_response(err: EmailError) -> JSONResponse:
    status = _STATUS_BY_CODE.get(err.code, 422)
    body = err.to_dict(include_details=True)
    return JSONResponse(
        status_code=status,
        content={"ok": False, "detail": {"reason_code": err.code, "message": err.message, **body}},
    )


async def _call(fn, *args: Any, **kwargs: Any) -> Any:
    try:
        return await _off_the_event_loop(fn, *args, **kwargs)
    except EmailError as err:
        return _error_response(err)


def _self_plane(request: Request) -> tuple[GatewayPrincipal, EmailPlane]:
    principal = _principal_from_request(request)
    try:
        return principal, mail_accounts.plane_for_principal(principal)
    except EmailPrincipalRefused as exc:
        raise HTTPException(status_code=403, detail={"reason_code": "email_principal_refused", "message": str(exc)}) from None


def _actor(principal: GatewayPrincipal) -> str:
    return f"{principal.tenant_id}:{principal.user_id}"


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class ServerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = ""
    port: Optional[int] = None
    security: str = "ssl"
    folder: str = "INBOX"
    ca_file: str = Field("", description="PEM file of a private CA on the gateway host (administrators only)")


class ConnectBody(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "address": "me@example.test",
                    "password": "app-password",
                    "imap": {"host": "imap.example.test", "port": 993, "security": "ssl"},
                    "smtp": {"host": "smtp.example.test", "port": 587, "security": "starttls"},
                }
            ]
        },
    )

    address: str
    password: str = Field(..., min_length=1, description="Password or app password; stored encrypted, never returned")
    username: str = ""
    display_name: str = ""
    imap: Optional[ServerBody] = None
    smtp: Optional[ServerBody] = None
    test: bool = True


class PolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"mode": "allowlist", "entries": ["me@example.test", "example.org"]}]})

    mode: str = "allowlist"
    entries: List[str] = Field(default_factory=list)


class CheckBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    addresses: List[str] = Field(default_factory=list)


class LimitsBody(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"per_hour": 20, "per_day": 100}]})

    per_hour: Optional[int] = None
    per_day: Optional[int] = None


class EnabledBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class OAuthStartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: str
    provider: str = Field(..., description="google | microsoft (custom: administrators only)")
    client_id: str = Field("", description="Your own OAuth client id; empty = the gateway's client (admin setting), else the built-in one")
    client_secret: str = Field("", description="Your own OAuth client secret; stored encrypted, never returned")
    tenant: str = ""
    flow: str = Field("", description="device | loopback (default: device for Microsoft, loopback for Google)")
    display_name: str = ""
    imap: Optional[ServerBody] = None
    smtp: Optional[ServerBody] = None
    token_endpoint: str = ""
    authorization_endpoint: str = ""
    device_authorization_endpoint: str = ""
    scopes: List[str] = Field(default_factory=list)
    ca_file: str = ""


class FlowBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flow_id: str
    wait_s: float = 0.0


class NotificationsBody(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"email": {"job_failed": True, "automation_result": False}}]})

    email: Dict[str, bool] = Field(default_factory=dict)


class OAuthClientBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field("", description="Empty = remove this provider's gateway client")
    client_secret: Optional[str] = Field(None, description="Omit to keep the stored secret of the same client id")
    tenant: str = ""


def _server_dict(body: Optional[ServerBody]) -> Optional[Dict[str, Any]]:
    return body.model_dump() if body is not None else None


# ---------------------------------------------------------------------------
# /me/email
# ---------------------------------------------------------------------------


@router.get("/me/email", summary="My email account: settings and status")
async def me_email_get(request: Request) -> Any:
    principal, plane = _self_plane(request)

    def run() -> Dict[str, Any]:
        out = mail_accounts.public_status(plane)
        if principal.is_admin() and plane.is_default:
            out["notices"] = mail_accounts.boot_notices()
        return out

    return await _call(run)


@router.put("/me/email", summary="Connect my email account (test, then store)")
async def me_email_put(request: Request, body: ConnectBody) -> Any:
    principal, plane = _self_plane(request)

    def run() -> Dict[str, Any]:
        return {
            "ok": True,
            **mail_accounts.connect_password(
                plane,
                address=body.address,
                password=body.password,
                username=body.username,
                display_name=body.display_name,
                imap=_server_dict(body.imap),
                smtp=_server_dict(body.smtp),
                test=body.test,
                allow_ca_file=principal.is_admin(),
                actor=_actor(principal),
            ),
        }

    return await _call(run)


@router.post("/me/email/test", summary="Test my stored email account")
async def me_email_test(request: Request) -> Any:
    principal, plane = _self_plane(request)
    return await _call(mail_accounts.test_account, plane, actor=_actor(principal))


@router.delete("/me/email", summary="Disconnect my email account")
async def me_email_delete(request: Request) -> Any:
    principal, plane = _self_plane(request)

    def run() -> Dict[str, Any]:
        return {"ok": True, **mail_accounts.disconnect(plane, actor=_actor(principal))}

    return await _call(run)


@router.put("/me/email/policy", summary="Set my recipient policy")
async def me_email_policy(request: Request, body: PolicyBody) -> Any:
    _principal, plane = _self_plane(request)

    def run() -> Dict[str, Any]:
        mail_accounts.account_store(plane).set_policy(mode=body.mode, add=body.entries, clear=True)
        return {"ok": True, **mail_accounts.public_status(plane)}

    return await _call(run)


@router.post("/me/email/policy/check", summary="Check recipients against my policy")
async def me_email_policy_check(request: Request, body: CheckBody) -> Any:
    _principal, plane = _self_plane(request)

    def run() -> Dict[str, Any]:
        try:
            addrs = parse_recipients(list(body.addresses))
        except ValueError as exc:
            raise EmailInvalidMessage(f"A recipient is not valid: {exc}.", "Give recipients as name@example.test.") from None
        return evaluate(mail_accounts.account_store(plane).settings().policy, to=addrs).to_dict()

    return await _call(run)


@router.put("/me/email/limits", summary="Set my send limits")
async def me_email_limits(request: Request, body: LimitsBody) -> Any:
    _principal, plane = _self_plane(request)

    def run() -> Dict[str, Any]:
        mail_accounts.account_store(plane).set_limits(per_hour=body.per_hour, per_day=body.per_day)
        return {"ok": True, **mail_accounts.public_status(plane)}

    return await _call(run)


@router.put("/me/email/enabled", summary="Turn my email on or off")
async def me_email_enabled(request: Request, body: EnabledBody) -> Any:
    principal, plane = _self_plane(request)

    def run() -> Dict[str, Any]:
        mail_accounts.account_store(plane).set_enabled(body.enabled)
        mail_accounts.rebind_live_runtime(plane)
        from ..mail.audit import audit_email_event

        audit_email_event("email.user_switch", tenant_id=plane.tenant_id, user_id=plane.user_id, actor=_actor(principal), enabled=bool(body.enabled))
        return {"ok": True, **mail_accounts.public_status(plane)}

    return await _call(run)


@router.get("/me/email/oauth/clients", summary="OAuth clients available for sign-in (no secrets)")
async def me_email_oauth_clients(request: Request) -> Any:
    _self_plane(request)
    return await _call(mail_accounts.oauth_clients_public)


@router.post("/me/email/oauth/start", summary="Begin an OAuth2 sign-in for my mailbox")
async def me_email_oauth_start(request: Request, body: OAuthStartBody) -> Any:
    principal, plane = _self_plane(request)

    def run() -> Dict[str, Any]:
        return mail_accounts.oauth_start(
            plane,
            address=body.address,
            provider=body.provider,
            client_id=body.client_id,
            client_secret=body.client_secret,
            tenant=body.tenant,
            flow=body.flow,
            display_name=body.display_name,
            imap=_server_dict(body.imap),
            smtp=_server_dict(body.smtp),
            token_endpoint=body.token_endpoint,
            authorization_endpoint=body.authorization_endpoint,
            device_authorization_endpoint=body.device_authorization_endpoint,
            scopes=body.scopes,
            ca_file=body.ca_file,
            is_admin=principal.is_admin(),
        )

    return await _call(run)


@router.post("/me/email/oauth/poll", summary="Is my OAuth2 sign-in approved yet? (no wait)")
async def me_email_oauth_poll(request: Request, body: FlowBody) -> Any:
    principal, plane = _self_plane(request)
    return await _call(mail_accounts.oauth_finish, plane, body.flow_id, wait_s=0.0, actor=_actor(principal))


@router.post("/me/email/oauth/finish", summary="Complete my OAuth2 sign-in (waits up to 60 s)")
async def me_email_oauth_finish(request: Request, body: FlowBody) -> Any:
    principal, plane = _self_plane(request)
    return await _call(mail_accounts.oauth_finish, plane, body.flow_id, wait_s=body.wait_s, actor=_actor(principal))


@router.post("/me/email/oauth/cancel", summary="Cancel my pending OAuth2 sign-in")
async def me_email_oauth_cancel(request: Request, body: FlowBody) -> Any:
    _principal, plane = _self_plane(request)
    return await _call(mail_accounts.oauth_cancel, plane, body.flow_id)


# ---------------------------------------------------------------------------
# /me/notifications
# ---------------------------------------------------------------------------


@router.get("/me/notifications", summary="My notification preferences")
async def me_notifications_get(request: Request) -> Any:
    _principal, plane = _self_plane(request)
    from ..mail.notifications import preferences_public

    return await _call(preferences_public, plane)


@router.put("/me/notifications", summary="Set my notification preferences")
async def me_notifications_put(request: Request, body: NotificationsBody) -> Any:
    _principal, plane = _self_plane(request)
    from ..mail.notifications import preferences_public, write_preferences

    def run() -> Dict[str, Any]:
        try:
            write_preferences(plane, dict(body.email))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"reason_code": "invalid_request", "message": str(exc)}) from None
        return {"ok": True, **preferences_public(plane)}

    return await _call(run)


@router.post("/me/notifications/test", summary="Send me a test notification now")
async def me_notifications_test(request: Request) -> Any:
    _principal, plane = _self_plane(request)
    from ..mail.notifications import send_test_notification

    return await _call(send_test_notification, plane)


# ---------------------------------------------------------------------------
# Admin: the per-user switch and status (never content), OAuth clients
# ---------------------------------------------------------------------------


def _admin_plane(user_id: str, tenant_id: str) -> EmailPlane:
    try:
        return mail_accounts.plane_for_user(user_id, tenant_id=tenant_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Gateway user not found") from None
    except EmailPrincipalRefused as exc:
        raise HTTPException(status_code=400, detail={"reason_code": "email_principal_refused", "message": str(exc)}) from None


@router.get("/admin/users/{user_id}/email", summary="A user's email status (no content)")
async def admin_user_email_get(request: Request, user_id: str, tenant_id: str = Query(default="default")) -> Any:
    _require_admin_principal(request)
    plane = _admin_plane(user_id, tenant_id)
    return await _call(mail_accounts.admin_status, plane)


@router.put("/admin/users/{user_id}/email", summary="Turn email on or off for a user")
async def admin_user_email_put(request: Request, user_id: str, body: EnabledBody, tenant_id: str = Query(default="default")) -> Any:
    admin = _require_admin_principal(request)
    plane = _admin_plane(user_id, tenant_id)

    def run() -> Dict[str, Any]:
        mail_accounts.set_admin_email_enabled(plane, body.enabled, actor=_actor(admin))
        return {"ok": True, **mail_accounts.admin_status(plane)}

    return await _call(run)


@router.get("/admin/email/oauth-clients", summary="Bring-your-own OAuth clients of this gateway")
async def admin_oauth_clients_get(request: Request) -> Any:
    _require_admin_principal(request)
    return await _call(mail_accounts.oauth_clients_public)


@router.put("/admin/email/oauth-clients/{provider}", summary="Set (or clear) the gateway's OAuth client for a provider")
async def admin_oauth_clients_put(request: Request, provider: str, body: OAuthClientBody) -> Any:
    admin = _require_admin_principal(request)
    return await _call(
        mail_accounts.set_oauth_client,
        provider,
        client_id=body.client_id,
        client_secret=body.client_secret,
        tenant=body.tenant,
        actor=_actor(admin),
    )


# ---------------------------------------------------------------------------
# Sign-in page: account recovery by email (public)
# ---------------------------------------------------------------------------


class RecoveryRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1, max_length=200)
    tenant_id: str = Field(default="default", min_length=1, max_length=200)
    purpose: str = Field(..., description="sign_in | reset_token")


class RecoveryRedeemBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1, max_length=200)
    tenant_id: str = Field(default="default", min_length=1, max_length=200)
    purpose: str = Field(..., description="sign_in | reset_token")
    code: str = Field(..., min_length=1, max_length=64)
    remember: bool = False


def _client_ip(request: Request) -> str:
    try:
        from ..network_exposure import live_reverse_proxy

        if live_reverse_proxy().trust_proxy:
            xff = str(request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
            if xff:
                return xff
    except Exception:  # noqa: BLE001
        pass
    return str(getattr(getattr(request, "client", None), "host", "") or "unknown")


@router.get("/session/recovery", summary="Are email recovery options offered on this gateway?")
async def session_recovery_available() -> Dict[str, Any]:
    from ..mail.recovery import recovery_available

    available = await _off_the_event_loop(recovery_available)
    return {"available": bool(available), "purposes": ["sign_in", "reset_token"] if available else []}


@router.post("/session/recovery/request", summary="Email me a code (constant answer)")
async def session_recovery_request(request: Request, body: RecoveryRequestBody) -> Dict[str, Any]:
    from ..mail.recovery import PURPOSES, request_code

    if body.purpose not in PURPOSES:
        raise HTTPException(status_code=400, detail={"reason_code": "invalid_request", "message": "purpose must be sign_in or reset_token"})
    return request_code(user_id=body.user_id, tenant_id=body.tenant_id, purpose=body.purpose, client_ip=_client_ip(request))


@router.post("/session/recovery/redeem", summary="Redeem an emailed code for a session (and a new token)")
async def session_recovery_redeem(request: Request, response: Response, body: RecoveryRedeemBody) -> Dict[str, Any]:
    from ..mail.recovery import REDEEM_REFUSED, redeem_code

    principal = await _off_the_event_loop(
        redeem_code, user_id=body.user_id, tenant_id=body.tenant_id, purpose=body.purpose, code=body.code, client_ip=_client_ip(request)
    )
    if principal is None:
        raise HTTPException(status_code=401, detail={"reason_code": "recovery_code_refused", "message": REDEEM_REFUSED})
    issued_token: Optional[str] = None
    if body.purpose == "reset_token":
        from ..users import GatewayUserRegistry

        _record, issued_token = await _off_the_event_loop(
            lambda: GatewayUserRegistry().update_user(user_id=principal.user_id, tenant_id=principal.tenant_id, token="")
        )
        request.state.audit_detail = {"recovery": {"purpose": "reset_token", "token_rotated": True}}
    out = _issue_gateway_browser_session(request, response, principal, remember=bool(body.remember))
    if issued_token:
        out["token"] = issued_token
        out["token_note"] = "Your new gateway token. It is shown once; your old token no longer works."
    return out


# ---------------------------------------------------------------------------
# Legacy aliases: the retired process-wide /email/* routes, now the calling admin's own account
# ---------------------------------------------------------------------------


def _legacy_plane(request: Request) -> tuple[GatewayPrincipal, EmailPlane]:
    principal = _require_admin_principal(request)
    try:
        return principal, mail_accounts.plane_for_principal(principal)
    except EmailPrincipalRefused as exc:
        raise HTTPException(status_code=403, detail={"reason_code": "email_principal_refused", "message": str(exc)}) from None


@router.get("/email/accounts", summary="Legacy: my email account (use GET /me/email)", deprecated=True)
async def legacy_email_accounts(request: Request) -> Any:
    _principal, plane = _legacy_plane(request)

    def run() -> Dict[str, Any]:
        pub = mail_accounts.public_status(plane)
        accounts = []
        if pub.get("configured"):
            accounts.append(
                {
                    "account": mail_accounts.ACCOUNT_ID,
                    "email": pub.get("address") or "",
                    "from_email": pub.get("address") or None,
                    "can_read": bool(pub.get("can_read")),
                    "can_send": bool(pub.get("can_send")),
                    "imap_password_set": bool(pub.get("secret_set")),
                    "smtp_password_set": bool(pub.get("secret_set")),
                }
            )
        return {"ok": True, "source": "gateway user settings", "config_path": "", "default_account": mail_accounts.ACCOUNT_ID if accounts else "", "accounts": accounts, "deprecated": "Use GET /api/gateway/me/email."}

    return await _call(run)


@router.get("/email/messages", summary="Legacy: list my mail (read-only)", deprecated=True)
async def legacy_email_messages(
    request: Request,
    account: str = Query(default="", description="Ignored: one account per user."),
    mailbox: str = Query(default="", description="Folder (default: the account's folder, INBOX)."),
    since: str = Query(default="", description="e.g. '7d' or an ISO date."),
    status: str = Query(default="all", description="all|unread|read"),
    limit: int = Query(default=20, ge=1),
) -> Any:
    _principal, plane = _legacy_plane(request)

    def run() -> Dict[str, Any]:
        status0 = str(status or "all").strip().lower()
        if status0 not in ("all", "unread", "read"):
            raise EmailInvalidMessage("status must be all, unread or read.", "Use status=all|unread|read.")
        unseen = True if status0 == "unread" else (False if status0 == "read" else None)
        crit = SearchCriteria.build(since=str(since or "").strip() or None, unseen=unseen)
        ctx = mail_accounts.email_context(plane)
        found = ctx.client().search(crit, folder=str(mailbox or "").strip() or None, limit=int(limit))
        msgs = [m.to_dict() for m in found["messages"]]
        unread = sum(1 for m in msgs if not m.get("seen"))
        return {
            "ok": True,
            "account": mail_accounts.ACCOUNT_ID,
            "mailbox": found["folder"],
            "filter": {"since": str(since or "") or None, "status": status0, "limit": int(limit)},
            "counts": {"returned": len(msgs), "unread": unread, "read": len(msgs) - unread},
            "messages": msgs,
        }

    return await _call(run)


@router.get("/email/messages/{uid}", summary="Legacy: read one of my messages (read-only, whole body)", deprecated=True)
async def legacy_email_message(
    request: Request,
    uid: str,
    account: str = Query(default=""),
    mailbox: str = Query(default=""),
) -> Any:
    _principal, plane = _legacy_plane(request)

    def run() -> Dict[str, Any]:
        ctx = mail_accounts.email_context(plane)
        detail = ctx.client().get(uid, folder=str(mailbox or "").strip() or None)
        out = detail.to_dict()
        out.update({"ok": True, "account": mail_accounts.ACCOUNT_ID, "mailbox": detail.summary.folder, "content_trust": "untrusted"})
        return out

    return await _call(run)


class LegacySendBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    to: Any = Field(..., description="Recipient email or list of emails.")
    subject: str
    body_text: Optional[str] = None
    body_html: Optional[str] = None
    cc: Any = None
    bcc: Any = None


@router.post("/email/send", summary="Legacy: send from my account (recipient policy + limits apply)", deprecated=True)
async def legacy_email_send(request: Request, body: LegacySendBody) -> Any:
    principal, plane = _legacy_plane(request)

    def run() -> Dict[str, Any]:
        try:
            to = parse_recipients(body.to)
            cc = parse_recipients(body.cc) if body.cc else []
            bcc = parse_recipients(body.bcc) if body.bcc else []
        except ValueError as exc:
            raise EmailInvalidMessage(f"A recipient is not valid: {exc}.", "Give recipients as name@example.test.") from None
        ctx = mail_accounts.email_context(plane)
        result = ctx.send(
            OutgoingMessage(to=tuple(to), cc=tuple(cc), bcc=tuple(bcc), subject=str(body.subject or ""), text=body.body_text or "", html=body.body_html or "")
        )
        from ..mail.audit import audit_email_event

        audit_email_event("email.sent_from_console", tenant_id=plane.tenant_id, user_id=plane.user_id, actor=_actor(principal), message_id=result.message_id, count=len(to) + len(cc) + len(bcc))
        return {
            "ok": True,
            "account": mail_accounts.ACCOUNT_ID,
            "message_id": result.message_id,
            "from": ctx.account.address,
            "to": list(to),
            "cc": list(cc),
            "bcc": list(bcc),
        }

    return await _call(run)
