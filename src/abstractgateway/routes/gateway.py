"""Run Gateway API (HTTP + SSE).

Backlog 307: Durable Run Gateway (Command Inbox + Ledger Stream)

This is intentionally replay-first:
- The durable ledger is the source of truth.
- SSE is an optimization; clients must be able to reconnect and replay by cursor.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from abstractruntime.storage.commands import CommandRecord

from ..service import get_gateway_service, run_summary


router = APIRouter(prefix="/gateway", tags=["gateway"])
logger = logging.getLogger(__name__)


class StartRunRequest(BaseModel):
    flow_id: str = Field(..., description="Flow id to start (VisualFlow JSON in flows_dir).")
    input_data: Dict[str, Any] = Field(default_factory=dict)


class StartRunResponse(BaseModel):
    run_id: str


class SubmitCommandRequest(BaseModel):
    command_id: str = Field(..., description="Client-supplied idempotency key (UUID recommended).")
    run_id: str = Field(..., description="Target run id (or session id for emit_event).")
    type: str = Field(..., description="pause|resume|cancel|emit_event")
    payload: Dict[str, Any] = Field(default_factory=dict)
    ts: Optional[str] = Field(default=None, description="ISO timestamp (optional).")
    client_id: Optional[str] = None


class SubmitCommandResponse(BaseModel):
    accepted: bool
    duplicate: bool
    seq: int


@router.post("/runs/start", response_model=StartRunResponse)
async def start_run(req: StartRunRequest) -> StartRunResponse:
    svc = get_gateway_service()
    svc.runner.start()

    flow_id = str(req.flow_id or "").strip()
    if not flow_id:
        raise HTTPException(status_code=400, detail="flow_id is required")

    try:
        run_id = svc.host.start_run(flow_id=flow_id, input_data=dict(req.input_data or {}), actor_id="gateway")
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Flow '{flow_id}' not found")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to start run: {e}")
    return StartRunResponse(run_id=str(run_id))


@router.get("/runs/{run_id}")
async def get_run(run_id: str) -> Dict[str, Any]:
    svc = get_gateway_service()
    rs = svc.host.run_store
    try:
        run = rs.load(str(run_id))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load run: {e}")
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    return run_summary(run)


@router.get("/runs/{run_id}/ledger")
async def get_ledger(
    run_id: str,
    after: int = Query(0, ge=0, description="Cursor: number of records already consumed."),
    limit: int = Query(200, ge=1, le=2000),
) -> Dict[str, Any]:
    svc = get_gateway_service()
    ledger = svc.host.ledger_store.list(str(run_id))
    if not isinstance(ledger, list):
        ledger = []
    a = int(after or 0)
    items = ledger[a : a + int(limit)]
    next_after = a + len(items)
    return {"items": items, "next_after": next_after}


@router.get("/runs/{run_id}/ledger/stream")
async def stream_ledger(
    run_id: str,
    after: int = Query(0, ge=0, description="Cursor: number of records already consumed."),
    heartbeat_s: float = Query(5.0, gt=0.1, le=60.0),
) -> StreamingResponse:
    svc = get_gateway_service()
    run_id2 = str(run_id)

    async def _gen():
        cursor = int(after or 0)
        last_emit = asyncio.get_event_loop().time()
        while True:
            ledger = svc.host.ledger_store.list(run_id2)
            if not isinstance(ledger, list):
                ledger = []
            if cursor < 0:
                cursor = 0
            if cursor < len(ledger):
                while cursor < len(ledger):
                    item = ledger[cursor]
                    data = json.dumps({"cursor": cursor + 1, "record": item}, ensure_ascii=False)
                    yield f"id: {cursor + 1}\n".encode("utf-8")
                    yield b"event: step\n"
                    yield f"data: {data}\n\n".encode("utf-8")
                    cursor += 1
                    last_emit = asyncio.get_event_loop().time()
            else:
                now = asyncio.get_event_loop().time()
                if (now - last_emit) >= float(heartbeat_s):
                    yield b": keep-alive\n\n"
                    last_emit = now
                await asyncio.sleep(0.25)

    return StreamingResponse(_gen(), media_type="text/event-stream")


@router.post("/commands", response_model=SubmitCommandResponse)
async def submit_command(req: SubmitCommandRequest) -> SubmitCommandResponse:
    svc = get_gateway_service()
    typ = str(req.type or "").strip()
    if typ not in {"pause", "resume", "cancel", "emit_event"}:
        raise HTTPException(status_code=400, detail="type must be one of pause|resume|cancel|emit_event")

    record = CommandRecord(
        command_id=str(req.command_id),
        run_id=str(req.run_id),
        type=typ,
        payload=dict(req.payload or {}),
        ts=str(req.ts) if isinstance(req.ts, str) and req.ts else "",
        client_id=str(req.client_id) if isinstance(req.client_id, str) and req.client_id else None,
        seq=0,
    )

    try:
        res = svc.runner.command_store.append(record)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to append command: {e}")

    try:
        logger.info(
            "gateway_command accepted=%s dup=%s seq=%s run_id=%s type=%s command_id=%s client_id=%s",
            bool(res.accepted),
            bool(res.duplicate),
            int(res.seq),
            str(req.run_id),
            str(req.type),
            str(req.command_id),
            str(req.client_id) if req.client_id else "",
        )
    except Exception:
        pass

    return SubmitCommandResponse(accepted=bool(res.accepted), duplicate=bool(res.duplicate), seq=int(res.seq))


