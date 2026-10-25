"""Triggers HTTP surface, under `/api/gateway/triggers` (framework backlog 1041/1042).

Connections (the caller's own; principal-scoped like the automations family, every route acts on
the caller's runtime plane; an admin also reads and revokes any account's with `?all=true` on the
list and by id on the others):

    GET    /triggers/connections[?source_id=&all=true]   {connections: [row]}
    POST   /triggers/connections                         {source_id, source_version, label?, config}
                                                         -> row + one-time `secret` (+ `ingress_url`)
    GET    /triggers/connections/{id}                    row
    PUT    /triggers/connections/{id}                    {label?, config?, status?: active|disabled, paused?, always_listen?} -> row
    DELETE /triggers/connections/{id}                    {deleted: true, connection_id}
    GET    /triggers/connections/{id}/events[?after=&limit=]  {events: [CloudEvents + sequence], next_after}
    POST   /triggers/connections/{id}/test-fire          {event_id, sequence, woken}

row = {connection_id, principal, source_id, source_version (int), label, status, listening
(listening|idle|paused), used_by (int), paused, always_listen, created_at,
updated_at, last_event_at (str|null), last_error (obj|null), config_public (object, never a
secret), secret_fields, stream, ingress_url? (webhooks)}.

- `POST /triggers/webhooks/{connection_id}`: the PUBLIC webhook ingress. No bearer token: the
  Standard Webhooks signature (`webhook-id`, `webhook-timestamp`, `webhook-signature`, HMAC-SHA256
  with the connection's secret) is the auth (`GatewaySecurityMiddleware._public_auth_path`). Rate
  limited per connection and per client address, body capped, origin allowlist not applied. An
  accepted call appends one CloudEvents-shaped event to the connection's inbox stream
  `trigger:<connection_id>` and wakes the automations bound to it.

Every non-2xx answer carries `{"detail": {"reason_code", "message", "field"?}}`: 404
`connection_not_found` (unknown, or another account's: indistinguishable), 422 `invalid_connection` /
`unknown_trigger_source`, 403 `admin_required` (`all=true` without the admin role).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .. import trigger_connections as tc

WEBHOOK_SOURCE_ID = "webhook.received"

router = APIRouter(prefix="/gateway/triggers", tags=["triggers"])
logger = logging.getLogger(__name__)

# AbstractRuntime's verdict (`append_webhook_event` reason) -> (HTTP status, wire reason_code, sentence).
# A stale timestamp is 409 like a replayed id (the request may be genuine, it is just not fresh);
# a missing or wrong signature is 401.
_REFUSALS: Dict[str, tuple] = {
    "missing_headers": (401, "webhook_unsigned", "webhook-id, webhook-timestamp and webhook-signature are required."),
    "bad_timestamp": (401, "webhook_signature_invalid", "webhook-timestamp must be Unix seconds."),
    "bad_signature": (401, "webhook_signature_invalid", "The webhook signature does not match this connection's secret."),
    "stale": (409, "webhook_stale", "webhook-timestamp is outside this connection's window (5 minutes by default) of the gateway's clock."),
    "replayed_id": (409, "webhook_replayed", "This webhook-id was already accepted for this connection: treat it as delivered."),
    "connection_disabled": (409, "connection_disabled", "This webhook connection is disabled or paused."),
    "too_large": (413, "webhook_too_large", "The body is too large."),
    "unsupported_body": (415, "webhook_unsupported_body", "The body must be JSON or UTF-8 text."),
}


def _refuse(status: int, reason_code: str, message: str, *, headers: Optional[Dict[str, str]] = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": {"reason_code": reason_code, "message": message}}, headers=headers)


def _audit(request: Request, **fields: Any) -> None:
    """The request's audit line names the webhook outcome (`webhook` {connection_id, outcome, ...})."""
    try:
        detail = getattr(request.state, "audit_detail", None)
        detail = dict(detail) if isinstance(detail, dict) else {}
        detail["webhook"] = {k: v for k, v in fields.items() if v is not None}
        request.state.audit_detail = detail
    except Exception:  # noqa: BLE001 - auditing never breaks the ingress
        pass


