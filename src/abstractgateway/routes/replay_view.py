"""The replay view of a run: one reconstruction, every client (framework backlog 0993).

    GET /api/gateway/runs/{run_id}/view           the whole view (JSON), ETag = its cursor token
    GET /api/gateway/runs/{run_id}/view/stream    SSE: `view`, then `delta` per fold, `done`

The view is AbstractRuntime's (`abstractruntime.replay`, ADR-0009): the run tree's status, the
person-facing interaction (a tool approval or a question, wherever in the tree it waits),
messages, activities, artifacts and usage, with storage encodings (`$slim`, windows) resolved.
A client renders it and acts through `POST /api/gateway/commands` (`interaction.command` is
the resume to send) and `/runs/start`; it never re-derives run state from raw records.

Authorization is the rule of `GET /runs/{run_id}`: the caller's own plane (its service's
stores); a run that plane does not hold is 404. The cache and fan-out live in
`abstractgateway.replay_view`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse

from ..replay_view import _Subscriber, followed, get_view_hub, poll, read_view, read_view_at, subscribe, unsubscribe
from ..work_pools import off_loop

router = APIRouter(prefix="/gateway", tags=["runs"])

# Where an append can come from another process (the split API process) or no observable store
# exists, the stream checks for new records this often (`list_after`: no new bytes = no parse).
CROSS_PROCESS_POLL_S = 0.25
# Otherwise appends wake the stream directly; this slow check only re-reads open runs' snapshots
# (a legacy ledger without terminal records, a wait saved without a record).
SAFETY_POLL_S = 5.0


async def _entry_for(run_id: str):
    from ..service import get_gateway_service

    svc = get_gateway_service()
    rs = svc.host.run_store
    try:
        run = await off_loop("light", rs.load, str(run_id))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load run: {e}")
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    entry = get_view_hub().entry(ledger_store=svc.host.ledger_store, run_store=rs, root_run_id=str(run_id))
    return svc, entry


def _etag(token: str) -> str:
    return f'"{token}"'


@router.get("/runs/{run_id}/view")
async def get_run_view(
    request: Request,
    run_id: str,
    at: Optional[str] = Query(None, description="A cursor token (a delta's id, a push's cursor): the view as the ledger stood then."),
) -> Response:
    """The run tree's replay view (`replay_view_v1`). `If-None-Match` with the ETag -> 304.

    `?at=<cursor token>`: the view at that cursor (the records up to each run's seq in the
    token; the runs' status and waits as those records say); 404
    when the token is not one of this run's."""
    _svc, entry = await _entry_for(run_id)
    if at is not None and at.strip():
        body_at = await off_loop("heavy", read_view_at, entry, at.strip())
        if body_at is None:
            raise HTTPException(status_code=404, detail="No view of this run at that cursor")
        return Response(content=body_at, media_type="application/json",
                        headers={"ETag": _etag("at." + at.strip()), "Cache-Control": "no-cache"})
    token, body, _delta = await off_loop("heavy", read_view, entry)
    etag = _etag(token)
    inm = [t.strip() for t in str(request.headers.get("if-none-match") or "").split(",") if t.strip()]
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if etag in inm or f"W/{etag}" in inm:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


@router.get("/runs/{run_id}/view/stream")
async def stream_run_view(
    request: Request,
    run_id: str,
    after: Optional[str] = Query(None, description="A cursor token the client holds (as Last-Event-ID)."),
    heartbeat_s: float = Query(5.0, gt=0.1, le=60.0),
) -> StreamingResponse:
    """SSE: `event: view` (the whole view), then `event: delta` per fold (`id:` = cursor token),
    `: keep-alive` comments, and `event: done` once the run tree is terminal and drained.

    Reconnect with `Last-Event-ID` (or `?after=`): the same token sends nothing until the next
    change; an older token of this view sends one `delta` from there; anything else, `view`."""
    _svc, entry = await _entry_for(run_id)
    last_token = (after or "").strip() or str(request.headers.get("last-event-id") or "").strip() or None

    async def _gen() -> AsyncIterator[bytes]:
        loop = asyncio.get_running_loop()
        sub = _Subscriber(loop=loop)
        # Registers the stream; the first one also subscribes the ENTRY to the ledger's appends,
        # so an append to the tree is folded once (the entry's pump) and reaches every stream
        # as the same frame. A stream never folds on its own except the checks below.
        frames, terminal = await off_loop("heavy", subscribe, entry, sub, last_token)
        try:
            from ..split_mode import is_api_process

            cross_process = bool(is_api_process()) or not followed(entry)
        except Exception:  # noqa: BLE001
            cross_process = not followed(entry)
        interval = CROSS_PROCESS_POLL_S if cross_process else SAFETY_POLL_S
        try:
            for f in frames:
                yield f
            last_emit = loop.time()
            last_check = last_emit
            last_safety = last_emit
            while True:
                if await request.is_disconnected():
                    return
                sub.wake.clear()
                sent = False
                while not sub.queue.empty():  # frames folded by the pump or by any request
                    yield sub.queue.get_nowait()
                    sent = True
                if sent:
                    last_emit = loop.time()
                if terminal or entry.terminal:
                    # One final drain (the terminal record can precede the last appends).
                    folded, terminal = await off_loop("heavy", poll, entry)
                    if not folded and sub.queue.empty():
                        yield b'event: done\ndata: ' + json.dumps({"run_id": str(run_id), "status": "terminal"}).encode() + b"\n\n"
                        return
                    continue
                if sub.queue.empty():
                    now = loop.time()
                    timeout = min(heartbeat_s - (now - last_emit), interval - (now - last_check))
                    if timeout > 0:
                        try:
                            await asyncio.wait_for(sub.wake.wait(), timeout=max(0.02, timeout))
                        except asyncio.TimeoutError:
                            pass
                now = loop.time()
                if now - last_check >= interval:
                    # Cross-process appends (no wake-up reaches us): `list_after`, no bytes read
                    # when nothing was appended. Otherwise the slow safety check re-reads open
                    # runs' snapshots (a legacy ledger without terminal records).
                    last_check = now
                    safety_due = now - last_safety >= SAFETY_POLL_S
                    if safety_due:
                        last_safety = now
                    _folded, terminal = await off_loop(
                        "heavy", poll, entry, reload_open_snapshots=safety_due,
                        check_every=interval * 0.8, reload_every=SAFETY_POLL_S * 0.8)
                if sub.queue.empty() and loop.time() - last_emit >= heartbeat_s:
                    yield b": keep-alive\n\n"
                    last_emit = loop.time()
        finally:
            await off_loop("light", unsubscribe, entry, sub)

    return StreamingResponse(_gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

