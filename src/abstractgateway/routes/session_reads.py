"""Account-wide conversation read receipts and their durable replay cursor."""
from __future__ import annotations

import logging
from typing import Literal
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from ..work_pools import off_loop
from ..session_reads import SessionReadLedger
from ..events_stream import stream_for

router = APIRouter(prefix="/gateway", tags=["sessions"])
logger = logging.getLogger(__name__)

class ReadReceipt(BaseModel):
    session_id: str = Field(min_length=1, max_length=512)
    run_id: str = Field(min_length=1, max_length=512, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    status: Literal["request", "completed", "failed", "cancelled"]

class ReadBatch(BaseModel):
    receipts: list[ReadReceipt] = Field(min_length=1, max_length=100)


def _context(request: Request):
    from ..service import get_gateway_service
    from .gateway import _principal_from_request
    p = _principal_from_request(request)
    svc = get_gateway_service()
    return svc, SessionReadLedger(svc.config.data_dir, p.tenant_id, p.user_id), stream_for(svc.config.data_dir, p.tenant_id, p.user_id)


def _publish(ledger, stream):
    try:
        ledger.publish_pending(stream)
    except Exception:
        # Durable receipts already committed. The pending outbox retries on the
        # next read/write; a stream outage cannot discard or forge a read.
        logger.warning("Session read event delivery pending; durable receipts retained", exc_info=True)


@router.get("/me/session-reads")
async def list_reads(request: Request, after: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000), run_id: list[str] | None = Query(None, max_length=100)):
    _svc, ledger, stream = _context(request)
    await off_loop("light", _publish, ledger, stream)
    return await off_loop("light", ledger.page, after, limit, run_id)


@router.post("/me/session-reads")
async def mark_reads(request: Request, body: ReadBatch):
    svc, ledger, stream = _context(request)
    def write():
        from abstractruntime.core.run_attribution import automation_index_fields, is_turn_root
        values = []
        for receipt in body.receipts:
            run = svc.host.run_store.load(receipt.run_id)
            if run is None or str(run.session_id or "") != receipt.session_id:
                raise HTTPException(404, detail={"reason_code": "session_run_not_found", "message": "This session turn does not exist in your runtime."})
            attribution = automation_index_fields(run.vars, run_id=run.run_id)
            role = attribution.get("role")
            if not is_turn_root(parent_run_id=run.parent_run_id, role=role) or attribution.get("session_kind") in ("automation", "occurrence"):
                raise HTTPException(422, detail={"reason_code": "invalid_session_turn", "message": "Read receipts apply to conversation turn roots. Use automation attention for automation results."})
            status = str(getattr(run.status, "value", run.status))
            if receipt.status != "request" and receipt.status != status:
                raise HTTPException(409, detail={"reason_code": "session_revision_changed", "message": "The turn changed. Reload its outcome before marking it read."})
            values.append(receipt.model_dump())
        rows = ledger.append(values)
        _publish(ledger, stream)
        return {"items": rows}
    return await off_loop("light", write)
