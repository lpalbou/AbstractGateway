"""Per-user email over HTTP (framework backlog 0992 WP3), under `/api/gateway`.

Self routes — every signed-in human, on THEIR OWN plane (resolved from the authenticated
principal, never from a path or body id; entities are refused):

Words (DESIGN 2026-09-30 §1): the EMAIL ADDRESS is the user's own address (sign-in codes,
notifications, the default allowed recipient; no password); the MAILBOX is the connection the
user makes so their agents and automations can read and send mail as them.

    GET    /me/email                     mailbox settings + status, email address, switches (never a secret)
    POST   /me/email/discover            {address} -> the mailbox's IMAP/SMTP servers (auto-discovery)
    PUT    /me/email                     connect the mailbox: test, then store (imap/smtp omitted = discovered)
    PUT    /me/email/address             {address}   my email address ("" clears it; users registry)
    PUT    /me/email/notifications       {job_failed?, approval_needed?}  my two notification switches
    POST   /me/email/test                sign in to IMAP and SMTP with the stored mailbox
    DELETE /me/email                     disconnect the mailbox: credentials and cursor deleted
    PUT    /me/email/policy              {mode: allowlist|denylist, entries: [address | domain]}
    POST   /me/email/policy/check        {addresses} -> would they be allowed?
    PUT    /me/email/limits              {per_hour, per_day}
    PUT    /me/email/folder              {folder}    the IMAP folder read (empty = INBOX; connection kept)
    PUT    /me/email/enabled             {enabled}   "Use this mailbox" (the user's own switch)
    PUT    /me/email/agent-tools         {enabled}   "Agent email tools" (default off; needs a usable mailbox)
    GET    /me/email/oauth/clients       which providers have a gateway OAuth client (no secrets)
    POST   /me/email/oauth/start         begin an OAuth2 sign-in (device code or loopback browser)
    POST   /me/email/oauth/poll          {flow_id}          -> pending | connected
    POST   /me/email/oauth/finish        {flow_id, wait_s}  wait up to 60 s for the approval
    POST   /me/email/oauth/cancel        {flow_id}
    GET    /me/notifications             events, channel availability, outbox summary
    PUT    /me/notifications             {email: {event: bool}}  (v1 five-kind body accepted and mapped)
    POST   /me/notifications/test        send one test notification now

Entity mailboxes (round 3 §3.1: entities are AI users with their own mailbox) — every `/me/email...`
and `/me/notifications...` route above is mirrored at `/accounts/{account_id}/email...` and
`/accounts/{account_id}/notifications...` with identical payloads and answers, acting on the
ENTITY's plane (rooted at its home). Allowed for an admin and for the entity's creator; 403
`{message}` when the target is a user (users manage their own mailbox through `/me`), is not an
entity the caller may manage, or is archived. There is no route that reads an entity's mail.

Admin routes — status and the per-user switch only (D3: administrators never read mail):

    GET    /admin/users/{user_id}/email          configured / address / state / last error
    PUT    /admin/users/{user_id}/email          {enabled?, agent_tools?, inherit?}  per-user overrides (legacy UI)
    GET    /admin/email/capabilities             gateway-wide defaults: email ("Mailboxes for users"),
                                                 email_agent_tools, email_recovery (Advanced)
    PUT    /admin/email/capabilities             {email?, email_agent_tools?, email_recovery?, reset?}
    GET    /admin/email/oauth-clients            bring-your-own OAuth clients (ids; secret_set only)
    PUT    /admin/email/oauth-clients/{provider} {client_id, client_secret?, tenant?}

Sign-in page (public, see mail/recovery.py):

    GET    /session/recovery                     {available}
    POST   /session/recovery/request             {user_id, purpose?: sign_in | reset_token} -> sent (masked to) |
                                                 no_email_address | too_many_requests (+ retry_after_s)
    POST   /session/recovery/redeem              {user_id, purpose, code} -> session (+ new token)

Legacy aliases of the retired process-wide `/email/*` routes (admin only, the CALLING admin's
own account; removed one minor later): GET /email/accounts, GET /email/messages,
GET /email/messages/{uid}, POST /email/send.

Errors carry `{"detail": {reason_code, message, cause, fix, retryable}}` (a failed connect also
`step`: imap | smtp, and `message` names it; `email_discovery_failed` also `tried`): 400 invalid input,
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
    "email_discovery_failed": 400,
    "email_invalid_message": 400,
    "email_policy_refused": 400,
    "email_not_configured": 404,
    "email_message_not_found": 404,
    "email_attachment_not_found": 404,
    "email_oauth_override_refused": 403,
    "email_disabled": 409,
    "email_secret_unavailable": 409,
    "email_rate_limited": 429,
}


def _error_response(err: EmailError) -> JSONResponse:
    status = _STATUS_BY_CODE.get(err.code, 422)
    body = err.to_dict(include_details=True)
    details = err.details or {}
    detail: Dict[str, Any] = {"reason_code": err.code, "message": str(details.get("step_message") or err.message), **body}
    if details.get("step"):
        detail["step"] = details["step"]
    if err.code == "email_discovery_failed":
        detail["tried"] = list(details.get("tried") or [])
    return JSONResponse(status_code=status, content={"ok": False, "detail": detail})


async def _call(fn, *args: Any, **kwargs: Any) -> Any:
    try:
        return await _off_the_event_loop(fn, *args, **kwargs)
    except EmailError as err:
        return _error_response(err)


def _self_plane(request: Request) -> tuple[GatewayPrincipal, EmailPlane]:
    """(the CALLER, the plane acted on). `/me/...`: the caller's own plane. The
    `/accounts/{account_id}/...` mirror: the entity's plane, after `_entity_target` checked that
    the caller may manage it (the caller stays the actor in audit lines)."""
    principal = _principal_from_request(request)
    account_id = request.path_params.get("account_id")
    try:
        if account_id is not None:
            plane = mail_accounts.plane_for_principal(_entity_target(principal, str(account_id)))
            # The entity's mailbox worker runs from the first time its mailbox is touched.
            from ..mail.worker import sync_entity_workers

            sync_entity_workers()
            return principal, plane
        return principal, mail_accounts.plane_for_principal(principal)
    except EmailPrincipalRefused as exc:
        raise HTTPException(status_code=403, detail={"reason_code": "email_principal_refused", "message": str(exc)}) from None


def _target_principal(request: Request, caller: GatewayPrincipal) -> GatewayPrincipal:
    """The account whose address/record a route reads or writes: the caller on `/me/...`, the
    entity on the `/accounts/{account_id}/...` mirror."""
    account_id = request.path_params.get("account_id")
    return caller if account_id is None else _entity_target(caller, str(account_id))


ENTITY_MAILBOX_NOT_YOURS = "There is no entity named {id!r} whose mailbox you can manage."
ENTITY_MAILBOX_USER_TARGET = (
    "{id} is a user: users manage their own mailbox from their own account page; this is for entities."
)


def _entity_target(caller: GatewayPrincipal, account_id: str) -> GatewayPrincipal:
    """The entity principal `account_id`, when `caller` may configure its mailbox: an admin, or
    the entity's creator (`entity_access.entity_visible_to` on the home's manifest). 403
    `{message}` otherwise: a user target (named only to an admin), an entity the caller may not
    see or that does not exist (the same sentence), an archived entity."""
    from ..entity_access import entity_archived, entity_home_dir, entity_visible_to, manifest_creator
    from ..users import GatewayUserRegistry

    def refuse(message: str, code: str = "email_target_refused") -> HTTPException:
        return HTTPException(status_code=403, detail={"reason_code": code, "message": message})

    rec = GatewayUserRegistry().get_user(account_id)
    if rec is not None and rec.principal_kind != "entity":
        if caller.is_admin():
            raise refuse(ENTITY_MAILBOX_USER_TARGET.format(id=rec.user_id), "email_target_is_user")
        raise refuse(ENTITY_MAILBOX_NOT_YOURS.format(id=account_id))
    home = entity_home_dir(account_id)
    if rec is None or home is None:
        raise refuse(ENTITY_MAILBOX_NOT_YOURS.format(id=account_id))
    if not caller.is_admin():
        _exists, created_by = manifest_creator(home.parent, home.name)
        if not entity_visible_to(caller, home.name, created_by):
            raise refuse(ENTITY_MAILBOX_NOT_YOURS.format(id=account_id))
    if entity_archived(rec.user_id):
        from ..entity_access import ENTITY_ARCHIVED

        raise refuse(ENTITY_ARCHIVED.format(slug=rec.user_id), "entity_archived")
    return rec.to_principal()


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

    address: str = Field(..., description="The mailbox's address")
    password: str = Field(..., min_length=1, description="Password or app password; stored encrypted, never returned")
    username: str = Field("", description="Login, optional: empty = the discovered login form, else the address")
    display_name: str = Field(
        "", description="Name on sent mail, optional: empty = the stored name, else the address's local part"
    )
    imap: Optional[ServerBody] = Field(None, description="Omit imap AND smtp to discover the servers from the address")
    smtp: Optional[ServerBody] = Field(None, description="Omit imap AND smtp to discover the servers from the address")
    test: bool = True


class DiscoverBody(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"address": "me@fastmail.com"}]})

    address: str = Field(..., min_length=3, max_length=254, description="The mailbox's address")


class AddressBody(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"address": "me@example.test"}]})

    address: str = Field(..., max_length=254, description="My email address; \"\" clears it")


class MyNotificationsBody(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"job_failed": True}, {"approval_needed": False}]})

    job_failed: Optional[bool] = Field(None, description="\"Job failed\": a run or automation of mine failed, after its retries")
    approval_needed: Optional[bool] = Field(None, description="\"Approval needed\": a run is waiting for my answer")
    email: Optional[Dict[str, bool]] = Field(None, description="Legacy five-kind body ({kind: bool}); mapped")


class PolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"mode": "allowlist", "entries": ["me@example.test", "example.org"]}]})

    mode: str = "allowlist"
    entries: List[str] = Field(default_factory=list)


class CheckBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    addresses: List[str] = Field(default_factory=list)


class LimitsBody(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"per_hour": 100, "per_day": 1000}]})

    per_hour: Optional[int] = None
    per_day: Optional[int] = None


class FolderBody(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"folder": "INBOX"}]})

    folder: str = Field("", max_length=255, description="The IMAP folder to read; empty = INBOX")


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
    token_endpoint: str = Field("", description="provider custom only (administrators); refused otherwise")
    authorization_endpoint: str = Field("", description="provider custom only (administrators); refused otherwise")
    device_authorization_endpoint: str = Field("", description="provider custom only (administrators); refused otherwise")
    scopes: List[str] = Field(default_factory=list, description="provider custom only (administrators); refused otherwise")
    ca_file: str = Field("", description="A PEM file on the gateway host (administrators only)")


class FlowBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flow_id: str
    wait_s: float = 0.0


class NotificationsBody(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"email": {"job_failed": True, "approval_needed": False}}]})

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


@router.get(
    "/me/email",
    summary="My mailbox and email address: settings, status and switches",
    description="Never a secret. Besides the mailbox settings: `email_address` (my email address as stored on my "
    "user record; \"\" when none), `registered_address` (\"self\" for runs: that address, else the connected "
    "mailbox's own), `email_available` (my admin allows mailboxes), `notifications` {job_failed, approval_needed} "
    "(+ `notifications_unavailable_reason`), `agent_tools` {on, available, unavailable_reason, active}, "
    "`oauth_providers` [{id, available, reason}].",
)
async def me_email_get(request: Request) -> Any:
    principal, plane = _self_plane(request)

    def run() -> Dict[str, Any]:
        out = mail_accounts.public_status(plane)
        # The shared resolver (DESIGN-v2 §2.5): the admin's row in /admin/users and
        # /admin/accounts reads exactly this.
        view = mail_accounts.account_email_view(_target_principal(request, principal))
        out["email_address"] = view["email_address"] or ""
        out["mailbox"] = view["mailbox"]
        if principal.is_admin() and plane.is_default:
            out["notices"] = mail_accounts.boot_notices()
        return out

    return await _call(run)


@router.post(
    "/me/email/discover",
    summary="Find my mailbox's IMAP and SMTP servers from its address",
    description="Known providers, the domain's autoconfig file, the Thunderbird ISPDB, DNS SRV, then MX "
    "(AbstractCore's deterministic discovery). 200 for a valid address: `found` says whether both servers were "
    "found, `tried` lists every step, and `defaults` is what the mailbox form pre-fills: {imap, smtp: {host, port, "
    "security}, login, source: discovered | standard, provider, message} (the discovered servers, else imap.<domain> "
    "993 SSL and smtp.<domain> 465 SSL). 400 `email_invalid_settings` for a non-address.",
)
async def me_email_discover(request: Request, body: DiscoverBody) -> Any:
    _self_plane(request)
    return await _call(mail_accounts.discover, body.address)


@router.put(
    "/me/email",
    summary="Connect my mailbox (test, then store)",
    description="Saves and tests in one call. Without `imap` and `smtp` the servers are discovered from the "
    "address (the answer then carries `discovery` {source, provider, tried}); none found = 400 "
    "`email_discovery_failed` with `tried`. `username` (optional) defaults to the discovered login, else the address; "
    "`display_name` (optional) to the stored name, else the address's local part. Connecting sets your email address "
    "when it is empty. A failed "
    "test stores nothing and names its step: `detail.step` (imap | smtp) and `detail.message` "
    "(\"Sign-in refused by imap.x.com \u2014 check the password.\" / \"Couldn't reach smtp.x.com:465.\").",
)
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


@router.put(
    "/me/email/address",
    summary="Set my email address",
    description="My email address (not a mailbox): where sign-in codes and notifications go, and the first address "
    "my agents may write to. Stored on my user record (the users registry is the source of truth; the account-less "
    "operator keeps the gateway's operator_email setting). \"\" clears it; an invalid address is 400.",
)
async def me_email_address(request: Request, body: AddressBody) -> Any:
    principal, _plane = _self_plane(request)

    def run() -> Dict[str, Any]:
        target = _target_principal(request, principal)
        return {"ok": True, **mail_accounts.set_email_address(target, body.address, actor=_actor(principal))}

    return await _call(run)


@router.put(
    "/me/email/notifications",
    summary="Set my notification switches (Job failed, Approval needed)",
    description="`{job_failed?, approval_needed?}`; both ON by default, sent only once a mailbox is connected. The "
    "old body `{email: {automation_result, automation_failed, approval_needed, job_finished, job_failed}}` is "
    "accepted and mapped (automation_failed counts for job_failed; the per-automation \"Email me the result\" and "
    "per-run \"email me when done\" options no longer need a preference). Answers like GET /me/email.",
)
async def me_email_notifications(request: Request, body: MyNotificationsBody) -> Any:
    _principal, plane = _self_plane(request)
    from ..mail.notifications import write_preferences

    changes: Dict[str, Any] = dict(body.email or {})
    for k in ("job_failed", "approval_needed"):
        v = getattr(body, k)
        if v is not None:
            changes[k] = v

    def run() -> Dict[str, Any]:
        try:
            write_preferences(plane, changes)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"reason_code": "invalid_request", "message": str(exc)}) from None
        return {"ok": True, **mail_accounts.public_status(plane)}

    return await _call(run)


@router.post(
    "/me/email/test",
    summary="Test my connected mailbox",
    description="Signs in to IMAP and SMTP with the stored mailbox: `{imap, smtp, ok, message}`; `message` says "
    "\"Test passed: signed in to imap.x and smtp.x.\" or which step failed and why.",
)
async def me_email_test(request: Request) -> Any:
    principal, plane = _self_plane(request)
    return await _call(mail_accounts.test_account, plane, actor=_actor(principal))


@router.delete("/me/email", summary="Disconnect my mailbox (policy and limits are kept)")
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


@router.put(
    "/me/email/folder",
    summary="Set my mailbox's folder (Advanced; empty = INBOX)",
    description="The IMAP folder my agents and the mail watcher read. The connection is kept and nothing is "
    "tested; the watcher starts the new folder from a fresh baseline. 404 `email_not_configured` without a "
    "mailbox, 400 `email_invalid_settings` for a control character or a name over 255. Answers like GET /me/email.",
)
async def me_email_folder(request: Request, body: FolderBody) -> Any:
    principal, plane = _self_plane(request)

    def run() -> Dict[str, Any]:
        return {"ok": True, **mail_accounts.set_folder(plane, body.folder, actor=_actor(principal))}

    return await _call(run)


@router.put("/me/email/enabled", summary="\"Use this mailbox\" (off keeps the settings; stops watching, sending and notifications)")
async def me_email_enabled(request: Request, body: EnabledBody) -> Any:
    principal, plane = _self_plane(request)

    def run() -> Dict[str, Any]:
        mail_accounts.account_store(plane).set_enabled(body.enabled)
        mail_accounts.rebind_live_runtime(plane)
        from ..mail.audit import audit_email_event

        audit_email_event("email.user_switch", tenant_id=plane.tenant_id, user_id=plane.user_id, actor=_actor(principal), enabled=bool(body.enabled))
        return {"ok": True, **mail_accounts.public_status(plane)}

    return await _call(run)


@router.put("/me/email/agent-tools", summary="\"Agent email tools\": my agents may use my mailbox (default off)")
async def me_email_agent_tools(request: Request, body: EnabledBody) -> Any:
    principal, plane = _self_plane(request)

    def run() -> Dict[str, Any]:
        mail_accounts.set_agent_tools_switch(plane, body.enabled, actor=_actor(principal))
        reloaded = False
        # The toolsets are built with the host: reload THIS user's host (only when it is
        # already built — never a first build from here) so agents see the change now.
        # An entity's runtime reads the switch at each tool call (entities.py): no host to reload.
        try:
            from .. import service as service_mod

            built = not plane.is_entity and (
                service_mod.principal_service_cached(principal)
                if service_mod.gateway_multi_user_enabled()
                else service_mod._service is not None
            )
            if built:
                service_mod.get_gateway_service().host.reload_bundles_from_disk()
                reloaded = True
        except Exception:  # noqa: BLE001 - the execution-time gate applies either way
            reloaded = False
        return {"ok": True, "tools_reloaded": reloaded, **mail_accounts.public_status(plane)}

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


@router.put("/me/notifications", summary="Set my notification preferences (legacy body; see PUT /me/email/notifications)")
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


@router.post(
    "/me/notifications/test",
    summary="Send me a test notification now",
    description="Always a sentence in `message` (\"Sent to x@y.\", \"Not sent: hourly limit reached (20 of 20 this hour) "
    "\u2014 resets at 14:05.\", gateway local time) and `reason_code`: null (sent) | no_mailbox | mailbox_paused | "
    "rate_limited | queued_behind (+ `queued_behind`: how many) | send_failed; `limit` {window: hour | day, limit, used, "
    "resets_at} when a send limit held it back.",
)
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
        raise HTTPException(
            status_code=404, detail={"reason_code": "user_not_found", "message": f"There is no user named {user_id!r} on this gateway."}
        ) from None
    except EmailPrincipalRefused as exc:
        raise HTTPException(status_code=400, detail={"reason_code": "email_principal_refused", "message": str(exc)}) from None


@router.get("/admin/users/{user_id}/email", summary="A user's mailbox status (never content)")
async def admin_user_email_get(request: Request, user_id: str, tenant_id: str = Query(default="default")) -> Any:
    _require_admin_principal(request)
    plane = _admin_plane(user_id, tenant_id)
    return await _call(mail_accounts.admin_status, plane)


class AdminUserEmailBody(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"enabled": True, "agent_tools": True}]})

    enabled: Optional[bool] = Field(None, description="Email for this user (off: no watcher, no sending, no notifications)")
    agent_tools: Optional[bool] = Field(None, description="Make Agent email tools available to this user")
    inherit: List[str] = Field(default_factory=list, description="Capabilities that follow the gateway default again: email, email_agent_tools")


@router.put(
    "/admin/users/{user_id}/email",
    summary="A user's mailbox overrides (legacy; `inherit` resets them)",
    description="Per-user overrides of \"Mailboxes for users\" (`enabled`) and \"Agent email tools for users\" "
    "(`agent_tools`). The console no longer creates them; `inherit: [\"email\", \"email_agent_tools\"]` clears them "
    "(the Users table's Reset action).",
)
async def admin_user_email_put(request: Request, user_id: str, body: AdminUserEmailBody, tenant_id: str = Query(default="default")) -> Any:
    admin = _require_admin_principal(request)
    plane = _admin_plane(user_id, tenant_id)

    def run() -> Dict[str, Any]:
        changes: Dict[str, Any] = {k: None for k in body.inherit}
        if body.enabled is not None:
            changes["email"] = body.enabled
        if body.agent_tools is not None:
            changes["email_agent_tools"] = body.agent_tools
        if changes:
            mail_accounts.set_user_capabilities(plane, changes, actor=_actor(admin))
        return {"ok": True, **mail_accounts.admin_status(plane)}

    return await _call(run)


class CapabilityDefaultsBody(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"email_agent_tools": True, "email_recovery": False}]})

    email: Optional[bool] = None
    email_agent_tools: Optional[bool] = None
    email_recovery: Optional[bool] = None
    reset: List[str] = Field(default_factory=list, description="Capabilities back to their built-in default")


@router.get(
    "/admin/email/capabilities",
    summary="What the gateway makes available to users (Mailboxes for users; Advanced: agent tools, sign-in by email)",
    description="`capabilities`: [{id, label, description, per_user, advanced, default, built_in_default}]. `email` = "
    "\"Mailboxes for users\" (the admin's one switch, ON built in); under Advanced `email_agent_tools` = \"Agent "
    "email tools for users\" (ON built in; each user still opts in) and `email_recovery` = \"Sign-in by email\".",
)
async def admin_capabilities_get(request: Request) -> Any:
    _require_admin_principal(request)
    return await _call(mail_accounts.capabilities_public)


@router.put("/admin/email/capabilities", summary="Set Mailboxes for users / Agent email tools for users / Sign-in by email")
async def admin_capabilities_put(request: Request, body: CapabilityDefaultsBody) -> Any:
    admin = _require_admin_principal(request)
    changes: Dict[str, Any] = {k: None for k in body.reset}
    for k in ("email", "email_agent_tools", "email_recovery"):
        v = getattr(body, k)
        if v is not None:
            changes[k] = v
    return await _call(mail_accounts.set_capability_defaults, changes, actor=_actor(admin))


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
    purpose: str = Field(default="sign_in", description="sign_in (default: the code opens a session) | reset_token")


class RecoveryRedeemBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1, max_length=200)
    tenant_id: str = Field(default="default", min_length=1, max_length=200)
    purpose: str = Field(default="sign_in", description="sign_in (default) | reset_token")
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


_RECOVERY_REQUEST_DESCRIPTION = """Emails a sign-in code (8 digits, single use, 10 minutes) to the account's email
address, sent through the user's own mailbox. The answer is honest (200 in every case below):