async def _read_capped(request: Request, cap: int) -> Optional[bytes]:
    """The whole body, or None when it exceeds `cap` bytes (stops reading at the cap)."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.strip().isdigit() and int(declared) > cap:
        return None
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > cap:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _accept(located: Any, *, body: bytes, headers: Dict[str, str]) -> Dict[str, Any]:
    """Verify + append through AbstractRuntime (`append_webhook_event`: Standard Webhooks, deduplicated
    on the webhook id, refusals recorded on the connection), then wake the bound automations. Runs
    off the event loop. Returns the runtime's verdict dict plus `woken`."""
    from abstractruntime.email import wake_stream_consumers
    from abstractruntime.triggers import append_webhook_event

    from ..service import get_gateway_service_for_principal

    svc = get_gateway_service_for_principal(located.principal)
    runtime = svc.host.runtime
    inbox = runtime.event_inbox
    if inbox is None:
        raise RuntimeError("the connection's runtime has no event inbox (mail/runtime_wiring.wire_runtime_email)")
    verdict = append_webhook_event(inbox, located.connection, body, headers, store=located.store, max_body_bytes=tc.WEBHOOK_MAX_BODY_BYTES)
    if verdict.get("status") != "appended":
        return dict(verdict)
    woken = wake_stream_consumers(runtime, located.connection.stream)
    for automation_id in woken:
        try:
            svc.runner.nudge(str(automation_id))
        except Exception:  # noqa: BLE001 - the runner's poll ticks it anyway
            pass
    return {**verdict, "woken": len(woken)}


@router.post("/webhooks/{connection_id}", status_code=202)
async def receive_webhook(connection_id: str, request: Request) -> JSONResponse:
    """Receive one signed webhook for a trigger connection (public: the signature is the auth).

    Verification and the append are AbstractRuntime's (`abstractruntime.triggers.append_webhook_event`).
    202 `{accepted, connection_id, webhook_id, event_id, seq, woken}`; 401 `webhook_unsigned` /
    `webhook_signature_invalid`; 404 `connection_not_found` (unknown, or not a webhook
    connection); 409 `connection_disabled` (disabled or paused; an idle connection accepts the call
    and keeps the event), `webhook_stale` (timestamp outside the connection's
    window, 5 minutes by default), `webhook_replayed` (this webhook-id was already accepted: the
    sender should treat it as delivered); 413 `webhook_too_large`; 415 `webhook_unsupported_body`;
    429 `webhook_rate_limited` (Retry-After)."""
    address = str(getattr(getattr(request, "client", None), "host", "") or "")
    wait = tc.webhook_rate_limiter.check(connection_id=str(connection_id), address=address)
    if wait is not None:
        _audit(request, connection_id=connection_id, outcome="refused", reason_code="webhook_rate_limited")
        return _refuse(429, "webhook_rate_limited", "Too many webhook calls; retry later.", headers={"Retry-After": str(wait)})
    body = await _read_capped(request, tc.WEBHOOK_MAX_BODY_BYTES)
    if body is None:
        _audit(request, connection_id=connection_id, outcome="refused", reason_code="webhook_too_large")
        return _refuse(413, "webhook_too_large", f"The body exceeds {tc.WEBHOOK_MAX_BODY_BYTES} bytes.")
    located = await asyncio.to_thread(tc.locate_connection, str(connection_id))
    if located is None or located.connection.source_id != WEBHOOK_SOURCE_ID:
        _audit(request, connection_id=connection_id, outcome="refused", reason_code="connection_not_found")
        return _refuse(404, "connection_not_found", "No webhook connection with this id.")
    headers = {k.lower(): v for k, v in request.headers.items()}
    verdict = await asyncio.to_thread(_accept, located, body=body, headers=headers)
    if verdict.get("status") != "appended":
        status, reason_code, message = _REFUSALS.get(
            str(verdict.get("reason") or ""), (int(verdict.get("http_status") or 400), f"webhook_{verdict.get('reason')}", "The webhook was refused.")
        )
        _audit(request, connection_id=connection_id, outcome="refused", reason_code=reason_code,
               webhook_id=str(verdict.get("webhook_id") or "")[:200] or None)
        return _refuse(status, reason_code, message)
    accepted = {"accepted": True, "connection_id": located.connection.connection_id, "webhook_id": verdict.get("webhook_id"),
                "event_id": verdict.get("event_id"), "seq": int(verdict.get("seq") or 0), "woken": int(verdict.get("woken") or 0)}
    _audit(request, connection_id=connection_id, outcome="accepted", webhook_id=accepted["webhook_id"], seq=accepted["seq"])
    return JSONResponse(status_code=202, content=accepted)


