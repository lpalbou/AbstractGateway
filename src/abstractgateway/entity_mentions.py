"""`@entity` from any chat window: a mention runs through the VISIT DOOR (entity mentions, 2026-11-08).

`POST /api/gateway/sessions/{session_id}/mentions` with `{entity, text, mention_index}`:

1. The caller is the signed-in person (`person:<user>`), never a client-claimed speaker.
2. ONE LIFE, ONE VISIT: the entity's live visit is REUSED when it is bound to this session (its
   run's `session_id`); a live visit bound to ANOTHER conversation refuses 409
   `in_another_conversation`, naming that conversation; with none, a visit is opened bound to this
   session (channel `mention`), with the seat's idle window (`entity_seat.SEAT_IDLE_TTL_S`): the
   visit host's reaper closes it gracefully (reflection runs) after that long without a mention.
3. `text` is sent as a visit TURN with the person as the stamped speaker. The entity's HOME holds
   the episode exactly like any visit (its ledger, memory formation, artifacts).
4. When the turn parks, the entity's reply is recorded as a TURN OF THE SESSION: one completed root
   run in the caller's plane (`workflow_id` `entity-mention`), prompt = `text`, answer = the reply,
   `vars._meta.author` = the entity, `vars._meta.prompt_author` = the person, `vars._meta.mention` =
   provenance (the home's visit run id, turn id, visit id, mention index). Session history, the
   run's view and stream, and the history replayed to the next agent run read it like any turn
   (AbstractRuntime `core.turn_author`).

Several mentions in one message are separate requests (`mention_index` 0, 1, 2...); one session's
mentions are serialized, so each is its own turn in order.

Errors: 404 unknown entity, or an entity this account may not see (a member mentions only the
entities they created; a hidden entity answers exactly like a missing one, RBAC ruling
2026-10-01); 409 `in_another_conversation` (or the visit door's own 409 codes: paused, asleep,
loop busy, home lease held); 503 `entity_cannot_answer` when its mind cannot be resolved or the
turn produced no reply.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any, Dict, Optional

from .entity_visits import VisitRefused

MENTION_WORKFLOW_ID = "entity-mention"
MENTION_CHANNEL = "mention"
CODE_ELSEWHERE = "in_another_conversation"
CODE_CANNOT_ANSWER = "entity_cannot_answer"
# The visit door's refusals that mean "the one life is in another conversation".
_ELSEWHERE_CODES = frozenset({"visit_already_open", "chat_session_open", "summon_seat_live"})

_SESSION_LOCKS: Dict[str, threading.Lock] = {}
_SESSION_LOCKS_LOCK = threading.Lock()


class MentionRefused(Exception):
    def __init__(self, status: int, detail: str, *, code: str = "", session_id: Optional[str] = None) -> None:
        super().__init__(detail)
        self.status = int(status)
        self.detail = detail
        self.code = code
        self.session_id = session_id


def _session_lock(session_id: str) -> threading.Lock:
    with _SESSION_LOCKS_LOCK:
        lock = _SESSION_LOCKS.get(session_id)
        if lock is None:
            lock = _SESSION_LOCKS[session_id] = threading.Lock()
        return lock


def _display_name(manifest: Any) -> str:
    return str(getattr(manifest, "name", "") or getattr(manifest, "slug", "") or "This entity")


def entity_author(registry: Any, manifest: Any) -> Dict[str, Any]:
    """The entity as a turn's author: name, entity_id, its identity ref and avatar (the record the
    `GET /entities` row serves; null when the home has no identity record yet)."""
    from abstractruntime.core.turn_author import author_for

    from .entity_identity import entity_identity_ref, list_row_identity

    home_dir = registry.entities_dir / manifest.slug
    ref = entity_identity_ref(manifest.entity_id, home_dir)
    row = list_row_identity(registry, manifest)
    avatar = row.get("avatar") if isinstance(row, dict) else None
    return author_for(
        "entity",
        _display_name(manifest),
        entity_id=str(manifest.entity_id),
        identity_ref=str(ref) if ref is not None else None,
        avatar={"style": avatar.get("style"), "color": avatar.get("color")} if isinstance(avatar, dict) else None,
    )


def person_author(principal: Any) -> Dict[str, Any]:
    """The signed-in person as a turn's author: the account's display name, else its user id
    (`operator` on a gateway without a principal)."""
    from abstractruntime.core.turn_author import author_for

    user = str(getattr(principal, "user_id", "") or "").strip() or "operator"
    name = ""
    if principal is not None and str(getattr(principal, "source", "") or "") == "user-registry":
        from .users import GatewayUserRegistry

        rec = GatewayUserRegistry().get_user(user, tenant_id=str(getattr(principal, "tenant_id", "") or "default"))
        name = str(getattr(rec, "display_name", "") or "").strip() if rec is not None else ""
    return author_for("person", name or user)


def _elsewhere(manifest: Any, holder_session: Optional[str], cause: str = "") -> MentionRefused:
    name = _display_name(manifest)
    where = f"another conversation (session {holder_session})" if holder_session else "another conversation"
    from .entity_seat import SEAT_IDLE_TTL_S

    sentence = (
        f"{name} is in {where}. One conversation at a time: {name} is free again when that conversation "
        f"ends, or after {int(SEAT_IDLE_TTL_S // 60)} minutes without a message there."
    )
    if cause:
        sentence += f" ({cause})"
    return MentionRefused(409, sentence, code=CODE_ELSEWHERE, session_id=holder_session)


def _from_visit_refusal(e: VisitRefused, manifest: Any) -> MentionRefused:
    code = str(getattr(e, "code", "") or "")
    if code in _ELSEWHERE_CODES:
        return _elsewhere(manifest, None, cause=str(e.detail))
    if code == "mind_unresolved":
        return MentionRefused(503, f"{_display_name(manifest)} cannot answer: {e.detail}", code=CODE_CANNOT_ANSWER)
    return MentionRefused(int(e.status), str(e.detail), code=code)


def _record_session_turn(
    svc: Any, *, session_id: str, text: str, reply: str, principal: Any,
    author: Dict[str, Any], prompt_author: Dict[str, Any], provenance: Dict[str, Any],
) -> Any:
    """The entity's reply as a turn of the session: one completed root run in the caller's plane."""
    from abstractruntime.core.models import RunState, RunStatus
    from abstractruntime.core.run_origin import ORIGIN_KEY, origin_for
    from abstractruntime.core.turn_author import stamp_turn_authors

    tenant = str(getattr(principal, "tenant_id", "") or "").strip() or "default"
    user = str(getattr(principal, "user_id", "") or "").strip() or "operator"
    vars_obj: Dict[str, Any] = {
        "prompt": text,
        "context": {"task": text, "messages": []},
        "_meta": {ORIGIN_KEY: origin_for("person", actor=f"{tenant}:{user}", client=MENTION_CHANNEL), "mention": provenance},
    }
    stamp_turn_authors(vars_obj, author=author, prompt_author=prompt_author)
    run = RunState(
        run_id=str(uuid.uuid4()),
        workflow_id=MENTION_WORKFLOW_ID,
        status=RunStatus.COMPLETED,
        current_node="done",
        vars=vars_obj,
        output={"answer": reply},
        actor_id="gateway",
        session_id=session_id,
    )
    svc.host.run_store.save(run)
    # run.started / run.finished on the account's event stream (every client of this person
    # learns of the new turn like any other).
    stream = getattr(getattr(svc, "runner", None), "user_stream", None)
    if stream is not None:
        from .events_stream import record_run

        record_run(stream, run)
    return run


def mention(
    *, svc: Any, registry: Any, visit_host: Any, principal: Any,
    session_id: str, entity: str, text: str, mention_index: int,
) -> Dict[str, Any]:
    """Run one mention (see the module doc). Raises MentionRefused, KeyError (unknown entity)."""
    from .entity_seat import SEAT_IDLE_TTL_S

    sid = str(session_id or "").strip()
    manifest = registry.manifest_for(entity)  # KeyError -> 404
    visitor = f"person:{str(getattr(principal, 'user_id', '') or '').strip() or 'operator'}"
    with _session_lock(sid):
        er = registry.get_entity_runtime(manifest.slug)
        live = visit_host._live_visit_run(er)
        reused = False
        if live is not None:
            if str(getattr(live, "session_id", "") or "") != sid:
                raise _elsewhere(manifest, str(getattr(live, "session_id", "") or "") or None)
            run_id = str(live.run_id)
            reused = True
        else:
            try:
                _pre, leg = visit_host.open_leg(
                    entity,
                    participants=[visitor],
                    visit_id=f"visit-{uuid.uuid4().hex[:12]}",
                    session_id=sid,
                    channel_label=MENTION_CHANNEL,
                    idle_seconds=float(SEAT_IDLE_TTL_S),
                )
            except VisitRefused as e:
                raise _from_visit_refusal(e, manifest) from None
            run_id = str(leg["run_id"])
        try:
            turned = visit_host.turn(entity, run_id, text=text, speaker=visitor)
        except VisitRefused as e:
            raise _from_visit_refusal(e, manifest) from None
        reply = str(turned.get("reply") or "").strip()
        status = str(turned.get("status") or "")
        if status == "failed" or not reply:
            why = str(turned.get("error") or "") or "the turn produced no reply"
            raise MentionRefused(503, f"{_display_name(manifest)} cannot answer: {why}", code=CODE_CANNOT_ANSWER)
        turn_id = str(turned.get("turn_id") or "") or None
        visit_ns = ((er.run_store.load(run_id).vars or {}).get("_runtime") or {}).get("entity") or {}
        author = entity_author(registry, manifest)
        provenance = {
            "entity": str(entity),
            "entity_id": str(manifest.entity_id),
            "mention_index": int(mention_index),
            "visit_run_id": run_id,
            "visit_id": visit_ns.get("visit_id"),
            "turn_id": turn_id,
        }
        run = _record_session_turn(
            svc, session_id=sid, text=text, reply=reply, principal=principal,
            author=author, prompt_author=person_author(principal), provenance=provenance,
        )
    return {
        "run_id": str(run.run_id),
        "turn_id": turn_id,
        "author": author,
        "session_id": sid,
        "mention_index": int(mention_index),
        "reply": reply,
        "visit": {
            "run_id": run_id,
            "turn_id": turn_id,
            "visit_id": visit_ns.get("visit_id"),
            "entity_id": str(manifest.entity_id),
            "reused": reused,
        },
    }


__all__ = ["CODE_CANNOT_ANSWER", "CODE_ELSEWHERE", "MENTION_WORKFLOW_ID", "MentionRefused", "entity_author", "mention", "person_author"]
