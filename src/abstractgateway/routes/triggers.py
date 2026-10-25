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
    "connection_disabled": (409, "connection_disabled", "This webhook connection is disabled."),
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
    connection); 409 `connection_disabled`, `webhook_stale` (timestamp outside the connection's
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