# ---------------------------------------------------------------------------------------
# Connections (framework backlog 1041)
# ---------------------------------------------------------------------------------------

_CONNECTION_ERROR_STATUS = {
    "connection_not_found": 404,
    "invalid_connection": 422,
    "unknown_trigger_source": 422,
    "connection_required": 422,
    "connection_source_mismatch": 422,
    "connection_disabled": 409,
}
EVENTS_LIMIT_MAX = 500


class CreateConnectionBody(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"source_id": "webhook.received", "source_version": 1, "label": "Orders from the shop", "config": {}},
                {"source_id": "feed.item", "source_version": 1, "label": "Python blog", "config": {"url": "https://blog.python.org/feeds/posts/default"}},
            ]
        },
    )

    source_id: str = Field(..., description="A trigger source whose descriptor says needs_connection (GET /trigger-sources)")
    source_version: int = Field(..., ge=1)
    label: Optional[str] = Field(None, description="One line, at most 120 characters (default: the source's label)")
    config: Dict[str, Any] = Field(default_factory=dict, description="Validated against the source's connection_schema; x-secret fields are sealed")


class UpdateConnectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: Optional[str] = None
    config: Optional[Dict[str, Any]] = Field(None, description="Replaces the config (validated whole); secret fields left out keep their sealed value")
    status: Optional[str] = Field(None, description="active | disabled")
    paused: Optional[StrictBool] = Field(None, description="Stop feeding this connection whatever listens (feeds are not fetched; webhooks answer 409)")
    always_listen: Optional[StrictBool] = Field(None, description="Feed it even while no automation listens (events show under Events)")


def _principal(request: Request) -> Any:
    from .gateway import _principal_from_request

    return _principal_from_request(request)


def _connection_error(exc: Any) -> JSONResponse:
    detail = exc.to_error() if hasattr(exc, "to_error") else {"reason_code": "invalid_connection", "message": str(exc)}
    return JSONResponse(status_code=_CONNECTION_ERROR_STATUS.get(detail.get("reason_code"), 422), content={"detail": detail})


def _own_store(principal: Any) -> tuple:
    """(service, store) of the caller's runtime plane."""
    from ..service import get_gateway_service_for_principal

    svc = get_gateway_service_for_principal(principal)
    return svc, tc.store_for_service(svc)


def ingress_url(request: Request, connection_id: str) -> str:
    """The absolute address a sender posts to (the request's own base URL)."""
    return f"{str(request.base_url).rstrip('/')}/api/gateway/triggers/webhooks/{connection_id}"


