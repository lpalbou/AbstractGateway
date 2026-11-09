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

import contextlib
import logging
import threading
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .entity_visits import VisitRefused

logger = logging.getLogger(__name__)

MENTION_WORKFLOW_ID = "entity-mention"
MENTION_CHANNEL = "mention"
CODE_ELSEWHERE = "in_another_conversation"
CODE_CANNOT_ANSWER = "entity_cannot_answer"
# The visit door's refusals that mean "the one life is in another conversation".
_ELSEWHERE_CODES = frozenset({"visit_already_open", "chat_session_open", "summon_seat_live"})

# One lock per conversation WHILE a mention runs in it (REVIEW-1 item 11): the entry is reference
# counted and removed when its last user leaves, so the map never outlives its use.
_SESSION_LOCKS: Dict[str, list] = {}
_SESSION_LOCKS_LOCK = threading.Lock()


class MentionRefused(Exception):
    def __init__(self, status: int, detail: str, *, code: str = "", session_id: Optional[str] = None) -> None:
        super().__init__(detail)
        self.status = int(status)
        self.detail = detail
        self.code = code
        self.session_id = session_id


@contextlib.contextmanager
def _session_lock(session_id: str):
    with _SESSION_LOCKS_LOCK:
        entry = _SESSION_LOCKS.get(session_id)
        if entry is None:
            entry = _SESSION_LOCKS[session_id] = [threading.Lock(), 0]
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _SESSION_LOCKS_LOCK:
            entry[1] -= 1
            if entry[1] <= 0 and _SESSION_LOCKS.get(session_id) is entry:
                del _SESSION_LOCKS[session_id]


def _display_name(manifest: Any) -> str:
    # THE entity display name (the spark's name, as the identity door words it), never the slug.
    from .entity_identity import _display_name as identity_display_name

    return identity_display_name(manifest)


# The conversation a mentioned entity reads (the chip promises "<name> will read this
# conversation's history"): the newest whole turns, at most this many and this many tokens,
# whichever bound comes first. Whole turns only: a turn is never cut.
CONTEXT_MAX_TURNS = 12
CONTEXT_MAX_TOKENS = 8000


def _speaker_label(author: Any) -> str:
    """`Castor (entity)` / `Laurent` / `Agent (Basic Agent)`, from the structured author only."""
    from abstractruntime.core.turn_author import read_author

    a = read_author(author) or {"kind": "person", "name": None, "entity_id": None}
    name = a["name"] or a["entity_id"] or "someone"
    if a["kind"] == "entity":
        return f"{name} (entity)"
    if a["kind"] == "agent":
        return f"Agent ({name})"
    return name


