"""Triggers HTTP surface, under `/api/gateway/triggers` (framework backlog 1041/1042).

- `POST /triggers/webhooks/{connection_id}`: the PUBLIC webhook ingress. No bearer token: the
  Standard Webhooks signature (`webhook-id`, `webhook-timestamp`, `webhook-signature`, HMAC-SHA256
  with the connection's secret) is the auth (`GatewaySecurityMiddleware._public_auth_path`). Rate
  limited per connection and per client address, body capped, origin allowlist not applied. An
  accepted call appends one CloudEvents-shaped event to the connection's inbox stream
  `trigger:<connection_id>` and wakes the automations bound to it.

Every non-2xx answer carries `{"detail": {"reason_code", "message"}}`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import trigger_connections as tc

router = APIRouter(prefix="/gateway/triggers", tags=["triggers"])
logger = logging.getLogger(__name__)

# The connection secret the webhook signature is checked with.
WEBHOOK_SECRET_FIELD = "secret"


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


def _record_refusal(located: Any, reason_code: str, message: str) -> None:
    """A refused, signed-for request is recorded on its connection (`last_error`)."""
    try:
        import datetime as _dt

        at = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
        located.store.record_event(located.connection.connection_id, error={"code": reason_code, "message": message, "at": at})
    except Exception:  # noqa: BLE001
        logger.warning("could not record a webhook refusal", exc_info=True)


def _accept(located: Any, *, webhook_id: str, timestamp: int, body: bytes, content_type: str) -> Dict[str, Any]:
    """Append (deduplicated) and wake the bound automations; runs off the event loop."""
    from abstractruntime.email import wake_stream_consumers

    from ..service import get_gateway_service_for_principal

    svc = get_gateway_service_for_principal(located.principal)
    runtime = svc.host.runtime
    inbox = runtime.event_inbox
    if inbox is None:
        raise RuntimeError("the connection's runtime has no event inbox (mail/runtime_wiring.wire_runtime_email)")
    stream = located.connection.stream
    # Deduplicated on the webhook id (the event's `dedupekey`): a replay appends nothing.
    result = tc.append_webhook_event(inbox, located, webhook_id=webhook_id, timestamp=timestamp, body=body, content_type=content_type)
    if result is None or getattr(result, "duplicate", False):
        raise tc.WebhookRefused(409, "webhook_replayed", "This webhook-id was already accepted for this connection.")
    woken = wake_stream_consumers(runtime, stream)
    for automation_id in woken:
        try:
            svc.runner.nudge(str(automation_id))
        except Exception:  # noqa: BLE001 - the runner's poll ticks it anyway
            pass
    return {"accepted": True, "connection_id": located.connection.connection_id, "webhook_id": webhook_id,
            "seq": int(getattr(result, "seq", 0) or 0), "woken": len(woken)}


@router.post("/webhooks/{connection_id}", status_code=202)
async def receive_webhook(connection_id: str, request: Request) -> JSONResponse:
    """Receive one signed webhook for a trigger connection (public: the signature is the auth).

    202 `{accepted, connection_id, webhook_id, seq, woken}`; 401 `webhook_unsigned` /
    `webhook_signature_invalid`; 404 `connection_not_found` (unknown, or not a webhook
    connection); 409 `connection_disabled`, `webhook_stale` (timestamp outside the 5-minute
    window), `webhook_replayed` (this webhook-id was already accepted); 413 `webhook_too_large`;
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
    secret = None
    if located is not None:
        try:
            secret = (await asyncio.to_thread(located.store.secrets, located.connection.connection_id, principal=None)).get(WEBHOOK_SECRET_FIELD)
        except Exception:  # noqa: BLE001 - an unreadable secret refuses like an unknown connection
            logger.warning("webhook connection %s: secret unreadable", connection_id, exc_info=True)
            secret = None
    if located is None or not secret:
        _audit(request, connection_id=connection_id, outcome="refused", reason_code="connection_not_found")
        return _refuse(404, "connection_not_found", "No webhook connection with this id.")
    if located.connection.status == "disabled":
        _audit(request, connection_id=connection_id, outcome="refused", reason_code="connection_disabled")
        return _refuse(409, "connection_disabled", "This webhook connection is disabled.")
    try:
        webhook_id, timestamp = tc.verify_standard_webhook(secret=str(secret), headers=request.headers, body=body)
        accepted = await asyncio.to_thread(
            _accept, located, webhook_id=webhook_id, timestamp=timestamp, body=body,
            content_type=str(request.headers.get("content-type") or ""),
        )
    except tc.WebhookRefused as refused:
        await asyncio.to_thread(_record_refusal, located, refused.reason_code, str(refused))
        _audit(request, connection_id=connection_id, outcome="refused", reason_code=refused.reason_code,
               webhook_id=str(request.headers.get("webhook-id") or "")[:200] or None)
        return _refuse(refused.status, refused.reason_code, str(refused))
    _audit(request, connection_id=connection_id, outcome="accepted", webhook_id=accepted["webhook_id"], seq=accepted["seq"])
    return JSONResponse(status_code=202, content=accepted)