def connection_row(conn: Any, request: Request, runtime: Any) -> Dict[str, Any]:
    """The one row shape (console page, Flow's connection selector, the TUI): never a secret.
    `listening` ("listening" | "idle" | "paused") and `used_by` (active automations reading the
    stream) are AbstractRuntime's `connection_usage` on the plane's runtime: a connection is fed
    only while something listens (or `always_listen`); `paused` stops it whatever listens."""
    from abstractruntime.triggers.connections import connection_usage

    usage = connection_usage(runtime, conn)
    row: Dict[str, Any] = {
        "connection_id": conn.connection_id,
        "principal": conn.principal,
        "source_id": conn.source_id,
        "source_version": int(conn.source_version),
        "label": str(conn.label or "") or conn.connection_id,
        "status": str(conn.status or "active"),
        "listening": str(usage["listening"]),
        "used_by": int(usage["used_by"]),
        "paused": bool(getattr(conn, "paused", False)),
        "always_listen": bool(getattr(conn, "always_listen", False)),
        "created_at": conn.created_at or None,
        "updated_at": conn.updated_at or None,
        "last_event_at": conn.last_event_at or None,
        "last_error": dict(conn.last_error) if isinstance(conn.last_error, dict) else None,
        "config_public": dict(conn.config or {}),
        "secret_fields": list(conn.secret_fields or []),
        "stream": conn.stream,
    }
    if conn.source_id == WEBHOOK_SOURCE_ID:
        row["ingress_url"] = ingress_url(request, conn.connection_id)
    return row


def _resolve(principal: Any, connection_id: str) -> tuple:
    """(service, store, connection) for the caller: their own connection; an admin also any
    account's (found across the planes). None when unknown or not theirs (404)."""
    svc, store = _own_store(principal)
    conn = store.get(str(connection_id), principal=tc.principal_key(principal))
    if conn is not None:
        return svc, store, conn
    if principal.is_admin():
        located = tc.locate_connection(str(connection_id))
        if located is not None:
            from ..service import get_gateway_service_for_principal

            return get_gateway_service_for_principal(located.principal), located.store, located.connection
    return None


def _not_found(connection_id: str) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": {"reason_code": "connection_not_found",
                                                             "message": f"No trigger connection {connection_id!r} for this account.",
                                                             "field": "connection_id"}})


def _all_connections(source_id: Optional[str]) -> list:
    """`[(connection, its plane's runtime)]` across every account's plane, oldest first."""
    from ..service import get_gateway_service_for_principal

    out, seen = [], set()
    for principal_of_plane, data_dir in tc._candidate_planes():
        conns = tc.store_for_data_dir(data_dir).list(source_id=source_id)
        if not conns:
            continue
        runtime = get_gateway_service_for_principal(principal_of_plane).host.runtime
        for conn in conns:
            if conn.connection_id not in seen:
                seen.add(conn.connection_id)
                out.append((conn, runtime))
    return sorted(out, key=lambda pair: (pair[0].created_at, pair[0].connection_id))


@router.get("/connections", summary="My trigger connections (an admin: every account's with all=true)")
async def list_connections(
    request: Request,
    source_id: Optional[str] = Query(None, description="Only the connections of this source id"),
    all: bool = Query(False, description="Admin only: every account's connections (rows carry `principal`)"),  # noqa: A002 - the wire name
) -> Any:
    principal = _principal(request)
    if all and not principal.is_admin():
        return JSONResponse(status_code=403, content={"detail": {"reason_code": "admin_required",
                                                                 "message": "Only an administrator lists every account's connections."}})

    def run() -> Dict[str, Any]:
        if all:
            pairs = _all_connections(source_id)
        else:
            svc, store = _own_store(principal)
            pairs = [(c, svc.host.runtime) for c in store.list(principal=tc.principal_key(principal), source_id=source_id)]
        return {"connections": [connection_row(c, request, runtime) for c, runtime in pairs]}

    return await asyncio.to_thread(run)


