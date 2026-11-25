"""`PUT /api/gateway/sessions/{session_id}/title`: rename a conversation.

Body `{"title": "..."}`. The title is stored by `session_titles.py` in the caller's
plane, one line of at most 120 characters (longer titles are cut); an empty or blank
title, `null` or no `title` clears it, and clients show the automatic name (the
opening message) again. `GET /runs` rows of a titled session carry `session_title`.

Who: the session's owner or an admin, exactly as archive (routes/session_archive.py):
the caller's plane must hold a run of the session, else 404 `session_not_found`.

Answers `{session_id, title, changed}`; storing the stored title again is
`changed: false`. Every change is audited (`session.titled` in
`<data_dir>/audit_log.jsonl`, with `action: set|cleared`, never the title text).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException, Request

from ..work_pools import off_loop
from .session_archive import _audit, _note, session_exists

router = APIRouter(prefix="/gateway", tags=["sessions"])


def _set(svc: Any, session_id: str, title: Optional[str], actor: str) -> Dict[str, Any]:
    from ..session_titles import SessionTitlesUnreadable, set_session_title

    if not session_exists(svc.host.run_store, session_id):
        raise HTTPException(status_code=404, detail={"reason_code": "session_not_found", "message": f"Session {session_id} does not exist."})
    try:
        return set_session_title(svc.config.data_dir, session_id, title, by=actor)
    except SessionTitlesUnreadable as exc:
        raise HTTPException(status_code=500, detail={"reason_code": "session_titles_unreadable", "message": str(exc)}) from None


@router.put(
    "/sessions/{session_id}/title",
    summary="Rename a session (owner or admin)",
    description=(
        "Body `{\"title\": \"...\"}`. Stores the conversation's name: line breaks and other control characters become "
        "spaces, whitespace runs one space, trimmed, and cut at 120 characters. An empty title, `null` or no `title` "
        "clears it (clients show the opening message again). Answers `{session_id, title, changed}` (`changed: false` "
        "when the stored title was already that). 404 `session_not_found` when the caller's plane has no run of that "
        "session; 422 `invalid_request` when the body is not an object or `title` is neither a string nor null. "
        "Audited (`session.titled`)."
    ),
)
async def put_session_title(request: Request, session_id: str, body: Any = Body(None)) -> Dict[str, Any]:
    from ..service import get_gateway_service
    from .gateway import _principal_from_request

    principal = _principal_from_request(request)
    sid = str(session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=422, detail={"reason_code": "invalid_request", "message": "session_id is required"})
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail={"reason_code": "invalid_request", "message": "The body must be a JSON object: {\"title\": \"...\"}"})
    title = body.get("title")
    if title is not None and not isinstance(title, str):
        raise HTTPException(status_code=422, detail={"reason_code": "invalid_request", "message": "title must be a string or null"})
    actor = str(getattr(principal, "user_id", "") or "")
    _note(request, sid, "title")
    svc = get_gateway_service()
    out = await off_loop("heavy", _set, svc, sid, title, actor)
    if out["changed"]:
        _audit("session.titled", session_id=sid, actor=actor, changed=True, action="set" if out["title"] else "cleared")
    return out
