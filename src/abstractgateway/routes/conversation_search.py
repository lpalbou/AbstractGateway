"""`GET /api/gateway/sessions/search?q=…`: keyword search over the caller's conversations.

What was asked and what was answered, in every turn of the caller's sessions, with
`abstractruntime.conversation_search` (an FTS5 trigram index: words and substrings, case and
accents ignored, a fuzzy pass for typos). One index per runtime plane, in that plane's data folder:
the gateway picks the caller's plane per principal (multi-user mode), so a search can only ever
read the caller's own conversations, exactly like `GET /runs`.

Results are filtered like the conversation listing (`GET /runs?root_only=true`), inside the search
itself: the session kinds asked for (`chat,discussion`, the kinds indexed), conversations rather
than Docs assistant chats (`kind`), archived sessions left out unless `archived=true` (then only
them). One item per session:
its best turn, a snippet around the match with the match positions, where it matched.

The index catches up on each call within a time budget; `indexing.complete: false` says older
conversations are still being read (a first search on a large plane), never a silent partial list.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(tags=["sessions"])   # included by routes/session_archive.py (prefix /gateway)

INDEX_FILENAME = "conversation_search.sqlite3"
SYNC_BUDGET_S = 2.5
_INDEXES: Dict[str, Any] = {}
_INDEXES_LOCK = threading.Lock()


def _index_for(svc: Any) -> Any:
    from abstractruntime.conversation_search import ConversationSearch

    path = Path(svc.config.data_dir) / INDEX_FILENAME
    key = str(path.resolve())
    with _INDEXES_LOCK:
        idx = _INDEXES.get(key)
        if idx is None or idx.run_store is not svc.host.run_store:
            idx = ConversationSearch(path, run_store=svc.host.run_store, ledger_store=svc.host.ledger_store,
                                     artifact_store=getattr(svc.host, "artifact_store", None))
            _INDEXES[key] = idx
        return idx


@router.get(
    "/sessions/search",
    summary="Search my conversations by keywords",
    description=(
        "Keyword search over the questions and answers of the caller's conversations (their own runtime plane). "
        "Words and substrings, case and accents ignored; a fuzzy pass finds a word with a typo. Filtered like "
        "`GET /runs?root_only=true` (session kinds, conversations, archived left out unless `archived=true`). One item "
        "per session: `{session_id, run_id, prompt, snippet, highlights, matched_in, score, created_at, updated_at, "
        "session_kind, archived, fuzzy}`. `indexing.complete: false` while older conversations are still being indexed."
    ),
)
async def search_sessions(
    request: Request,
    q: str = Query(..., min_length=1, max_length=200, description="The words to find."),
    limit: int = Query(20, ge=1, le=50),
    archived: bool = Query(False, description="Search archived conversations (only them) instead of the listed ones."),
    session_kind: Optional[str] = Query("chat,discussion", description="Session kinds, as in GET /runs."),
    kind: str = Query("conversation", description="conversation (default), docs or all, as in GET /runs."),
) -> Dict[str, Any]:
    from ..service import get_gateway_service
    from ..session_archive import SessionArchiveUnreadable, archived_sessions
    from ..session_kinds import SessionKindsUnreadable, session_kinds
    from .gateway import _parse_session_kind_filter

    want_kind = str(kind or "conversation").strip().lower()
    if want_kind not in ("conversation", "docs", "all"):
        raise HTTPException(status_code=400, detail=f"Invalid kind {kind!r} (expected conversation|docs|all)")
    kinds = _parse_session_kind_filter(session_kind)
    svc = get_gateway_service()
    try:
        archived_map = archived_sessions(svc.config.data_dir)
        kinds_map = session_kinds(svc.config.data_dir)
    except (SessionArchiveUnreadable, SessionKindsUnreadable) as exc:
        raise HTTPException(status_code=500, detail={"reason_code": "session_state_unreadable", "message": str(exc)}) from None

    idx = _index_for(svc)
    report = await asyncio.to_thread(idx.sync, budget_s=SYNC_BUDGET_S)

    # The listing's filters, applied inside the search (so they never empty a page that had
    # matches further down): archived in or out, the session purpose, the session kinds.
    archived_ids = set(archived_map)
    only: Optional[set] = archived_ids if archived else None
    exclude = set() if archived else set(archived_ids)
    if want_kind == "conversation":
        exclude |= {sid for sid, k in kinds_map.items() if k != "conversation"}
    elif want_kind != "all":
        wanted = {sid for sid, k in kinds_map.items() if k == want_kind}
        only = wanted if only is None else only & wanted
    searched = set(idx.kinds) if kinds is None else set(kinds) & set(idx.kinds)
    hits = await asyncio.to_thread(idx.search, q, limit=limit, sessions=only, exclude=exclude, session_kinds=searched)
    items = [{**h.as_dict(), "archived": h.session_id in archived_ids} for h in hits]
    return {
        "query": q,
        "items": items,
        "count": len(items),
        "fuzzy": bool(idx.trigram),
        "kinds": sorted(searched),
        "indexing": {"complete": report.complete, "pending_sessions": report.pending_sessions, "turns": report.total_turns,
                     "errors": len(report.errors)},
    }