@router.post("/connections", status_code=201, summary="Create a trigger connection (the secret is shown once)")
async def create_connection(request: Request, body: CreateConnectionBody) -> Any:
    """201: the connection row at top level, plus the one-time `secret` (a webhook's signing
    secret, generated when the config gives none; never shown again) and, for a webhook,
    `ingress_url`."""
    principal = _principal(request)
    from abstractruntime.triggers.connections import TriggerConnectionError

    def run() -> Any:
        svc, store = _own_store(principal)
        try:
            conn = store.create(principal=tc.principal_key(principal), source_id=body.source_id, source_version=int(body.source_version),
                                config=dict(body.config or {}), label=body.label)
            secrets = store.secrets(conn.connection_id, principal=tc.principal_key(principal)) if conn.secret_ref else {}
        except TriggerConnectionError as exc:
            return _connection_error(exc)
        out = connection_row(conn, request, svc.host.runtime)
        if isinstance(secrets.get("secret"), str):
            out["secret"] = secrets["secret"]
        return JSONResponse(status_code=201, content=out)

    return await asyncio.to_thread(run)


@router.get("/connections/{connection_id}", summary="One trigger connection")
async def get_connection(request: Request, connection_id: str) -> Any:
    principal = _principal(request)
    found = await asyncio.to_thread(_resolve, principal, connection_id)
    return _not_found(connection_id) if found is None else connection_row(found[2], request, found[0].host.runtime)


@router.put("/connections/{connection_id}", summary="Change a trigger connection's label, config or status")
async def update_connection(request: Request, connection_id: str, body: UpdateConnectionBody) -> Any:
    principal = _principal(request)
    from abstractruntime.triggers.connections import TriggerConnectionError

    def run() -> Any:
        found = _resolve(principal, connection_id)
        if found is None:
            return _not_found(connection_id)
        svc, store, conn = found
        if body.status is not None and body.status not in ("active", "disabled"):
            return JSONResponse(status_code=422, content={"detail": {"reason_code": "invalid_connection",
                                                                     "message": "status must be active or disabled", "field": "status"}})
        try:
            conn = store.update(conn.connection_id, principal=conn.principal, config=body.config, label=body.label, status=body.status,
                                paused=body.paused, always_listen=body.always_listen)
        except TriggerConnectionError as exc:
            return _connection_error(exc)
        return connection_row(conn, request, svc.host.runtime)

    return await asyncio.to_thread(run)


@router.delete("/connections/{connection_id}", summary="Delete a trigger connection (its past events stay until retention)")
async def delete_connection(request: Request, connection_id: str) -> Any:
    principal = _principal(request)

    def run() -> Any:
        found = _resolve(principal, connection_id)
        if found is None:
            return _not_found(connection_id)
        _svc, store, conn = found
        store.delete(conn.connection_id, principal=conn.principal)
        return {"deleted": True, "connection_id": conn.connection_id}

    return await asyncio.to_thread(run)


def _event_item(rec: Dict[str, Any]) -> Dict[str, Any]:
    payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
    return {**payload, "sequence": int(rec.get("seq") or 0), "recordedtime": rec.get("appended_at")}


@router.get("/connections/{connection_id}/events", summary="A connection's recent events")
async def connection_events(
    request: Request,
    connection_id: str,
    after: Optional[int] = Query(None, ge=0, description="Events after this sequence, oldest first; omitted = the newest `limit`"),
    limit: int = Query(50, ge=1, le=EVENTS_LIMIT_MAX),
) -> Any:
    """`{events: [CloudEvents JSON + sequence + recordedtime], next_after}`: the connection's inbox
    stream (`trigger:<id>`). Without `after`: the newest `limit` events (oldest first); with it,
    the next `limit` after that sequence; `next_after` is the last sequence returned (page on)."""
    principal = _principal(request)

    def run() -> Any:
        found = _resolve(principal, connection_id)
        if found is None:
            return _not_found(connection_id)
        svc, _store, conn = found
        inbox = svc.host.runtime.event_inbox
        if inbox is None:
            raise RuntimeError("the connection's runtime has no event inbox")
        if after is None:
            records = inbox.read(stream=conn.stream)[-int(limit):]
        else:
            records = inbox.read(after_seq=int(after), stream=conn.stream, limit=int(limit))
        events = [_event_item(r) for r in records]
        return {"events": events, "next_after": events[-1]["sequence"] if events else after}

    return await asyncio.to_thread(run)