def _attachment_names(row: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for a in row.get("attachments") or []:
        if isinstance(a, dict):
            name = str(a.get("filename") or a.get("name") or a.get("path") or "").strip()
            if name:
                out.append(name.rsplit("/", 1)[-1])
    return out


def _session_turn_lines(svc: Any, session_id: str, *, current_turn: Optional[str], after_visit_run: Optional[str]) -> Tuple[List[List[str]], bool]:
    """The session's recent turns as quotable line groups (from the turn rows and their `author`
    fields, never from parsing text). With `after_visit_run` (a REUSED mention visit, REVIEW-1 item
    6) only the turns newer than that visit's last mention are returned: the visit already holds
    the rest, and quoting them again duplicated history, verbatims and memory."""
    from abstractruntime.history_bundle import _best_effort_session_turns

    rows = _best_effort_session_turns(
        run_store=svc.host.run_store, ledger_store=getattr(svc.host, "ledger_store", None), artifact_store=None,
        session_id=session_id, limit=CONTEXT_MAX_TURNS + 1, include_stats=False, include_artifacts=False,
    )
    if after_visit_run:
        last = -1
        for i, row in enumerate(rows):
            try:
                run = svc.host.run_store.load(str(row.get("run_id") or ""))
            except Exception:  # noqa: BLE001
                run = None
            meta = ((getattr(run, "vars", None) or {}).get("_meta") or {}) if run is not None else {}
            if str((meta.get("mention") or {}).get("visit_run_id") or "") == str(after_visit_run):
                last = i
        rows = rows[last + 1:]
    older_unread = len(rows) > CONTEXT_MAX_TURNS
    rows = rows[-CONTEXT_MAX_TURNS:]
    turns: List[List[str]] = []
    for row in rows:
        lines: List[str] = []
        prompt = str(row.get("prompt") or "").strip()
        if prompt and not row.get("repeat_of") and str(row.get("run_id") or "") != str(current_turn or ""):
            attached = _attachment_names(row)
            note = f" (attached: {', '.join(attached)})" if attached else ""
            lines.append(f"{_speaker_label(row.get('prompt_author'))}: {prompt}{note}")
        answer = str(row.get("answer") or "").strip()
        if answer and str(row.get("status") or "") == "completed":
            lines.append(f"{_speaker_label(row.get('author'))}: {answer}")
        if lines:
            turns.append(lines)
    return turns, older_unread


def _visit_lane_turn_lines(registry: Any, visit_host: Any, *, session_id: str, caller: str, mentioned_slug: str) -> Optional[List[List[str]]]:
    """In a VISIT LANE (an Entity-app visit with another entity, bound to this conversation, the
    caller among its participants; REVIEW-1 item 5) the conversation IS that visit: its visitors'
    words (the door's own resume records, never the rendered prompt with its memories) and the
    visited entity's answers, in order. None when no such visit is bound."""
    from abstractruntime.core.run_origin import origin_of
    from abstractruntime.identity.visit_workflow import VISITOR_WAIT_KEY

    entities_dir = registry.entities_dir
    if not entities_dir.exists():
        return None
    for child in sorted(entities_dir.iterdir()):
        if not child.is_dir() or child.name.startswith(".") or child.name == mentioned_slug:
            continue
        if not any(child.glob("runtime_*.sqlite3")):
            continue
        er = registry.get_entity_runtime(child.name)
        live = visit_host._live_visit_run(er)
        if live is None or str(getattr(live, "session_id", "") or "") != session_id:
            continue
        if str((origin_of(live.vars) or {}).get("client") or "") == MENTION_CHANNEL or caller not in _stamped_participants(live):
            continue
        name = _display_name(registry.manifest_for(child.name))
        words: List[Tuple[str, str]] = []
        stamped = _stamped_participants(live)
        default_speaker = next((p for p in stamped if not p.startswith("entity:")), caller)
        for rec in er.ledger_store.list(str(live.run_id)) or []:
            if not isinstance(rec, dict) or str(rec.get("status") or "") != "completed":
                continue
            effect = rec.get("effect") if isinstance(rec.get("effect"), dict) else {}
            outer = effect.get("payload") if isinstance(effect.get("payload"), dict) else {}
            inner = outer.get("payload") if isinstance(outer.get("payload"), dict) else {}
            # The visitor's turn is the door's own resume of the visit's wait (its payload carries
            # the words as sent and the verified speaker), never the rendered prompt.
            if effect.get("type") == "resume" and outer.get("wait_key") == VISITOR_WAIT_KEY and isinstance(inner.get("text"), str):
                words.append((str(inner.get("speaker") or default_speaker), inner["text"].strip()))
        history = ((live.vars or {}).get("_visit") or {}).get("history") or []
        answers = [str(m.get("content") or "").strip() for m in history if isinstance(m, dict) and m.get("role") == "assistant"]
        # Tail-anchored pairing: the newest words go with the newest answer.
        turns: List[List[str]] = []
        offset = len(words) - len(answers)
        for i, (speaker, text) in enumerate(words):
            if not text:
                continue
            label = _person_label(speaker)
            lines = [f"{label}: {text}"]
            j = i - offset
            if 0 <= j < len(answers) and answers[j]:
                lines.append(f"{name} (entity): {answers[j]}")
            turns.append(lines)
        return turns
    return None


def _person_label(principal: str) -> str:
    kind, _, ident = str(principal or "").partition(":")
    if kind == "entity":
        return f"{ident.split('@', 1)[0].capitalize()} (entity)"
    return ident or "someone"


def conversation_context(
    svc: Any, session_id: str, *, current_turn: Optional[str] = None, after_visit_run: Optional[str] = None,
    lane_turns: Optional[List[List[str]]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """The conversation as one quoted block plus the bound's receipt: the newest WHOLE turns that
    fit (at most CONTEXT_MAX_TURNS turns and CONTEXT_MAX_TOKENS tokens). A single turn too long to
    fit is skipped alone, with a note, and the newer and older turns that fit are still quoted
    (never a cut turn, never nothing because of one long turn). `lane_turns` (a visit lane) replace
    the session's turns."""
    from abstractruntime.memory.token_budget import estimate_message_tokens

    if lane_turns is not None:
        turns, older_unread = list(lane_turns)[-CONTEXT_MAX_TURNS:], len(lane_turns) > CONTEXT_MAX_TURNS
    else:
        turns, older_unread = _session_turn_lines(svc, session_id, current_turn=current_turn, after_visit_run=after_visit_run)
    kept: List[List[str]] = []
    tokens = 0
    skipped = 0
    for lines in reversed(turns):  # newest first, whole turns
        cost = estimate_message_tokens({"role": "user", "content": "\n".join(lines)})
        if cost > CONTEXT_MAX_TOKENS:
            skipped += 1  # too long to quote on its own: left out, said so
            continue
        if tokens + cost > CONTEXT_MAX_TOKENS:
            break
        kept.append(lines)
        tokens += cost
    kept.reverse()
    report = {
        "turns": len(kept), "tokens": tokens, "max_turns": CONTEXT_MAX_TURNS, "max_tokens": CONTEXT_MAX_TOKENS,
        "dropped_turns": len(turns) - len(kept), "older_turns_not_read": older_unread, "skipped_oversize": skipped,
    }
    if not kept and not skipped:
        return "", report
    body = "\n".join(line for lines in kept for line in lines)
    note = (f"\n[{skipped} turn(s) too long to quote are left out.]" if skipped else "")
    block = (
        f"[This conversation so far, quoted (the last {len(kept)} turn(s)):\n{body}{note}\n"
        "End of the quoted conversation.]"
    )
    return block, report


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


def _elsewhere(manifest: Any, caller_session: str, cause: str = "", *, own_session: Optional[str] = None) -> MentionRefused:
    """The entity is in someone else's conversation. The sentence and the body name NO other
    session, run or account (security, 2026-11-09 review K: the holder's session id let a member
    post into the admin's visit); `session_id` is the caller's own. The cause goes to the log."""
    name = _display_name(manifest)
    from .entity_seat import SEAT_IDLE_TTL_S

    if cause:
        logger.info("mention refused, %s is busy elsewhere: %s", getattr(manifest, "slug", "?"), cause)
    where = f"another of your conversations (session {own_session})" if own_session else "another conversation"
    sentence = (
        f"{name} is in {where}. One conversation at a time: {name} is free again when that "
        f"conversation ends, or after {int(SEAT_IDLE_TTL_S // 60)} minutes without a message there."
    )
    return MentionRefused(409, sentence, code=CODE_ELSEWHERE, session_id=own_session or caller_session or None)


def _stamped_participants(run: Any) -> list:
    return [str(p) for p in ((((getattr(run, "vars", None) or {}).get("_runtime") or {}).get("entity") or {}).get("participants") or [])]


def _own_mention_visit(run: Any, *, session_id: str, caller: str) -> bool:
    """A live visit the mention door may reuse: opened by this door, bound to this session, with
    the caller among its stamped participants."""
    from abstractruntime.core.run_origin import origin_of

    return (
        str(getattr(run, "session_id", "") or "") == session_id
        and str((origin_of(getattr(run, "vars", None)) or {}).get("client") or "") == MENTION_CHANNEL
        and caller in _stamped_participants(run)
    )


def _from_visit_refusal(e: VisitRefused, manifest: Any, session_id: str = "") -> MentionRefused:
    code = str(getattr(e, "code", "") or "")
    if code in _ELSEWHERE_CODES:
        return _elsewhere(manifest, session_id, cause=str(e.detail))
    if code == "mind_unresolved":
        logger.info("mention: %s cannot answer (mind unresolved): %s", getattr(manifest, "slug", "?"), e.detail)
        return _cannot_answer(manifest, session_id)
    if code == "stopped_use_resume":
        # The kill switch's own refusal passes through intact (review #6).
        return MentionRefused(int(e.status), str(e.detail), code=code, session_id=session_id or None)
    if not code or int(e.status) == 409:
        # Every refusal carries a code and a person's sentence (REVIEW-1 / mentions-review M): an
        # uncoded busy refusal ("a visit is opening ... retry shortly") is the entity being busy.
        if int(e.status) == 409:
            return _elsewhere(manifest, session_id, cause=str(e.detail))
        logger.info("mention refused by the visit door (%s): %s", e.status, e.detail)
        return MentionRefused(int(e.status), f"{_display_name(manifest)} cannot take this message now.",
                              code="entity_unavailable", session_id=session_id or None)
    return MentionRefused(int(e.status), str(e.detail), code=code, session_id=session_id or None)


def _cannot_answer(manifest: Any, session_id: str) -> MentionRefused:
    """503 in plain words: the provider's error and model list go to the log, never to the person."""
    return MentionRefused(503, f"{_display_name(manifest)} cannot answer right now. Try again in a moment.",
                          code=CODE_CANNOT_ANSWER, session_id=session_id or None)


def _check_repeat_of(svc: Any, session_id: str, repeat_of: Optional[str], *, principal: Any = None, text: str = "") -> Optional[str]:
    """`repeat_of` names the person's OWN turn of this session whose words the mention repeats: a
    root run of this session in the caller's plane, started by the caller, carrying the same words
    (mentions-review E); anything else refuses 422 `repeat_of_unknown`."""
    from abstractruntime.core.run_origin import origin_of

    rid = str(repeat_of or "").strip()
    if not rid:
        return None
    run = svc.host.run_store.load(rid)
    refused = MentionRefused(422, "That earlier message is not one of your own turns in this conversation.",
                             code="repeat_of_unknown", session_id=session_id or None)
    if run is None or str(getattr(run, "session_id", "") or "") != session_id or getattr(run, "parent_run_id", None):
        raise refused
    tenant = str(getattr(principal, "tenant_id", "") or "").strip() or "default"
    user = str(getattr(principal, "user_id", "") or "").strip() or "operator"
    actor = str((origin_of(getattr(run, "vars", None)) or {}).get("actor") or "")
    if actor not in (f"{tenant}:{user}", f"person:{user}", user):
        raise refused
    if str((getattr(run, "vars", None) or {}).get("prompt") or "").strip() != str(text or "").strip():
        raise refused
    return rid


def _record_session_turn(
    svc: Any, *, session_id: str, text: str, reply: str, principal: Any,
    author: Dict[str, Any], prompt_author: Dict[str, Any], provenance: Dict[str, Any],
    asked_at: str, repeat_of: Optional[str] = None, run_id: Optional[str] = None,
) -> Any:
    """The entity's reply as a turn of the session: one completed root run in the caller's plane.
    `created_at` is when the person's words arrived (`asked_at`), `updated_at` when the reply did,
    so the question always sorts before its answer."""
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
    if repeat_of:
        from abstractruntime.core.turn_author import REPEAT_OF_KEY

        vars_obj["_meta"][REPEAT_OF_KEY] = repeat_of
    from abstractruntime.core.models import utc_now
    run = RunState(
        run_id=str(run_id or uuid.uuid4()),
        workflow_id=MENTION_WORKFLOW_ID,
        status=RunStatus.COMPLETED,
        current_node="done",
        vars=vars_obj,
        output={"answer": reply},
        actor_id="gateway",
        session_id=session_id,
        created_at=asked_at,
        updated_at=utc_now().isoformat(),
    )
    svc.host.run_store.save(run)
    # run.started / run.finished on the account's event stream (every client of this person
    # learns of the new turn like any other).
    stream = getattr(getattr(svc, "runner", None), "user_stream", None)
    if stream is not None:
        from .events_stream import record_run

        record_run(stream, run)
    return run


# ONE ANSWER PER mention_id, durably (review 2026-11-09 #12): one record per (conversation,
# mention_id) in the plane's data folder, written atomically WITH the answer before the session
# turn is saved; the mirrors' completion is recorded separately. A resend reads it directly (never a
# bounded scan of the session's runs) and re-saves a session turn a crash left unsaved.
_IDEM_LOCK = threading.Lock()


def _idem_path(svc: Any, session_id: str) -> Any:
    import hashlib
    from pathlib import Path as _P

    digest = hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:40]
    return _P(svc.config.data_dir) / "mentions" / f"{digest}.json"


def _idem_read(svc: Any, session_id: str, mention_id: str) -> Optional[Dict[str, Any]]:
    import json as _json

    path = _idem_path(svc, session_id)
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rec = (data.get("mentions") or {}).get(str(mention_id)) if isinstance(data, dict) else None
    return dict(rec) if isinstance(rec, dict) else None


def _idem_write(svc: Any, session_id: str, mention_id: str, update: Dict[str, Any]) -> None:
    import json as _json
    import os as _os

    path = _idem_path(svc, session_id)
    with _IDEM_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = _json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data.setdefault("session_id", session_id)
        mentions = data.setdefault("mentions", {})
        mentions[str(mention_id)] = {**(mentions.get(str(mention_id)) or {}), **update}
        tmp = path.with_suffix(f".tmp-{uuid.uuid4().hex[:8]}")
        tmp.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
        _os.replace(tmp, path)


def _lane_cursor(svc: Any, session_id: str, key: str) -> int:
    import json as _json

    try:
        data = _json.loads(_idem_path(svc, session_id).read_text(encoding="utf-8"))
        return int(((data or {}).get("lane_cursors") or {}).get(key) or 0)
    except (OSError, ValueError, TypeError):
        return 0


def _set_lane_cursor(svc: Any, session_id: str, key: str, value: int) -> None:
    import json as _json
    import os as _os

    path = _idem_path(svc, session_id)
    with _IDEM_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = _json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data.setdefault("session_id", session_id)
        data.setdefault("lane_cursors", {})[key] = int(value)
        tmp = path.with_suffix(f".tmp-{uuid.uuid4().hex[:8]}")
        tmp.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
        _os.replace(tmp, path)


def _prior_answer(svc: Any, session_id: str, mention_id: str, principal: Any = None) -> Optional[Dict[str, Any]]:
    """The answer already given to this `mention_id` in this conversation (a resend), or None. A
    session turn a crash left unsaved is saved again from the record."""
    rec = _idem_read(svc, session_id, mention_id)
    if not rec or not isinstance(rec.get("response"), dict):
        return None
    turn = rec.get("turn") if isinstance(rec.get("turn"), dict) else None
    if turn and svc.host.run_store.load(str(rec["response"].get("run_id") or "")) is None:
        _record_session_turn(svc, session_id=session_id, principal=principal, run_id=rec["response"]["run_id"], **turn)
    out = dict(rec["response"])
    out["mirrors"] = list(rec.get("mirrors") or [])
    return out


def mention(
    *, svc: Any, registry: Any, visit_host: Any, principal: Any,
    session_id: str, entity: str, text: str, mention_index: int, repeat_of: Optional[str] = None,
    mention_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one mention (see the module doc). Raises MentionRefused, KeyError (unknown entity)."""
    from abstractruntime.core.models import utc_now

    from .entity_seat import SEAT_IDLE_TTL_S

    asked_at = utc_now().isoformat()  # the person's words arrive now; the answer comes later

    sid = str(session_id or "").strip()
    manifest = registry.manifest_for(entity)  # KeyError -> 404
    visitor = f"person:{str(getattr(principal, 'user_id', '') or '').strip() or 'operator'}"
    repeated = _check_repeat_of(svc, sid, repeat_of, principal=principal, text=text)
    mid = str(mention_id or "").strip() or None
    from .entity_phase import generation_of

    # The kill switch's fence (contract-phases §3.3): a Stop that lands while this mention's turn
    # runs moves the generation, and the answer is then neither recorded nor mirrored.
    generation = generation_of(registry.entities_dir, manifest.slug)
    with _session_lock(sid):
        if mid:
            # ONE ANSWER PER mention_id (mentions-review B): a resend returns the first result.
            earlier = _prior_answer(svc, sid, mid, principal)
            if earlier is not None:
                return earlier
        er = registry.get_entity_runtime(manifest.slug)
        live = visit_host._live_visit_run(er)
        reused = False
        if live is not None:
            # REUSE ONLY THE CALLER'S OWN MENTION VISIT (security, 2026-11-09 review K): the visit
            # must be one this door opened, in this session, with the caller among its stamped
            # participants. Anything else — another account's visit, an Entity-app visit, a visit
            # bound to a session id the caller merely posted — is someone else's conversation:
            # refused with a sentence that names no session, run or account.
            if not _own_mention_visit(live, session_id=sid, caller=visitor):
                # The caller's OWN other conversation may be named (it is theirs to go back to);
                # anyone else's never is.
                own_elsewhere = visitor in _stamped_participants(live)
                holder = str(getattr(live, "session_id", "") or "") if own_elsewhere else ""
                raise _elsewhere(manifest, sid, own_session=holder or None)
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
                raise _from_visit_refusal(e, manifest, sid) from None
            run_id = str(leg["run_id"])
        # The mentioned entity reads the conversation: the quoted recent turns precede the words. In
        # a visit lane the conversation is that visit's exchange; a reused visit is only sent what it
        # does not already hold.
        lane = _visit_lane_turn_lines(registry, visit_host, session_id=sid, caller=visitor, mentioned_slug=manifest.slug)
        # A REUSED mention visit already holds what earlier mentions quoted from the visit lane: the
        # per-entity source-visit cursor (review #13) skips those turns.
        lane_key = f"{manifest.slug}|{run_id}" if lane is not None else None

        def _context(after: Optional[str]) -> Tuple[str, Dict[str, Any]]:
            turns = lane
            if turns is not None and after and lane_key:
                turns = turns[_lane_cursor(svc, sid, lane_key):]
            return conversation_context(svc, sid, current_turn=repeated, after_visit_run=after, lane_turns=turns)

        context_block, context_report = _context(run_id if reused else None)
        visit_text = f"{context_block}\n\n{text}" if context_block else text
        try:
            turned = visit_host.turn(entity, run_id, text=visit_text, speaker=visitor, caller=visitor)
        except VisitRefused as e:
            if not (reused and e.code in ("not_waiting", "visit_not_found")):
                raise _from_visit_refusal(e, manifest, sid) from None
            # The reused visit closed under us (its idle close raced this mention, REVIEW-1 item 12):
            # a fresh visit, once.
            try:
                _pre, leg = visit_host.open_leg(
                    entity, participants=[visitor], visit_id=f"visit-{uuid.uuid4().hex[:12]}", session_id=sid,
                    channel_label=MENTION_CHANNEL, idle_seconds=float(SEAT_IDLE_TTL_S),
                )
                run_id, reused = str(leg["run_id"]), False
                context_block, context_report = _context(None)
                visit_text = f"{context_block}\n\n{text}" if context_block else text
                turned = visit_host.turn(entity, run_id, text=visit_text, speaker=visitor, caller=visitor)
            except VisitRefused as e2:
                raise _from_visit_refusal(e2, manifest, sid) from None
        reply = str(turned.get("reply") or "").strip()
        status = str(turned.get("status") or "")
        if status == "failed" or not reply or turned.get("turn_error"):
            # A failed model call answers the visit turn with the error and the visit stays open
            # (runtime 1306804); the mention's caller gets the plain sentence, never the error.
            why = str(turned.get("turn_error") or turned.get("error") or "") or "the turn produced no reply"
            logger.warning("mention: %s cannot answer: %s", manifest.slug, why)
            raise _cannot_answer(manifest, sid)
        from .entity_phase import GenerationMoved, fenced
        from .entity_visits import STOPPED_VISIT_SENTENCE

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
            **({"mention_id": mid} if mid else {}),
        }
        session_run_id = str(uuid.uuid4())
        out = {
            "run_id": session_run_id,
            "turn_id": turn_id,
            "author": author,
            "session_id": sid,
            "mention_index": int(mention_index),
            "repeat_of": repeated,
            "context": context_report,
            "reply": reply,
            "visit": {
                "run_id": run_id,
                "turn_id": turn_id,
                "visit_id": visit_ns.get("visit_id"),
                "entity_id": str(manifest.entity_id),
                "reused": reused,
            },
            "mirrors": [],
        }
        turn_fields = {"text": text, "reply": reply, "author": author, "prompt_author": person_author(principal),
                       "provenance": provenance, "asked_at": asked_at, "repeat_of": repeated}
        # THE KILL SWITCH'S FENCE around every persistence step (review #2): the generation seen at
        # the door is checked and the answer written in ONE locked step — a Stop that landed since
        # (even between the turn and here) means nothing is recorded and nothing is mirrored. The
        # lock never spans cognition: the turn has already returned.
        try:
            with fenced(registry.entities_dir, manifest.slug, generation):
                if mid:
                    _idem_write(svc, sid, mid, {"response": out, "turn": turn_fields, "mirrors": None})
                _record_session_turn(svc, session_id=sid, principal=principal, run_id=session_run_id, **turn_fields)
        except GenerationMoved:
            raise MentionRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume", session_id=sid) from None
        mirrors = _mirror_into_bound_visits(
            registry=registry, visit_host=visit_host, session_id=sid, mentioned=manifest, author=author, reply=reply,
            caller=visitor, generation=generation,
        )
        out["mirrors"] = mirrors
        if lane_key is not None:
            # Everything in the visit lane now (the mirrored turn included) is held by the visit
            # this mention used: the next reuse quotes only what comes after.
            try:
                after_lane = _visit_lane_turn_lines(registry, visit_host, session_id=sid, caller=visitor,
                                                    mentioned_slug=manifest.slug) or []
                _set_lane_cursor(svc, sid, f"{manifest.slug}|{run_id}", len(after_lane))
            except Exception:  # noqa: BLE001 - a lost cursor only re-quotes
                logger.exception("mention: the visit-lane cursor could not be recorded")
        if mid:
            try:
                _idem_write(svc, sid, mid, {"mirrors": mirrors})  # the mirrors' completion, recorded on its own
            except Exception:  # noqa: BLE001 - the answer stands; a resend just shows no mirrors
                logger.exception("mention: could not record the mirrors of mention_id %s", mid)
    return out


def _mirror_into_bound_visits(
    *, registry: Any, visit_host: Any, session_id: str, mentioned: Any, author: Dict[str, Any], reply: str,
    caller: str, generation: Optional[int] = None,
) -> list:
    """A VISIT LANE (the conversation is another entity's live visit, e.g. an Entity-app visit with
    nerin): the mentioned entity's reply is a turn of THAT visit, spoken by the mentioned entity,
    through the visit door (join the stamped participants, then `turn` with that speaker), so the
    visited entity hears it and its home's ledger and memory keep it like any visitor's words. The
    words read `<name> (entity): <reply>` (`speaker_line`, from the author field). The visited
    entity answers that turn like any other. One row per bound visit:
    `{entity_id, run_id, turn_id, reply}` or `{entity_id, run_id, error}` (a refusal there never
    undoes the mention, it is stated); a turn the visited entity's model failed carries the
    runtime's answer for it as `reply` plus `turn_error`, and that visit stays open."""
    from abstractruntime.core.turn_author import speaker_line

    out: list = []
    entities_dir = registry.entities_dir
    if not entities_dir.exists():
        return out
    for child in sorted(entities_dir.iterdir()):
        if not child.is_dir() or child.name.startswith(".") or child.name == mentioned.slug:
            continue
        if not any(child.glob("runtime_*.sqlite3")):
            continue  # never had a durable visit
        er = registry.get_entity_runtime(child.name)
        live = visit_host._live_visit_run(er)
        if live is None or str(getattr(live, "session_id", "") or "") != session_id:
            continue
        # Only a visit the CALLER is in (security, 2026-11-09 review K): a session id that names
        # someone else's visit is not the caller's conversation, and nothing is mirrored into it.
        if caller not in _stamped_participants(live):
            continue
        # Only the VISITED entity's own visit (the Entity app's lane), never the visit another
        # mention opened in this conversation: mentioned entities do not mirror into each other.
        from abstractruntime.core.run_origin import origin_of

        if str((origin_of(live.vars) or {}).get("client") or "") == MENTION_CHANNEL:
            continue
        row: Dict[str, Any] = {"entity_id": str(er.home.entity_id), "run_id": str(live.run_id)}
        # Mirror ADMISSION is fenced on the mentioned entity's generation (review #2): a Stop of the
        # mentioned entity since its answer means its words are not carried into another visit.
        # ATOMIC ADMISSION (Codex review 2 #3): the generation check and the join are ONE step under
        # the mentioned entity's lifecycle lock, so a Stop can no longer land between them; the turn
        # (cognition) runs outside the lock.
        from .entity_phase import GenerationMoved, fenced

        try:
            if generation is not None:
                with fenced(registry.entities_dir, mentioned.slug, generation):
                    visit_host.join_participant(child.name, live.run_id, str(mentioned.entity_id))
            else:
                visit_host.join_participant(child.name, live.run_id, str(mentioned.entity_id))
        except GenerationMoved:
            row["reply"] = None
            row["error"] = f"{_display_name(mentioned)} was stopped; nothing was carried into this visit."
            out.append(row)
            continue
        except VisitRefused as e:
            row["error"] = str(e.detail)
            out.append(row)
            continue
        if generation is not None:
            from .entity_phase import generation_of

            if generation_of(registry.entities_dir, mentioned.slug) != generation:
                # Stopped after the admission: his words still never reach the other visit.
                row["reply"] = None
                row["error"] = f"{_display_name(mentioned)} was stopped; nothing was carried into this visit."
                out.append(row)
                continue
        try:
            turned = visit_host.turn(child.name, live.run_id, text=speaker_line(author, reply),
                                     speaker=str(mentioned.entity_id))
            row["turn_id"] = str(turned.get("turn_id") or "") or None
            if turned.get("turn_error"):
                # The visited entity's model call failed on THIS turn: the runtime answered it with
                # the error and its visit stays open (runtime 1306804). That answer is the row's.
                row["reply"] = f"This turn could not be answered: {turned['turn_error']}"
                row["turn_error"] = str(turned["turn_error"])
            elif turned.get("status") == "failed" or not turned.get("answered"):
                # The visited entity did not answer THIS turn: never its previous reply (review D).
                row["reply"] = None
                logger.warning("mirrored turn into %s failed: %s", child.name, turned.get("error"))
                row["error"] = f"{_display_name(registry.manifest_for(child.name))} could not answer this turn."
            else:
                row["reply"] = str(turned.get("reply") or "")
        except VisitRefused as e:
            row["error"] = str(e.detail)
        out.append(row)
    return out


__all__ = ["CODE_CANNOT_ANSWER", "CODE_ELSEWHERE", "MENTION_WORKFLOW_ID", "MentionRefused", "entity_author", "mention", "person_author"]
