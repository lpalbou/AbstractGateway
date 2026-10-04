"""`POST /api/gateway/sessions/{session_id}/archive` and `.../unarchive`.

Archive takes a conversation out of the default listing (`GET /runs?root_only=true`);
it deletes nothing. The session's runs, ledgers and artifacts are untouched and
`GET /runs?session_id=...` still reads it. The mark is stored by
`session_archive.py` in the caller's plane.

Who: the session's owner or an admin. The gateway selects a principal's runtime
plane per principal (multi-user mode), so a session another account owns is
absent from the caller's run store and answers 404, exactly like
`load_automation_controller` does for automations.

Answers `{session_id, archived, archived_at, archived_by, changed}`; a repeat is
`changed: false`. Every call is audited (`session.archived` /
`session.unarchived` in `<data_dir>/audit_log.jsonl`, and the request's audit line
names the session).
"""

from __future__ import annotations

import asyncio
import datetime
import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from abstractruntime.storage.base import QueryableRunIndexStore, QueryableRunStore

router = APIRouter(prefix="/gateway", tags=["sessions"])


def _audit(event: str, *, session_id: str, actor: str, changed: bool) -> Optional[Dict[str, Any]]:
    """Append one typed session event to the gateway audit log. Never raises."""
    try:
        from ..security.gateway_security import _AUDIT_LOCK, _audit_data_dir_from_env, _audit_log_enabled

        if not _audit_log_enabled(default=True):
            return None
        entry = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "event": event,
            "session_id": session_id,
            "actor": actor or None,
            "changed": bool(changed),
        }
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _AUDIT_LOCK:
            path = (_audit_data_dir_from_env() / "audit_log.jsonl").resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "ab") as fh:
                fh.write(line.encode("utf-8", errors="replace"))
        return entry
    except Exception:  # noqa: BLE001 - auditing never breaks the request
        return None


def _note(request: Request, session_id: str, action: str) -> None:
    try:
        detail = getattr(request.state, "audit_detail", None)
        detail = dict(detail) if isinstance(detail, dict) else {}
        detail["session"] = {"session_id": session_id, "action": action}
        request.state.audit_detail = detail
    except Exception:  # noqa: BLE001
        pass


def session_exists(run_store: Any, session_id: str) -> bool:
    """True when at least one run of this plane carries `session_id`."""
    if isinstance(run_store, QueryableRunIndexStore):
        return bool(run_store.list_run_index(session_id=session_id, limit=1))
    if isinstance(run_store, QueryableRunStore):
        return any(str(getattr(r, "session_id", "") or "") == session_id for r in run_store.list_runs(limit=1_000_000))
    raise HTTPException(status_code=400, detail="Run store does not support listing runs")


def _act(svc: Any, session_id: str, actor: str, archive: bool) -> Dict[str, Any]:
    from ..session_archive import SessionArchiveUnreadable, archive_session, unarchive_session

    if not session_exists(svc.host.run_store, session_id):
        raise HTTPException(status_code=404, detail={"reason_code": "session_not_found", "message": f"Session {session_id} does not exist."})
    try:
        if archive:
            return archive_session(svc.config.data_dir, session_id, by=actor)
        return unarchive_session(svc.config.data_dir, session_id, by=actor)
    except SessionArchiveUnreadable as exc:
        raise HTTPException(status_code=500, detail={"reason_code": "session_archive_unreadable", "message": str(exc)}) from None


async def _route(request: Request, session_id: str, archive: bool) -> Dict[str, Any]:
    from ..service import get_gateway_service
    from .gateway import _principal_from_request

    principal = _principal_from_request(request)
    sid = str(session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=422, detail={"reason_code": "invalid_request", "message": "session_id is required"})
    actor = str(getattr(principal, "user_id", "") or "")
    _note(request, sid, "archive" if archive else "unarchive")
    svc = get_gateway_service()
    out = await asyncio.to_thread(_act, svc, sid, actor, archive)
    _audit("session.archived" if archive else "session.unarchived", session_id=sid, actor=actor, changed=bool(out.get("changed")))
    return out


@router.post(
    "/sessions/{session_id}/archive",
    summary="Archive a session (owner or admin)",
    description=(
        "Takes the session out of `GET /runs?root_only=true` (listed with `archived_only=true`). Deletes nothing: "
        "runs, ledgers and artifacts are kept and `GET /runs?session_id=` still reads it. 404 `session_not_found` when "
        "the caller's plane has no run of that session. A repeat answers `changed: false`. Audited (`session.archived`)."
    ),
)
async def archive_session_route(request: Request, session_id: str) -> Dict[str, Any]:
    return await _route(request, session_id, True)


@router.post(
    "/sessions/{session_id}/unarchive",
    summary="Unarchive a session (owner or admin)",
    description=(
        "Puts an archived session back in `GET /runs?root_only=true`. 404 `session_not_found` when the caller's plane "
        "has no run of that session; `changed: false` when it was not archived. Audited (`session.unarchived`)."
    ),
)
async def unarchive_session_route(request: Request, session_id: str) -> Dict[str, Any]:
    return await _route(request, session_id, False)
