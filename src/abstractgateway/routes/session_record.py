"""`GET /api/gateway/sessions/{session_id}/track-record|reputation|acquaintances`.

A regular agent's track record, reputation and acquaintances (ADR 0052 sections 5, 6 and 11): the
runtime computes them over the session's runs; the answer is its typed output, unchanged
(`entity_record.py`). Entities have their own routes (`/entities/{name}/...`).

Who: the session's owner or an admin, the same rule as `/sessions/{id}/archive`. The gateway
selects a principal's runtime plane per principal (multi-user mode), so a session another account
owns is absent from the caller's run store and answers 404 `session_not_found`.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

from ..work_pools import off_loop

router = APIRouter(prefix="/gateway", tags=["sessions"])


def _context(request: Request, session_id: str):
    from ..service import get_gateway_service
    from .gateway import _principal_from_request

    _principal_from_request(request)
    sid = str(session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=422, detail={"reason_code": "invalid_request", "message": "session_id is required"})
    return get_gateway_service(), sid


@router.get(
    "/sessions/{session_id}/track-record",
    summary="A session's track record (owner or admin)",
    description=(
        "The runtime's `TrackRecord` over the session's runs: sessions, runs, successes, failures, cancelled, in "
        "progress, tokens, per task kind, the last outcomes, `basis`. `since` (ISO-8601) keeps what started at or "
        "after it. 404 `session_not_found` when the caller's plane has no run of that session."
    ),
)
async def get_session_track_record(request: Request, session_id: str, since: Optional[str] = None) -> Dict[str, Any]:
    from ..entity_record import parse_since, session_track_record_answer

    svc, sid = _context(request, session_id)
    after = parse_since(since)
    return await off_loop("heavy", session_track_record_answer, svc, sid, since=after)


@router.get(
    "/sessions/{session_id}/reputation",
    summary="A session's reputation (owner or admin)",
    description=(
        "The runtime's `Reputation` over the session's runs: trust per task kind with its support, the overall "
        "cell, the evidence, `basis`. 404 `session_not_found` when the caller's plane has no run of that session."
    ),
)
async def get_session_reputation(request: Request, session_id: str) -> Dict[str, Any]:
    from ..entity_record import session_reputation_answer

    svc, sid = _context(request, session_id)
    return await off_loop("heavy", session_reputation_answer, svc, sid)


@router.get(
    "/sessions/{session_id}/acquaintances",
    summary="Whom a session met (owner or admin)",
    description=(
        "The runtime's `Acquaintances` over the session's runs: the entities and humans met, with `not_counted`. "
        "404 `session_not_found` when the caller's plane has no run of that session."
    ),
)
async def get_session_acquaintances(request: Request, session_id: str) -> Dict[str, Any]:
    from ..entity_record import session_acquaintances_answer

    svc, sid = _context(request, session_id)
    return await off_loop("heavy", session_acquaintances_answer, svc, sid)


@router.get(
    "/sessions/{session_id}/identity",
    summary="A conversation's temporary identity (owner or admin)",
    description=(
        "The session's identity (framework ADR 0052 §9) in the entity shape: `{session_id, identity: {rev, avatar: "
        "{style, color, source, watch_code}, voice, set_at, set_by}, identity_ref}` (`session:<sid>#<rev>`); "
        "`identity: null` when the session has none. `?rev=N` serves an earlier revision. 404 `session_not_found` "
        "when the caller's plane has no run of that session."
    ),
)
async def get_session_identity(request: Request, session_id: str, rev: Optional[int] = None) -> Dict[str, Any]:
    from ..session_identity import SessionIdentityUnreadable, session_identity_view
    from .session_archive import session_exists

    svc, sid = _context(request, session_id)

    def read() -> Dict[str, Any]:
        if not session_exists(svc.host.run_store, sid):
            raise HTTPException(status_code=404, detail={"reason_code": "session_not_found", "message": f"Session {sid} does not exist."})
        try:
            return session_identity_view(svc.config.data_dir, sid, rev=rev)
        except LookupError as e:
            raise HTTPException(status_code=404, detail={"reason_code": "identity_rev_not_found", "message": str(e)}) from None
        except SessionIdentityUnreadable as e:
            logging.getLogger("abstractgateway.session_identity").error("session %s: %s", sid, e)
            raise HTTPException(status_code=500, detail={"reason_code": "record_unreadable",
                                                         "message": "This conversation's identity record (session_identities.json) can't be read."}) from None

    return await off_loop("light", read)