@router.post("/connections/{connection_id}/test-fire", summary="Send a test event to a connection")
async def connection_test_fire(request: Request, connection_id: str) -> Any:
    """Appends a synthetic `{data: {test: true}}` event to the connection's stream, exactly like a
    real one (bound automations admit it), and wakes them: `{event_id, sequence, woken}`."""
    principal = _principal(request)
    from abstractruntime.email import wake_stream_consumers
    from abstractruntime.triggers.connections import TriggerConnectionError, test_fire

    def run() -> Any:
        found = _resolve(principal, connection_id)
        if found is None:
            return _not_found(connection_id)
        svc, store, conn = found
        runtime = svc.host.runtime
        try:
            result = test_fire(store, runtime.event_inbox, conn.connection_id, principal=conn.principal)
        except TriggerConnectionError as exc:
            return _connection_error(exc)
        woken = wake_stream_consumers(runtime, conn.stream)
        for automation_id in woken:
            try:
                svc.runner.nudge(str(automation_id))
            except Exception:  # noqa: BLE001
                pass
        return {"event_id": result.event_id, "sequence": int(result.seq), "woken": len(woken)}

    return await asyncio.to_thread(run)


# ---------------------------------------------------------------------------------------
# Sample inputs: run a triggered flow as if its trigger fired (Flow's Run dialog)
# ---------------------------------------------------------------------------------------

SAMPLE_COUNT_MAX = 500
SAMPLE_CAP_S = 20.0
_SAMPLE_ERROR_STATUS = {"invalid_sample": 422, "connection_not_found": 404, "email_not_configured": 409, "sample_unavailable": 503}


class SampleBody(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"trigger": {"source_id": "email.received", "source_version": 1, "config": {"account": "self"}}, "count": 100}]},
    )

    trigger: Dict[str, Any] = Field(..., description="{source_id, source_version, config}: the binding an automation would use")
    count: int = Field(100, ge=1, le=SAMPLE_COUNT_MAX, description="How many items (messages, feed items, webhook calls), newest first")


def _sample_summary(svc: Any, source_id: str, inputs: Dict[str, Any], *, count: int, truncated: bool, trigger: Dict[str, Any]) -> str:
    """One sentence saying what the sample holds ("Last 100 messages of test@abstractframework.ai")."""
    if source_id == "email.received":
        mailbox = str(inputs.get("mailbox") or "")
        if "@" not in mailbox:
            from ..mail.accounts import DEFAULT_ACCOUNT_ID, account_store, plane_for_service_config

            account = str((trigger.get("config") or {}).get("account") or "").strip()
            plane = plane_for_service_config(svc.config)
            plane = plane.primary if account in ("", "self", DEFAULT_ACCOUNT_ID) else plane.for_account(account)
            try:
                acct = account_store(plane).settings().account
                mailbox = str(getattr(acct, "address", "") or mailbox)
            except Exception:  # noqa: BLE001 - the sentence falls back to the mailbox id
                pass
        text = f"Last {count} message{'s' if count != 1 else ''} of {mailbox or 'your mailbox'}"
    elif source_id == "feed.item":
        feed = inputs.get("feed") if isinstance(inputs.get("feed"), dict) else {}
        text = f"Latest {count} item{'s' if count != 1 else ''} of {feed.get('title') or feed.get('url') or 'the feed'}"
    elif source_id == "webhook.received":
        events = ((inputs.get("event") or {}).get("data") or {}).get("events") or []
        if events and events[0].get("sample_call"):
            text = "A sample call (this connection has not received one yet)"
        else:
            text = f"Last {count} call{'s' if count != 1 else ''} received by this webhook"
    elif source_id == "schedule":
        text = "One occurrence, as if the schedule fired now"
    else:
        text = "One run, as if it was started by hand"
    return text + (f" (stopped after {int(SAMPLE_CAP_S)} s)" if truncated else "")


