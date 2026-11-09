"""`POST /api/gateway/sessions/{session_id}/mentions`: `@entity` from any chat window.

The whole door is `entity_mentions.mention` (visit reuse, the seat rule, the session turn). This
route derives the caller from the signed-in principal, applies entity visibility (a hidden entity
answers 404 exactly like a missing one) and maps refusals to `{detail, code}` (plus
`X-Gateway-Error-Code`, the visit door's error shape).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/gateway", tags=["sessions"])


class MentionRequest(BaseModel):
    entity: str = Field(..., min_length=1, max_length=200, description="The entity's name (as `GET /entities` lists it)")
    text: str = Field(..., description="The message as typed (the entity receives it as the visit turn)")
    mention_index: int = Field(default=0, ge=0, description="0, 1, 2... for several mentions in one message, sent in order")
    mention_id: Optional[str] = Field(
        default=None, min_length=1, max_length=200,
        description="The client's id for THIS mention: a resend with the same id returns the first answer, never a "
                    "second one (a double submit, a retry after a lost response)",
    )
    repeat_of: Optional[str] = Field(
        default=None, max_length=200,
        description="The run id of the person's own turn of this conversation that already carries these words "
                    "(the agent turn the same message started); the mention turn is then marked repeat_of so "
                    "clients show the words once",
    )


def _not_found(name: str) -> JSONResponse:
    """The one 404 for a missing AND a hidden entity: the sentence only (no server folder path)."""
    from ..entities import entity_slug

    try:
        shown = entity_slug(name)
    except Exception:  # noqa: BLE001 - an invalid name is still just "not found"
        shown = str(name or "").strip()
    return JSONResponse(status_code=404, content={"detail": f"There is no entity called {shown!r}.", "code": "entity_not_found"},
                        headers={"X-Gateway-Error-Code": "entity_not_found"})


@router.post(
    "/sessions/{session_id}/mentions",
    summary="Mention an entity in a conversation (@entity)",
    description=(
        "Runs `text` as a visit turn of the entity (one life, one visit: reused when bound to this session) and "
        "records its reply as a turn of this session with `author`. 200 `{run_id, turn_id, author, session_id, "
        "mention_index, reply, visit}`; 404 unknown or hidden entity; 409 `in_another_conversation` when the "
        "entity's visit is bound to another conversation; 503 `entity_cannot_answer`."
    ),
)
def mention_entity(session_id: str, req: MentionRequest) -> Any:
    from ..entity_access import require_entity_visible
    from ..entity_mentions import MentionRefused, mention
    from ..security.principal import current_gateway_principal
    from ..service import get_gateway_service
    from .entities import _registry, _visit_host

    sid = str(session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=422, detail="session_id is required")
    if not str(req.text or "").strip():
        raise HTTPException(status_code=422, detail="text is empty: say something to the entity")
    from ..entities import entity_slug

    try:
        entity_slug(req.entity)
    except ValueError:
        # A name no entity can have ('@Castor', a reserved word such as 'auth') is simply not an
        # entity: the same 404 as a missing or hidden one, never a 500 carrying internal text
        # (review 2026-11-09 finding 4). A malformed body is FastAPI's 422.
        return _not_found(req.entity)
    registry = _registry()
    try:
        # A hidden entity answers EXACTLY like a missing one (RBAC ruling 2026-10-01): same 404, same
        # sentence, same code `entity_not_found` (so a client tells it from a missing route).
        require_entity_visible(req.entity, registry=registry)
    except HTTPException as e:
        if e.status_code == 404:
            return _not_found(req.entity)
        raise
    try:
        return mention(
            svc=get_gateway_service(),
            registry=registry,
            visit_host=_visit_host(),
            principal=current_gateway_principal(),
            session_id=sid,
            entity=req.entity,
            text=req.text,
            mention_index=int(req.mention_index),
            repeat_of=req.repeat_of,
            mention_id=req.mention_id,
        )
    except KeyError:
        return _not_found(req.entity)
    except MentionRefused as e:
        body: Dict[str, Any] = {"detail": e.detail}
        headers = None
        if e.code:
            body["code"] = e.code
            headers = {"X-Gateway-Error-Code": e.code}
        # Every refusal names the caller's own conversation (never another account's): the
        # client always knows which conversation the sentence is about.
        body["session_id"] = e.session_id or sid
        return JSONResponse(status_code=e.status, content=body, headers=headers)
