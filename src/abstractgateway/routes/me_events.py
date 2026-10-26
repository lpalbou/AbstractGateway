"""The caller's own event stream (framework backlog 1046): catch-up and live tail.

    GET /api/gateway/me/events?after=<seq>&limit=<n>&channel=<a,b>   catch-up page (JSON)
    GET /api/gateway/me/events/stream?after=<seq>&channel=<a,b>      live tail (SSE)
    GET /api/gateway/me/events/channels                              the channels seen, with counts
    POST /api/gateway/me/events/notifications                        a client reports a notice it showed
    POST /api/gateway/me/events/seen                                 a published alert was seen

Principal-scoped by construction: the stream is the one of the request's principal in the plane
its service serves (`events_stream.stream_for(data_dir, tenant, user)`), so no parameter can name
another person's stream. `after` is the last `sequence` the client consumed (0 = from the start),
the same cursor semantics as `/runs/{run_id}/ledger/stream`; the SSE `id:` of each event is its
`sequence`, and `Last-Event-ID` is honoured when `after` is absent or 0.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..work_pools import off_loop
from ..events_stream import UserEventStream, event_channel, parse_channels, record_notification, record_publish_seen, stream_for

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/gateway/me", tags=["events"])

PAGE_MAX = 1000
# Cross-process fallback: an append made by ANOTHER process (split runner) is seen by this
# stat-only poll; in-process appends wake the stream at once.
POLL_S = 1.0
BATCH = 200


def _my_stream(request: Request) -> UserEventStream:
    from ..service import get_gateway_service
    from .gateway import _principal_from_request

    principal = _principal_from_request(request)
    svc = get_gateway_service()
    try:
        return stream_for(svc.config.data_dir, principal.tenant_id, principal.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def _channels(channel: Optional[str]):
    try:
        return parse_channels(channel)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def resolve_start(after: int, last_event_id: Optional[str]) -> int:
    """`after` wins when set; else `Last-Event-ID` (the last `id:` the client saw); else 0."""
    start = max(0, int(after or 0))
    if start <= 0:
        raw = str(last_event_id or "").strip()
        if raw:
            try:
                start = max(0, int(raw))
            except ValueError:
                start = 0
    return start


@router.get("/events")
async def list_my_events(
    request: Request,
    after: int = Query(0, ge=0, description="The last sequence already consumed (0 = from the start)."),
    limit: int = Query(100, ge=1, le=PAGE_MAX),
    channel: Optional[str] = Query(None, description="Only these channels (comma-separated); `attention` = attention.* events."),
) -> Dict[str, Any]:
    """A page of the caller's events with `sequence > after`, oldest first, optionally only on some
    channels (`channel=a,b`; `attention` is the channel of attention.* events).

    `next_after` is the cursor to send next (past events of other channels); `head` the newest
    sequence; `more` whether events past this page may exist."""
    stream = _my_stream(request)
    channels = _channels(channel)
    events, next_after = await off_loop("light", stream.read_filtered, after=after, limit=limit, channels=channels)
    head = await off_loop("light", stream.head)
    out: Dict[str, Any] = {
        "stream": stream.name,
        "events": events,
        "next_after": int(next_after),
        "head": head,
        "more": head > int(next_after) and len(events) >= limit,
    }
    if channels:
        out["channels"] = sorted(channels)
    return out


@router.get("/events/channels")
async def list_my_channels(request: Request) -> Dict[str, Any]:
    """The channels in the caller's retained stream: `[{channel, count, last_time, last_sequence}]`,
    most recently used first (`attention` included when attention events exist)."""
    stream = _my_stream(request)
    return {"stream": stream.name, "channels": await off_loop("light", stream.channels)}


def _frame(event: Dict[str, Any]) -> bytes:
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"id: {int(event['sequence'])}\nevent: {event['type']}\ndata: {data}\n\n".encode("utf-8")


async def sse_frames(
    stream: UserEventStream,
    *,
    after: int,
    heartbeat_s: float,
    disconnected: Callable[[], Awaitable[bool]],
    poll_s: float = POLL_S,
    channels: Optional[frozenset] = None,
) -> AsyncIterator[bytes]:
    """Catch-up from `after`, then live events, `: keep-alive` comments every `heartbeat_s`.
    Never ends on its own (the client closes it)."""
    loop = asyncio.get_running_loop()
    dirty = asyncio.Event()
    dirty.set()  # the first pass is the catch-up

    def _on_append() -> None:  # writer thread: one flag on the loop, never a read
        try:
            loop.call_soon_threadsafe(dirty.set)
        except RuntimeError:
            pass

    off = stream.add_listener(_on_append)
    cursor = int(after)
    token = None
    last_emit = loop.time()
    try:
        yield b": stream " + stream.name.encode("utf-8") + b"\n\n"
        while True:
            if await disconnected():
                return
            if dirty.is_set():
                dirty.clear()
                token = await off_loop("light", stream.change_token)
                events = await off_loop("light", stream.read, after=cursor, limit=BATCH)
                if events:
                    cursor = int(events[-1]["sequence"])
                    shown = [e for e in events if not channels or event_channel(e) in channels]
                    if shown:
                        yield b"".join(_frame(e) for e in shown)
                        last_emit = loop.time()
                    if len(events) >= BATCH:
                        dirty.set()
                        continue
            now = loop.time()
            if (now - last_emit) >= float(heartbeat_s):
                yield b": keep-alive\n\n"
                last_emit = now
            try:
                await asyncio.wait_for(dirty.wait(), timeout=min(float(poll_s), float(heartbeat_s)))
            except asyncio.TimeoutError:
                # Another process may have appended: one stat decides whether to read.
                if await off_loop("light", stream.change_token) != token:
                    dirty.set()
    finally:
        off()


@router.get("/events/stream")
async def stream_my_events(
    request: Request,
    after: int = Query(0, ge=0, description="The last sequence already consumed; Last-Event-ID is used when 0."),
    heartbeat_s: float = Query(15.0, gt=0.1, le=60.0),
    channel: Optional[str] = Query(None, description="Only these channels (comma-separated); `attention` = attention.* events."),
) -> StreamingResponse:
    """The caller's events as server-sent events: the catch-up from `after`, then each new event.

    Frames: `id: <sequence>`, `event: <type>`, `data: <CloudEvent JSON>`; a `: keep-alive`
    comment every `heartbeat_s`. A reconnecting client sends `Last-Event-ID` (used when `after`
    is 0) and receives exactly the events after it. Same cursor semantics as
    `/runs/{run_id}/ledger/stream`; the stream never ends on its own."""
    stream = _my_stream(request)
    channels = _channels(channel)
    start = resolve_start(after, request.headers.get("last-event-id"))
    return StreamingResponse(
        sse_frames(stream, after=start, heartbeat_s=heartbeat_s, disconnected=request.is_disconnected, channels=channels),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class NotificationReport(BaseModel):
    channel: Literal["tray", "assistant", "code", "watch", "phone"] = Field(
        ..., description="The client channel that showed (or failed to show) the notice."
    )
    outcome: Literal["sent", "failed"]
    record_key: str = Field(..., min_length=1, max_length=400, description="The attention record the notice was about.")
    device: Optional[str] = Field(
        None, min_length=1, max_length=200,
        description="An opaque id of the machine the notice was shown on (the dedupe rule compares it).",
    )
    error: Optional[str] = Field(None, max_length=400)


@router.post("/events/notifications")
async def report_notification(request: Request, body: NotificationReport) -> Dict[str, Any]:
    """A client channel (tray, assistant, code, watch, phone) records the notice it showed as
    `notification.sent|failed {channel, notice, record_key, device?}` in the caller's own stream.
    Idempotent per (channel, record_key). Dedupe rule for apps: before notifying, stay silent when
    a `notification.sent` for the record from another channel on the same device is under a
    minute old (the tray is the default notifier)."""
    stream = _my_stream(request)
    data: Dict[str, Any] = {"record_key": body.record_key}
    if body.device:
        data["device"] = body.device
    if body.error:
        data["error"] = body.error
    created = await off_loop("light",
        record_notification, stream, body.outcome, channel=body.channel, notice=f"attention:{body.record_key}", data=data
    )
    return {"ok": True, "created": bool(created)}


class SeenReport(BaseModel):
    record_key: str = Field(..., min_length=1, max_length=400, description="A published alert's record_key (publish:<run_id>:<step_id>).")


@router.post("/events/seen")
async def mark_published_seen(request: Request, body: SeenReport) -> Dict[str, Any]:
    """Mark a published alert seen: `attention.seen {record_key}` in the caller's own stream (the
    badge drops it). Automation items keep `POST /automations/{id}/seen`; waits end when answered."""
    stream = _my_stream(request)
    try:
        created = await off_loop("light", record_publish_seen, stream, record_key=body.record_key)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True, "created": bool(created)}


__all__ = ["resolve_start", "router", "sse_frames"]