- sent: `{"sent": true, "to": "l•••@•••", "expires_in_s": 600, "message": "A sign-in code is on its way to l•••@•••. It expires in 10 minutes."}`
  (the mask keeps the first character of the local part, never the domain);
- `{"sent": false, "reason_code": "no_email_address", "message": "This account has no email address, so a code can't be sent. Ask your gateway admin for a token."}`
  (also for an unknown account, a deactivated one, or one without a usable mailbox to send with);
- `{"sent": false, "reason_code": "too_many_requests", "retry_after_s": N, "message": "Too many codes requested for this account. Try again in N minutes."}`
  (3 per account and 10 per client address per 15 minutes).

Sign-in by email off (the admin's Advanced switch): 404 `recovery_off`. Trade-off: a requester can learn that an
account id has an email address; the rate limits and the audit log bound it. `purpose` defaults to `sign_in`."""


@router.post("/session/recovery/request", summary="Email me a sign-in code", description=_RECOVERY_REQUEST_DESCRIPTION)
async def session_recovery_request(request: Request, body: RecoveryRequestBody) -> Dict[str, Any]:
    from ..mail.recovery import PURPOSES, request_code

    if body.purpose not in PURPOSES:
        raise HTTPException(status_code=400, detail={"reason_code": "invalid_request", "message": "purpose must be sign_in or reset_token"})
    if not await _off_the_event_loop(mail_accounts.recovery_enabled):
        raise HTTPException(status_code=404, detail={"reason_code": "recovery_off", "message": "Sign-in by email is off on this gateway."})
    return await _off_the_event_loop(
        request_code, user_id=body.user_id, tenant_id=body.tenant_id, purpose=body.purpose, client_ip=_client_ip(request)
    )


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


# ---------------------------------------------------------------------------
# Entity mailboxes: the /accounts/{account_id}/... mirror of every /me/email and
# /me/notifications route (round 3 §3.1). Registered from the routes above so a /me route
# added later is mirrored too; `_self_plane` resolves the entity's plane from the path.
# ---------------------------------------------------------------------------

_MIRRORED_PREFIXES = ("/me/email", "/me/notifications")


def _mirror_entity_routes() -> List[str]:
    from fastapi.routing import APIRoute

    added: List[str] = []
    prefix = router.prefix
    for route in list(router.routes):
        if not isinstance(route, APIRoute) or not route.path.startswith(prefix):
            continue
        rel = route.path[len(prefix):]
        if not rel.startswith(_MIRRORED_PREFIXES):
            continue
        target = "/accounts/{account_id}" + rel[len("/me"):]
        router.add_api_route(
            target,
            route.endpoint,
            methods=sorted(route.methods or ()),
            summary=f"Entity mailbox: {route.summary or route.name}",
            description=(route.description or "")
            + "\n\nEntity mirror (round 3): acts on the mailbox of entity `account_id` (admin or the entity's creator; "
            "403 `{message}` for a user, an entity you can't manage, or an archived entity).",
            tags=["email"],
        )
        added.append(target)
    return added


ENTITY_MAILBOX_ROUTES = _mirror_entity_routes()