def _sample(svc: Any, principal: Any, body: SampleBody) -> Any:
    from abstractruntime.automations.samples import SampleError, resolved_sample_inputs, sample_occurrence_inputs

    from .automations import _check_email_account, _check_trigger_connection
    from ..automation_errors import AutomationError

    trigger = dict(body.trigger or {})
    source_id, version = trigger.get("source_id"), trigger.get("source_version")
    if not isinstance(source_id, str) or not source_id.strip() or isinstance(version, bool) or not isinstance(version, int):
        return JSONResponse(status_code=422, content={"detail": {"reason_code": "invalid_sample",
                                                                 "message": "trigger needs source_id (string) and source_version (integer)", "field": "trigger"}})
    try:
        # The same host checks as an automation's creation: the mailbox and the connection must be
        # the caller's (an unknown or foreign one answers 404).
        _check_email_account(svc, trigger, field="trigger.config.account")
        trigger = _check_trigger_connection(svc, principal, trigger, from_defaults=False, field="trigger.connection_id")
    except AutomationError as e:
        detail = dict(e.detail) if isinstance(e.detail, dict) else {"reason_code": "invalid_sample", "message": str(e.detail)}
        status = 404 if detail.get("reason_code") in ("email_account_not_found", "connection_not_found") else e.status_code
        return JSONResponse(status_code=status, content={"detail": detail})
    runtime = svc.host.runtime
    try:
        sample = sample_occurrence_inputs(runtime, source_id, int(version), dict(trigger.get("config") or {}), count=int(body.count),
                                          principal=tc.principal_key(principal), email_budget_s=SAMPLE_CAP_S)
        inputs = resolved_sample_inputs(sample, artifact_store=getattr(runtime, "artifact_store", None))
    except SampleError as e:
        return JSONResponse(status_code=_SAMPLE_ERROR_STATUS.get(e.reason_code, 422), content={"detail": {"reason_code": e.reason_code, "message": str(e)}})
    count = int(sample.get("count") or 0)
    return {
        "inputs": inputs,
        "truncated": bool(sample.get("truncated")),
        "count": count,
        "source": sample.get("source"),
        "summary": _sample_summary(svc, source_id, inputs, count=count, truncated=bool(sample.get("truncated")), trigger=trigger),
    }


@router.post("/sample", summary="The inputs a triggered flow would receive, from real data (Run as if the trigger fired)")
async def sample_trigger_inputs(request: Request, body: SampleBody) -> Any:
    """`{inputs, truncated, count, source, summary}`: the flat named inputs plus `event` that a real
    occurrence of `trigger` would receive, built read-only from the caller's own data (AbstractRuntime's
    `sample_occurrence_inputs`): the newest `count` messages of the bound mailbox (its filters applied;
    no cursor moves, nothing is marked read), the latest feed items of the connection, the last webhook
    calls stored for it (or one documented sample call), `scheduled_at` = now. A mailbox is read for
    at most 20 s (`truncated: true` then). Nothing is stored except the artifacts holding message
    bodies. 404 `email_account_not_found` / `connection_not_found` (not the caller's), 409
    `email_not_configured`, 422 `invalid_sample`, 503 `sample_unavailable`, 504 `sample_timeout`."""
    from ..service import get_gateway_service

    principal = _principal(request)
    svc = get_gateway_service()
    try:
        return await asyncio.wait_for(asyncio.to_thread(_sample, svc, principal, body), timeout=SAMPLE_CAP_S + 10.0)
    except asyncio.TimeoutError:
        return JSONResponse(status_code=504, content={"detail": {"reason_code": "sample_timeout",
                                                                 "message": "Reading the sample took too long; try a smaller count."}})
