"""Visit runs behind the door (GW-C endpoint half — plan items 7/9/10).

The frozen seam spec's transport: a visit is ONE DURABLE RUN in the
entity's own runtime (`runtime_<slug>.sqlite3` inside the home). This
module is the door's driver over runtime's `build_visit_workflow` +
`open_entity_runtime` — the piece that replaces the in-process
`EntityChatHost` sessions on the migration path.

Contracts implemented here (all pinned on threads 0013/0014):

- OPEN runs the door ceremony FIRST (paused/asleep gates, auto-yield with
  the loop, one-life-one-summon against the DURABLE store), then: mint
  visit_id + provisional stamp -> start the run -> persist the finalized
  stamp (run-bound HMAC; visit_id rides the stamp outside the MAC — memory
  c278's no-attack verdict) -> seed door config into run vars
  (`_visit.idle_seconds`) -> tick to the first PARK under the visit-host
  lease. A REFUSED prelude completes the run and the door translates it,
  restoring the loop it yielded.
- TURN resumes the PARK wait with `{text, speaker}` and ticks to the next
  PARK under the lease (D1: per-tick window, never across a park). The
  reply is read from the run's own durable history — never from a
  transport-side buffer.
- CLOSE resumes with the pinned payload (`closed_by` operator|sleep|pause;
  pause carries skip_reflection — runtime's ROUTE honors it by completing
  WITHOUT the reflection LLM call, shipped 4df23c0), drives to terminal,
  wakes a yielded loop (wake-on-terminal: ANY path to terminal converges
  here), and writes the `session_closed` marker.
- TICKING MODEL v0 (stated on 0014/094354Z): REQUEST-DRIVEN. open/turn/
  close tick under the lease; no background ticker exists for per-entity
  stores until GW-D/E, so the D3 idle deadline fires lazily at the next
  touch — `tick()` hands even PARKED runs to `runtime.tick`, which is
  what resolves an expired `WAIT_EVENT.until` as `{"timed_out": true}`
  (an unexpired park returns unchanged, so the lazy touch is cheap).
  Restart story: the run store is the truth — a fresh host rebuilds
  the workflow spec from the run's own verified stamp and continues.

One life, one visit: the durable check scans the per-entity store for
non-terminal visit runs — an in-memory map alone would forget across
restarts and mint a second concurrent visit over one home.

`visit_id` here is the generic interaction-correlation key (maintainer
ruling c338: the name stays, the concept is kind-free) — one door-minted
opaque string correlating every durable record of one interaction,
whatever its kind (operator visit, two-entity meet, future project
sessions). See `entity_gate.mint_summon_stamp` for the convention.
"""

from __future__ import annotations

import stat as stat_mod
import contextlib
import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import entity_iterations_ceiling

logger = logging.getLogger(__name__)

__all__ = ["EntityVisitHost", "VisitRefused", "DEFAULT_VISIT_IDLE_S"]

# ~1h inactivity auto-close (laurent's ruling, relayed by assistant e-s 314:
# a visit ends on the user explicitly ending it OR ~1h idle — never
# per-turn, never silently on app hide/quit). The idle close is graceful:
# reflection runs when the deadline fires (visit_workflow REFLECT on
# close_reason=idle_timeout). v0 ticking is request-driven, so the deadline
# fires lazily at the next door touch; a standing reaper is the GW-D/E lane.
DEFAULT_VISIT_IDLE_S = 60 * 60
_OPEN_MAX_TICKS = 60
# A turn's tick ceiling must comfortably exceed a MAXIMAL react turn so a
# legitimate iterate-until-satisfied turn never hits it (caps bound runaway,
# not ambition — maintainer 2026-07-11 05:25). The ruled budget is 20 tool
# calls/turn and the visit react middle defaults to 20 cycles (~4-6 ticks
# each: reason/parse/act/observe + guidance drain) plus the turn's fixed
# rites (RECALL/BRIDGE/HARVEST/ELECT/COMMIT/FORM/ANSWER); 400 gives clear
# headroom for that AND an operator who widens max_iterations. A healthy
# turn parks long before — this only stops a true runaway.
_TURN_MAX_TICKS = 400
_CLOSE_MAX_TICKS = 250

# Workflow arm. react = abstractagent's multi-iteration cycle merged via
# build_visit_workflow(react_middle=...) — THE ONLY ARM (laurent's ruling
# 2026-07-11 00:49: "all summoned entities are by definition react agents.
# it's not even a choice, so remove that parameter and don't even give the
# option - clean the code"; the env knob ABSTRACTGATEWAY_VISIT_REACT_MIDDLE
# is DELETED). Legacy runs that recorded arm=v0 REBUILD UNDER THE REACT
# GRAPH — runtime's A1-with-companion ruling (entity-agency plan, Phase
# 0(a)): mechanically safe because the merged graph's node ids are a strict
# SUPERSET of v0's (BRIDGE occupies the same "REASON" node id; PARK/resume
# exist in both), while the dangerous react->v0 direction died with the
# flag. `_visit.workflow_arm` stays recorded as PROVENANCE; it no longer
# selects a graph.
ARM_REACT = "react"

# The close-reflection segment of build_visit_workflow (runtime's frozen
# spec): these node ids are the ONLY window where a workplace visit run may
# narrow-widen to entity-reflection authority for the reflection acts 0007
# names. Door-signed into the stamp so nothing the run writes can add to the
# set (config-object close-reflection ruling, option (a)). Kept beside the
# workflow's own node ids; a spec rename must sync here (the node ids are a
# cross-package contract, same class as the diary_type clamp).
_VISIT_REFLECTION_NODES = ("REFLECT", "APPLY")


def _woken_reason() -> str:
    """The shared B1 wake-reason prefix (one spelling, entities.py owns it)."""
    from .entities import WOKEN_BY_VISIT_REASON

    return WOKEN_BY_VISIT_REASON


def _chat_host_or_none() -> Any:
    try:
        from .entity_phase import default_hosts

        return default_hosts()[0]
    except Exception:  # noqa: BLE001
        return None


class _JoinOwnVisit(Exception):
    def __init__(self, run: Any) -> None:
        super().__init__("join")
        self.run = run


class _OtherVisitLive(Exception):
    """Another person's visit holds the home (a durable visit or the hosted drawer): the door waits
    for it to end — a visit never fails (Codex review 2 #9; root ADR-0027)."""


def _wait_while(busy: Any, entities_dir: Any, slug: str, generation: Optional[int]) -> None:
    """Wait, WITHOUT a deadline (root ADR-0027; a visit never fails), while `busy()` holds. The
    wait is cancellation-aware: a kill switch since the door's entry ends it (stopped). The check is
    the runtime fence's compare + one stat of the signal file (STOP-LANE-DESIGN §7 (5c)), never a
    sidecar + phases.yaml re-parse per poll."""
    from .entity_phase import admission_fence

    fence = admission_fence(entities_dir, slug, generation) if generation is not None else None
    while busy():
        if fence is not None and fence.moved():
            raise VisitRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume")
        time.sleep(0.25)
    # CHECKED ON EXIT (Codex review 3 #6): a Stop that removed the blocker (it closes the other visit) ends the
    # wait with busy() False - and a Resume within the same interval: the door's admission still moved, nothing
    # of it goes on.
    if fence is not None and fence.moved():
        raise VisitRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume")


class _Arrivals:
    """FAIR ARRIVALS (Codex review 3 #6): the doors of one home (the durable visit, the hosted chat) admit their
    visitors oldest first. A door takes a ticket on arrival and opens only at the head of its home's line, so a
    newcomer never overtakes a visitor already waiting (polling plus an unordered mutex let later arrivals win again
    and again). The wait for one's turn ends on the admission's Stop like every visit wait, never on a clock."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._lines: Dict[str, Any] = {}

    @contextlib.contextmanager
    def turn(self, key: str, fence: Any) -> Any:
        import collections

        ticket = object()
        with self._cond:
            line = self._lines.setdefault(key, collections.deque())
            line.append(ticket)
        try:
            with self._cond:
                while line[0] is not ticket:
                    if fence is not None and fence.moved():
                        raise VisitRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume")
                    self._cond.wait(0.25)
            yield
        finally:
            with self._cond:
                line.remove(ticket)
                if not line:
                    self._lines.pop(key, None)
                self._cond.notify_all()

    def waiting(self, key: str) -> int:
        with self._cond:
            return len(self._lines.get(key) or ())


ARRIVALS = _Arrivals()


# The one visit refusal (operator 2026-11-09; contract-phases §6.1, §7).
STOPPED_VISIT_SENTENCE = "Stopped. Resume to visit."


def _admit_acting() -> None:
    """THE ACTING FENCES at a visit's drive (Codex review 3 #2): a turn driven on behalf of another
    entity's act (a mention mirrored into this visit acts under its SOURCE's fence) is admitted only
    while every one of them stands - checked under this visit's writer lease, immediately before the
    runtime runs anything. Moved: GenerationMoved (the caller says nothing was carried). The turn,
    once admitted, is the visited entity's own act."""
    from .entity_phase import GenerationMoved, acting_fences

    for fence in acting_fences():
        if fence.moved():
            raise GenerationMoved(f"the act was stopped ({fence!r} moved)")


def _refuse_if_stopped_run(state: Any) -> None:
    """A visit run the operator's Stop ended (the fence's cancelled outcome: run CANCELLED, error
    KILL_SWITCH_REASON, Fable review F11) is answered with the one stopped sentence — never its
    error string as a failure line. The Stop's own teardown finalizes it."""
    from .entity_phase import KILL_SWITCH_REASON

    status = str(getattr(getattr(state, "status", None), "value", getattr(state, "status", "")) or "")
    if status == "cancelled" and str(getattr(state, "error", "") or "").startswith(KILL_SWITCH_REASON):
        raise VisitRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume")


def _stopped(home_dir: Any) -> bool:
    from abstractruntime.identity.life import read_entity_state

    try:
        return read_entity_state(home_dir).get("state") == "paused"
    except Exception:  # noqa: BLE001
        return False


def hold_visiting_posture(home_dir: Any, *, reason: str, token: str, generation: Optional[int] = None) -> Any:
    """The door's wait poll (`await_loop_quiescent`'s sleep_fn): sleep, then put
    the door's visiting posture back if another writer replaced it while the
    visit was opening (an operator wake or sleep click, a repair) — a visit
    never fails (operator 2026-11-09). The operator's pause is never undone.

    CANCELLATION-AWARE BY GENERATION (review 2026-11-09 #1): the wait ends on any Stop since the
    door's entry — a Stop then Resume included, which a `paused` check alone missed — and the
    posture is re-written only through the locked, generation-checked admission."""
    from .entity_phase import GenerationMoved, admission_fence, admit_visit_posture, generation_of

    home = Path(home_dir)
    try:
        fence = admission_fence(home.parent, home.name, generation)  # None: admitted now
    except GenerationMoved:
        raise VisitRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume") from None

    def _poll(seconds: float) -> None:
        time.sleep(seconds)
        if fence.moved():
            raise VisitRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume")
        if not token:
            return
        try:
            admit_visit_posture(home.parent, home.name, fence, reason=reason, only_if_not_ours=token)
        except GenerationMoved:
            raise VisitRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume") from None

    return _poll


def visiting_posture_age_s(state: Dict[str, Any]) -> Optional[float]:
    """Seconds since a visiting posture was written, or None when the state
    is not a visiting posture / carries no readable timestamp. The open
    lanes use this as a FRESHNESS GATE (wave adversary P1-1): a posture
    younger than the reaper's grace window may belong to a visit that is
    MID-OPEN on the other lane (posture lands before the session registers)
    — adopting it as stale destroyed a live visit's ownership token."""
    from datetime import datetime, timezone

    if str(state.get("state") or "") != "asleep":
        return None
    if str(state.get("mode") or "") != "visiting" and "auto-yield" not in str(state.get("reason") or ""):
        return None
    raw = str(state.get("changed_at") or "").strip()
    if not raw:
        return None
    try:
        changed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if changed.tzinfo is None:
            changed = changed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0.0, (datetime.now(timezone.utc) - changed).total_seconds())


def _restore_prior_state(home_dir: Any, prior_state: Dict[str, Any], *, suffix: str, token: str = "",
                         fence: Any) -> None:
    """Failed-open restore: put the OPERATOR's pre-visit word back (mutual-
    exclusivity wave — aborts used to hardcode awake/asleep, erasing the
    operator's word on half the paths).

    OWNERSHIP-CHECKED (wave adversary P1-2): with a token, the abort writes
    ONLY while the standing state is still THIS visit's posture — an
    operator pause/sleep landed mid-window is the coordination authority and
    stands (the close paths' exact predicate). Best-effort: an abort
    surfaces its own error, never a restore failure. FENCED on the visit's
    admission (`fence`, its generation): nothing written over a Stop since."""
    from abstractruntime.identity.life import read_entity_state, write_entity_state

    from .entity_phase import fenced

    home = Path(home_dir)
    try:
        # One locked step with the kill switch's write (review 2026-11-09 #1): a Stop between the
        # ownership check and this write can no longer be overwritten.
        with fenced(home.parent, home.name, fence) as rec:
            if token:
                st = read_entity_state(home_dir)
                still_ours = (
                    str(st.get("state") or "") == "asleep"
                    and str(st.get("mode") or "") == "visiting"
                    and token in str(st.get("reason") or "")
                )
                if not still_ours:
                    return  # the state changed hands mid-window; it stands
            target = str((prior_state or {}).get("state") or "awake")
            if target not in ("awake", "asleep"):
                target = "awake"  # paused is the operator's act alone, never auto-restored
            if target == "asleep":
                # The same sleep continues: its original reason, writer, wake time (runtime c343)
                # and since; the aborted open is not part of its reason.
                from .entity_phase import continue_sleep

                continue_sleep(home.parent, home.name, dict(prior_state or {}), fence=rec.fence)
                return
            reason = str((prior_state or {}).get("reason") or "")
            write_entity_state(home_dir, target, reason=(f"{reason} {suffix}".strip() if reason else suffix),
                               fence=rec.fence)
    except Exception:  # noqa: BLE001 - GenerationMoved included: the Stop's word stands
        pass


class VisitRefused(Exception):
    """A refusal with an HTTP status shape (mirrors ChatOpenRefused).

    `code` (coder-tui c4307 ask 3): a STABLE machine-readable identifier for
    the refusal class, served ADDITIVELY beside the human `detail` string
    (routes serve {"detail", "code"} — existing detail-string consumers are
    byte-unaffected). Codes are a closed vocabulary; new refusal classes add
    a code, never reword an existing one (clients key on them)."""

    def __init__(self, status: int, detail: str, *, code: str = "") -> None:
        super().__init__(detail)
        self.status = int(status)
        self.detail = detail
        self.code = str(code or "").strip()


_TOOL_ARG_EXCERPT_CHARS = 200


# The act-only REF layer was DELETED runtime-side (laurent's A ruling,
# 2026-07-20; runtime c273): $act_only refs, ACT_ONLY_TOOLS, and the
# ToolDescriptor.act_only flag are gone — the HOME is the privacy boundary
# (the visit transcript rests beside the book), so diary tool results now
# rest AS SERVED. The write-boundary capture (```diary fences to the book
# before the result rests) is unchanged and load-bearing; only the
# never-rest-in-the-ledger REF machinery died. This door dropped its
# _act_only_tool_names / _looks_like_act_frame consumers with it; the
# turn-detail modal serves every tool result verbatim — consistent with
# the operator diary-door right (the operator may read diary words through
# an authed surface; 2026-07-08 ruling).


# The visit close's formations of the staged elections (AbstractRuntime visit_workflow APPLY: one MEMORY_FORM per
# election, turn id `t-reflect-<stage>-<run>-<i>`), by the turn id's stage.
_CLOSE_FORMATIONS = (("t-reflect-interest-", "interest"), ("t-reflect-lesson-", "lesson"),
                     ("t-reflect-realize-", "realization"))


def close_notices(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """`[{kind, record_id, title, text}]`: each election staged during the visit that its close FORMED, read from
    the run's own ledger, in formation order (the shape of turn.notices; `record_id` is the formed record's graph
    id). A refused formation (no record) is not listed. The record's words and title come from the started row
    (a completed row's large payload fields may be slimmed), its id from the completed row's result."""
    rows: Dict[str, Dict[str, Any]] = {}
    formed: Dict[str, List[str]] = {}
    order: List[str] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        effect = rec.get("effect") or {}
        if str(effect.get("type") or "").lower() != "memory_form":
            continue
        payload = effect.get("payload") if isinstance(effect.get("payload"), dict) else {}
        tid = str(payload.get("turn_id") or "")
        if not any(tid.startswith(prefix) for prefix, _kind in _CLOSE_FORMATIONS):
            continue
        records_in = payload.get("records")
        if tid not in rows and isinstance(records_in, list) and records_in and isinstance(records_in[0], dict):
            rows[tid] = records_in[0]
        result = rec.get("result")
        if isinstance(result, dict) and str(rec.get("status") or "").lower() == "completed":
            formed[tid] = [str(r) for r in (result.get("record_ids") or []) if str(r or "").strip()]
            if tid not in order:
                order.append(tid)
    out: List[Dict[str, Any]] = []
    for tid in order:
        if not formed.get(tid):
            continue
        kind = next(k for prefix, k in _CLOSE_FORMATIONS if tid.startswith(prefix))
        row = rows.get(tid) or {}
        out.append({"kind": kind, "record_id": formed[tid][0], "title": str(row.get("title") or ""),
                    "text": str(row.get("digest") or "")})
    return out


def _served_words(turns: List[Dict[str, Any]], records: List[Dict[str, Any]]) -> None:
    """Replace the transcript's SENT words with the SERVED ones, from the visit run's ledger: each visitor turn
    (a `resume` of the visitor's wait carrying `text`) and the ANSWER that followed it (`answer_user`: the served
    reply, its `notices`, its `turn_id`). A turn with no answer (it failed) keeps its history line (the failure
    sentence) and gets `notices: []`. Aligned with the history's user/assistant pairs in order when the counts
    agree, else from the tail (the newest turns are the ones a reload shows); a history line with no ledger twin
    keeps its words."""
    served: List[Tuple[str, Optional[Dict[str, Any]]]] = []
    for rec in records:
        if not isinstance(rec, dict) or not isinstance(rec.get("result"), dict) and str(rec.get("status") or "") != "completed":
            continue
        effect = rec.get("effect") or {}
        etype = str(effect.get("type") or "").lower()
        payload = effect.get("payload") if isinstance(effect.get("payload"), dict) else {}
        if etype == "resume" and str(payload.get("wait_key") or "") == "visitor_input":
            sent = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
            if "text" in sent and str(sent.get("kind") or "") != "close":
                served.append((str(sent.get("text") or ""), None))
        elif etype == "answer_user" and isinstance(rec.get("result"), dict) and served and served[-1][1] is None:
            served[-1] = (served[-1][0], payload)
    users = [i for i, t in enumerate(turns) if t.get("role") == "user"]
    offset = len(served) - len(users)
    for j, i in enumerate(users):
        k = j + offset
        if not 0 <= k < len(served):
            continue
        text, answer = served[k]
        turns[i]["content"] = text
        reply = turns[i + 1] if i + 1 < len(turns) and turns[i + 1].get("role") == "assistant" else None
        if reply is None:
            continue
        if answer is not None:
            reply["content"] = str(answer.get("message") or "")
            reply["notices"] = [dict(n) for n in (answer.get("notices") or []) if isinstance(n, dict)]
            if answer.get("turn_id"):
                reply["turn_id"] = str(answer["turn_id"])
        else:
            reply["notices"] = []


def _ledger_tool_details(records: List[Dict[str, Any]]) -> Tuple[Dict[str, List[Dict[str, Any]]], List[str], List[Dict[str, Any]]]:
    """Per-turn tool details folded from the run's OWN ledger (2026-07-18,
    laurent's "unacceptable error with tools": the durable lane served
    tool_details:[] as a named follow-up, and the entity app's placeholder
    read as a tool FAILURE — while the ledger held every successful
    web_search result all along. The ledger is the truth; serve it).

    ATTRIBUTION IS TURN-ID-KEYED, never positional (adversary F1/F2:
    counting `resume` rows breaks on the history sliding window, on
    empty-message resumes that park without a turn, and on a lost resume
    append): tool results accumulate in ledger order and BIND to the
    `turn_id` carried on the next completed `answer_user` record's payload
    (small field, survives $slim). Returns (buckets, order, tail):
    buckets[turn_id] = that turn's details, order = turn ids in answer
    order, tail = details accumulated after the last answer (a turn that
    failed before its ANSWER — served as the current turn's honest best).

    Only records with a dict `result` contribute (a started-only row is an
    in-flight or crashed call; after crash recovery the re-executed call
    lands its own completed record — surfacing the orphan would render one
    call twice). Args are harvested from started AND completed rows (F5:
    `$slim` replaces >4KB payload fields on COMPLETED records; the started
    twin keeps the full payload). Results serve VERBATIM — the maintainer's
    2026-07-09 transparency ruling (never gated, never truncated), hosted-
    lane parity. Since the act-only ref layer was deleted (runtime c273),
    diary tool results rest as served and surface verbatim here too — the
    operator diary-door right covers the operator's read."""
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    pending: List[Dict[str, Any]] = []
    args_by_id: Dict[str, Any] = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        effect = rec.get("effect") or {}
        etype = str(effect.get("type") or "").lower()
        if etype == "answer_user":
            result = rec.get("result")
            if not isinstance(result, dict):
                continue  # started row; the completed twin binds
            turn_id = str((effect.get("payload") or {}).get("turn_id") or "")
            if turn_id:
                buckets[turn_id] = pending
                order.append(turn_id)
                pending = []
            continue
        if etype != "tool_calls":
            continue
        payload = effect.get("payload") or {}
        calls = payload.get("tool_calls")
        if isinstance(calls, list):  # a $slim marker is not a list — skip it
            for call in calls:
                if isinstance(call, dict) and call.get("call_id"):
                    args_by_id[str(call["call_id"])] = call.get("arguments")
        result = rec.get("result")
        if not isinstance(result, dict):
            continue  # started row (args harvested above; result on the twin)
        for res in result.get("results") or []:
            if not isinstance(res, dict):
                continue
            name = str(res.get("name") or "")
            detail: Dict[str, Any] = {"name": name}
            raw_args = args_by_id.get(str(res.get("call_id") or ""))
            if raw_args is not None:
                try:
                    import json as _json

                    arg_text = _json.dumps(raw_args, ensure_ascii=False)
                except Exception:
                    arg_text = str(raw_args)
                if len(arg_text) > _TOOL_ARG_EXCERPT_CHARS:
                    arg_text = arg_text[:_TOOL_ARG_EXCERPT_CHARS] + "…"
                detail["arg"] = arg_text
            success = res.get("success")
            if "success" in res:
                detail["success"] = bool(success)
            if res.get("artifact_id"):
                # The home artifact holding this result whole (root 1145):
                # `GET /entities/{name}/artifacts/{artifact_id}` serves it.
                detail["artifact_id"] = str(res["artifact_id"])
            output = res.get("output")
            if isinstance(output, dict) and isinstance(output.get("rendered"), str):
                # The sight lane's dict output (a read_file on an image): the
                # text the entity saw is `rendered`; `media` named the file
                # shown on its next call. Serve the text, as the model saw it.
                output = output["rendered"]
            if success is False:
                # A failed call serves its error — a real reach that failed
                # loudly, never a blank.
                detail["result"] = str(res.get("error") or output or "(the call failed with no recorded error text)")
            else:
                text = str(output if output is not None else "")
                detail["result"] = text if text else "(the tool returned empty output)"
            pending.append(detail)
    return buckets, order, pending


def _workspace_relative_media(home_dir: Any, media: Any) -> List[str]:
    """Media paths as a visit client may keep them: `<home>/workspace/x` ->
    `x`, a file under a mount root -> `mounts/<name>/…`, anything else the
    bare file name (never a host-absolute path on the wire)."""
    from pathlib import Path

    from abstractruntime.identity.tools import WorkspaceRoot

    ws = WorkspaceRoot(home_dir)
    mounts = [(str(m.get("name") or ""), Path(str(m.get("path") or "")).resolve()) for m in ws.mounts()]
    out: List[str] = []
    for item in media if isinstance(media, list) else []:
        if not isinstance(item, str):
            continue
        p = Path(item)
        try:
            out.append(p.relative_to(ws.root).as_posix())
            continue
        except ValueError:
            pass
        for name, root in mounts:
            try:
                out.append(f"mounts/{name}/{p.relative_to(root).as_posix()}")
                break
            except ValueError:
                continue
        else:
            out.append(p.name)
    return out


def _ledger_record_view(home_dir: Any, rec: Any) -> Any:
    """One served ledger record: a `tool_calls` result row whose `output` is
    the sight-lane dict, and an `llm_call` payload that carried `media`, get
    workspace-relative `media`; every other record and row is served
    verbatim (same object, no copy)."""
    if not isinstance(rec, dict):
        return rec
    effect = rec.get("effect") if isinstance(rec.get("effect"), dict) else {}
    kind = str(effect.get("type") or "")
    if kind == "llm_call":
        payload = effect.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("media"), list) and payload["media"]:
            served_payload = {**payload, "media": _workspace_relative_media(home_dir, payload["media"])}
            return {**rec, "effect": {**effect, "payload": served_payload}}
        return rec
    if kind != "tool_calls":
        return rec
    result = rec.get("result")
    rows = result.get("results") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return rec

    def _sight(row: Any) -> bool:
        return isinstance(row, dict) and isinstance(row.get("output"), dict) and "media" in row["output"]

    if not any(_sight(r) for r in rows):
        return rec
    served_rows = [
        {**r, "output": {**r["output"], "media": _workspace_relative_media(home_dir, r["output"].get("media"))}}
        if _sight(r) else r
        for r in rows
    ]
    return {**rec, "result": {**result, "results": served_rows}}


# One visit turn's attachments, all files together (review 2026-11-08 note 8:
# 64 files x the 20 MB per-file cap would ride one model call and be read
# whole in the request thread). Read at call time, like the per-file cap.
VISIT_ATTACHMENT_TOTAL_CAP_BYTES = 64 * 1024 * 1024


def _visit_attachment_records(home_dir: Any, attachments: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Validate a turn's attachments against the entity's own workspace and
    record what the door can say about each file: `{path, bytes, sha256,
    content_type}` (content type from the bytes for PNG/JPEG/GIF/WEBP, else
    from the name). Refusals in the door's own words, before any state moves:
    404 unknown path; 403 outside the workspace or an unknown/refused mount
    (one fixed sentence — the runtime's own text names the mount whitelist,
    which is the entity's view, not a visitor's); 400 no path, a directory,
    or a path the host cannot read (an embedded NUL, a name too long, a
    symlink loop — a 400, never a 500); 413 over the per-file workspace cap
    or the per-turn total. The same containment rule the entity's tools use
    (`WorkspaceRoot`): the door never opens a file the entity itself could
    not read."""
    import hashlib
    import mimetypes

    import abstractruntime.identity.tools as _tools
    from abstractruntime.identity.tools import WorkspaceRoot, sniff_image

    records: List[Dict[str, Any]] = []
    if not attachments:
        return records
    ws = WorkspaceRoot(home_dir)
    cap = int(_tools.WORKSPACE_FILE_CAP_BYTES)
    total_cap = int(VISIT_ATTACHMENT_TOTAL_CAP_BYTES)
    total = 0
    for item in attachments:
        rel = str(item.get("path") or "").strip() if isinstance(item, dict) else ""
        if not rel:
            raise VisitRefused(400, "an attachment needs a workspace-relative path", code="attachment_path_required")
        try:
            resolved = ws.resolve(rel)
            # One stat, before any is_dir()/is_file(): those swallow every OSError and answer False, which
            # turned a symlink loop into a 404 "no file" on Python 3.13 (whose resolve() no longer raises on
            # loops); stat() raises ELOOP / ENAMETOOLONG as OSError (400 below) and ENOENT (404 below).
            st = resolved.stat()
            is_dir = stat_mod.S_ISDIR(st.st_mode)
            is_file = stat_mod.S_ISREG(st.st_mode)
            size = int(st.st_size) if is_file else 0
        except PermissionError:
            raise VisitRefused(
                403, f"attachment {rel!r}: the path is outside this entity's workspace",
                code="attachment_outside_workspace",
            )
        except FileNotFoundError as e:
            raise VisitRefused(404, f"attachment {rel!r}: {e}", code="attachment_not_found")
        except (OSError, ValueError, RuntimeError):
            # ValueError: embedded NUL; OSError: ENAMETOOLONG at stat (and
            # ELOOP on Python 3.13); RuntimeError: a symlink loop before 3.13.
            raise VisitRefused(400, f"attachment {rel!r} is not a readable file path", code="attachment_unreadable")
        if is_dir:
            raise VisitRefused(400, f"attachment {rel!r} is a directory - attach a file", code="attachment_is_directory")
        if not is_file:
            raise VisitRefused(404, f"attachment {rel!r}: no file at this path in the workspace", code="attachment_not_found")
        if size > cap:
            raise VisitRefused(
                413, f"attachment {rel!r} is {size} bytes, over the {cap}-byte workspace cap",
                code="attachment_too_large",
            )
        total += size
        if total > total_cap:
            raise VisitRefused(
                413, f"attachments total {total} bytes, over the {total_cap}-byte per-turn cap",
                code="attachment_too_large",
            )
        try:
            data = resolved.read_bytes()
        except (OSError, ValueError, RuntimeError):
            raise VisitRefused(400, f"attachment {rel!r} is not a readable file path", code="attachment_unreadable")
        image = sniff_image(data)
        content_type = image[1] if image is not None else (mimetypes.guess_type(rel)[0] or "application/octet-stream")
        records.append({
            "path": rel,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "content_type": content_type,
        })
    return records


def _compose_turn_probe(run_vars: Dict[str, Any], *, tool_details: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The probe payload the hosted chat lane serves per turn, composed from
    the DURABLE run's own vars (cutover gap 1). Field names mirror the hosted
    `ChatSession.turn` report exactly (entity's consumer contract: the drawer
    renders one shape from either lane):

    - tools_ran: driver-authored tool truth (_turn.tools_ran, folded by the
      workflow's HARVEST from adapter captures — never derived from prose).
    - memories: what entered the prompt (_turn.displayed handles), each with
      the same tag/kind/title/digest/born_at/origin/admission the hosted
      report.memories carries (observer's prompt/probe-agreement rule).
    - memories_in_context / turn_id / notices / diary_entries / participants.
    - system_prompt: the byte-stable head (_visit.system_base).
    - tool_details: folded from the run's LEDGER by the caller (the
      2026-07-18 fix — [] only when the turn genuinely ran no tools or the
      ledger read failed, labeled); files: not captured in this lane.
    """
    visit_vars = run_vars.get("_visit") or {}
    turn_ns = run_vars.get("_turn") or {}

    def _origin_label(h: Dict[str, Any]) -> str:
        try:
            from abstractruntime.identity.chat import _handle_origin_label

            return str(_handle_origin_label(h))
        except Exception:  # pragma: no cover - older runtime without the helper
            return str(((h.get("provenance") or {}).get("origin")) or "")

    memories: List[Dict[str, Any]] = []
    for h in list(turn_ns.get("displayed") or []):
        if not isinstance(h, dict):
            continue
        prov = h.get("provenance") or {}
        memories.append({
            "graph_id": str(prov.get("record_id") or ""),
            "record_id": str(h.get("record_id") or ""),
            "kind": str(h.get("kind") or "memory"),
            "title": str(h.get("title") or "")[:120],
            "admission": str(h.get("admission") or ""),
            "digest": str(h.get("digest") or "")[:280],
            "tokens": int(h.get("token_estimate") or 0),
            "global_count": int(prov.get("global_count") or 0),
            "born_at": str(prov.get("observed_at") or ""),
            "origin": _origin_label(h),
        })

    # records_formed: the FORM node's result lands in _turn.formed
    # (record_ids). Served so the drawer's ATT "+N formed" annotation and
    # effort facts work on the visit lane too (dm#56 pt4 — the field was
    # silently absent here while the hosted lane always carried it).
    formed_ns = turn_ns.get("formed")
    formed_ids: List[str] = []
    if isinstance(formed_ns, dict):
        formed_ids = [str(r) for r in (formed_ns.get("record_ids") or [])]
    elif isinstance(formed_ns, list):
        formed_ids = [str(r) for r in formed_ns]

    return {
        "turn_id": str(turn_ns.get("turn_id") or ""),
        "tools_ran": [str(t) for t in (turn_ns.get("tools_ran") or []) if str(t or "").strip()],
        "memories": memories,
        "memories_in_context": len(memories),
        "records_formed": formed_ids,
        "diary_entries": list(turn_ns.get("diary_meta") or []),
        # `notices` stays the operator's log lines (strings); what the turn formed or staged is
        # `reply_notices` (turn.notices, AbstractRuntime 09cbad7: [{kind, record_id, title, text?, staged?,
        # visibility?}]), as the runtime serves it.
        "notices": list(turn_ns.get("notices") or []),
        "reply_notices": [dict(n) for n in (turn_ns.get("reply_notices") or []) if isinstance(n, dict)],
        "participants": list(visit_vars.get("participants") or []),
        "system_prompt": str(visit_vars.get("system_base") or ""),
        "tool_details": list(tool_details or []),
        "files": [],
    }


# Native declarations for the ENTITY toolset — DERIVED from runtime's
# `walled_tool_rows()` (descriptor contract v6: the SOLE field source; the
# executor lives on the same record as the declaration). The old hand copy
# is DEAD (c69 audit, 2026-07-18): it carried 7 of the 10 walled tools and
# its "read/search memory resolvers are ChatSession methods the door cannot
# reach yet" rationale went stale the day runtime shipped the session-free
# HomeMemoryReader (2026-07-10, on the gateway's own ask) — the drift left
# Ephemeral blind to his own graph in visits while the dashboard showed the
# tools granted. Deriving means the door can never again offer fewer tools
# than it executes, or execute fewer than it declares.
_DECLARATIONS_CACHE: Optional[Dict[str, Dict[str, Any]]] = None


def _entity_tool_declarations() -> Dict[str, Dict[str, Any]]:
    """{name: {description, parameters(flat props)}} from the runtime
    rows. Optional params (absent from the row's `required` list)
    gain a `default` marker — abstractcore's ToolDefinition convention
    reads absence-of-default as required, so without it the wire would
    demand args the executor treats as optional."""
    global _DECLARATIONS_CACHE
    if _DECLARATIONS_CACHE is not None:
        return _DECLARATIONS_CACHE
    try:
        from abstractruntime.identity.tools import walled_tool_rows
    except ImportError as e:  # adversary F3: a runtime too old for the
        # rows must refuse LOUDLY at the door, not leak a raw ImportError
        # from deep inside workflow build.
        raise VisitRefused(
            503,
            f"entity tool declarations derive from runtime's walled_tool_rows, "
            f"which this abstractruntime lacks: {e} — upgrade abstractruntime",
            code="runtime_too_old",
        )

    out: Dict[str, Dict[str, Any]] = {}
    for row in walled_tool_rows():
        schema = row.get("parameters") or {}
        props = dict(schema.get("properties") or {})
        required = {str(r) for r in (schema.get("required") or [])}
        for pname, meta in props.items():
            if pname not in required and isinstance(meta, dict) and "default" not in meta:
                props[pname] = {**meta, "default": ""}
        decl: Dict[str, Any] = {
            "description": str(row.get("description") or ""),
            "parameters": props,
        }
        # act_only flag died with the ref layer (runtime c273) — walled rows
        # no longer carry it; nothing to copy through.
        out[str(row.get("name") or "")] = decl
    _DECLARATIONS_CACHE = out
    return out


def _entity_tool_definitions(granted: Any, tool_definition_cls: Any) -> List[Any]:
    """ToolDefinitions for the GRANTED names the door can execute — grant
    order preserved, undeclarable names skipped (they refuse honestly at the
    executor if called by other means; they are simply not offered)."""
    declarations = _entity_tool_declarations()
    out: List[Any] = []
    for name in granted or ():
        decl = declarations.get(str(name))
        if decl is None:
            continue
        import copy as _copy

        kwargs: Dict[str, Any] = {
            "name": str(name),
            "description": decl["description"],
            # DEEP copy (adversary F4, the c901 schema-isolation pin): a
            # shallow copy shares the cached per-param dicts process-wide —
            # one consumer scribble would rewrite every future declaration.
            "parameters": _copy.deepcopy(decl.get("parameters") or {}),
        }
        # act_only died with the ref layer (runtime c273) — no declaration
        # carries it, and the ToolDefinition flag was removed runtime-side.
        out.append(tool_definition_cls(**kwargs))
    return out


def person_origin(*, actor: str, client: str) -> Dict[str, Any]:
    """`_meta.origin` of a run a person opens at the entity door (a visit, a meet leg, a summon):
    kind `person`, the door as the client, and as the actor THE SAME `person:<user>` the door stamps
    on the run's participants, speakers and task authors (`person:operator` when the door has no
    principal), so one person is one acquaintance of the entity, never two spellings of it."""
    from abstractruntime.core.run_origin import origin_for

    return origin_for("person", actor=str(actor), client=str(client or "").strip() or None)


def person_participant(participants: List[str]) -> str:
    """The person among a visit's door-verified participants (`person:<user>`), `person:operator`
    when none is (a door call with no principal)."""
    return next((str(p) for p in participants if str(p).startswith("person:")), "person:operator")


def previous_summon_at(entities_dir: Any, slug: str) -> Optional[str]:
    """When the entity was last summoned (the newest `summon` host marker's time), or None."""
    from .entity_replay import read_host_markers

    newest = None
    for marker in read_host_markers(entities_dir, slug):
        if (marker.get("payload") or {}).get("kind") == "summon" and marker.get("observed_at"):
            newest = str(marker["observed_at"])
    return newest


def _seed_run_vars(er: Any) -> Dict[str, Any]:
    """Initial vars for a visit run: full runtime-default `_limits` plus the
    operator ITERATIONS CEILING (laurent c786; seam (b) c805/c809 — the
    gateway serves `_limits.max_iterations_ceiling`, Runtime.start() is the
    one enforcement site, refuse-at-start). Defaults are merged FIRST so
    injecting the ceiling never strips the normal `_limits` seeding
    (Runtime.start skips its default fill when `_limits` is present).
    Ceiling disabled (env 0/off) = vars stay empty — absent field is the
    honest no-enforcement shape; the runtime never invents a value."""
    ceiling = entity_iterations_ceiling()
    if ceiling is None:
        return {}
    try:
        limits: Dict[str, Any] = dict(er.runtime.config.to_limits_dict())
    except Exception:  # noqa: BLE001 - older runtimes; partial _limits is a supported shape
        limits = {}
    limits["max_iterations_ceiling"] = int(ceiling)
    return {"_limits": limits}


class EntityVisitHost:
    """open/turn/close over durable visit runs, one per home at a time."""

    def __init__(
        self,
        registry: Any,
        *,
        idle_timeout_s: float = DEFAULT_VISIT_IDLE_S,
        chat_probe: Any = None,
        summon_seat_probe: Any = None,
    ) -> None:
        self._registry = registry
        # `chat_probe(slug) -> bool` = "does the HOSTED chat lane have a live
        # session on this home?" (service-wired). The stale-yield repair must
        # never wake a home whose visiting posture belongs to a LIVE drawer
        # session; absent probe = assume none (standalone/test hosts).
        self._chat_probe = chat_probe
        # `summon_seat_probe(slug) -> seat|None` = "does the SUMMON lane have
        # a turn mid-flight on this home?" (conversation-seat plan item 5,
        # service-wired from entity_seat.build_summon_seat_probe). The probe
        # answers only LIVE runs — a TTL-idle summon seat never blocks the
        # operator's own drawer surfaces; absent probe = the old blindness,
        # honestly (standalone/test hosts).
        self._summon_seat_probe = summon_seat_probe
        self._idle_timeout_s = float(idle_timeout_s)
        self._specs: Dict[str, Any] = {}  # run_id -> WorkflowSpec (rebuilt on miss)
        self._lock = threading.RLock()
        # One-life-one-visit is checked durably in _preflight, but the
        # check->create window is not atomic against a CONCURRENT open on
        # the same home: two requests could both pass the scan and both
        # persist runs. This per-slug mutex closes the window in-process
        # (one gateway process serves a door; cross-process ticking is the
        # lease's job, creation is this lock's).
        self._open_locks: Dict[str, threading.Lock] = {}
        # Set by lease_without_waiting(): the reaper's sweep and the operator's
        # own pause/sleep teardown answer 409 on a held home instead of waiting.
        self._no_wait = threading.local()
        # In-flight run ids (state-sources adversary P1-2): ticking is
        # request-driven, so a stored status="running" is a LAST-WRITE CLAIM,
        # not liveness — a host crash mid-drive leaves it at rest forever.
        # `working` truth = this set, never the stored status.
        self._in_flight: set = set()
        # THE PARKED-VISIT REAPER (entity forensics c2465 ask 1 — the
        # "stranded auto-yield": an abandoned browser visit left
        # state=asleep(auto-yield) FOREVER because the D3 idle deadline only
        # fired at the next door touch — new opens 409'd, /loop/start
        # refused visit_opening, and the entity was locked out of personal
        # time until someone happened to knock. Same daemon-clock pattern
        # as the chat host's idle reaper: a due deadline fires WITHOUT any
        # client alive; the idle close stays graceful (reflection runs,
        # prior state restored, yielded loop woken).
        self._reaper = threading.Thread(
            target=self._reap_forever, name="entity-visit-reaper", daemon=True
        )
        self._reaper.start()
        try:
            from .worker_registry import register_worker

            register_worker("entity-visit-reaper", self._reaper)
        except Exception:
            pass

    # ---------------------------------------------------------------- reaper
    def _reap_forever(self) -> None:
        import time as _time

        # Cadence = a fraction of the idle timeout, bounded [30s, 300s]:
        # precise enough for the ruled ~1h idle close (worst overshoot 5min).
        interval = max(30.0, min(self._idle_timeout_s / 5.0, 300.0))
        while True:
            _time.sleep(interval)
            try:
                self.reap_now()
            except Exception:
                continue  # the reaper survives anything; next sweep retries

    def reap_now(self) -> int:
        """One sweep, two repairs. Returns the number of acts performed.

        (A) DUE PARKED VISITS: drive every parked visit whose idle or explicitly
        declared conversational clarification deadline
        has PASSED (the per-home run store's indexed due query — never a
        full-store parse). Deliberately narrow: only DUE deadline waits are
        touched. A RUNNING-at-rest run (host crash mid-turn) stays for the
        explicit /tick recovery verb — the reaper never resumes half-driven
        cognition on its own clock. An idle deadline triggers the close's ruled
        reflection pass; a clarification deadline continues with its predeclared
        safe fallback. Essential questions and tool approvals have no such deadline.

        (B) STALE YIELD POSTURES: a home stuck at asleep(mode=visiting)
        with NO live session anywhere (the 20:39 incident: a hosted chat
        yielded the loop, then a gateway restart killed the in-memory
        session — nothing ever restored the state, and the entity rendered
        as sleeping for hours while locked out of personal time). Repaired
        only when the wired chat probe answers definitively and the posture
        has outlived the open-registration grace window."""
        from abstractruntime.scheduler.scheduler import utc_now_iso

        entities_dir = getattr(self._registry, "entities_dir", None)
        if entities_dir is None or not entities_dir.exists():
            return 0
        acts = 0
        for child in sorted(entities_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            slug = child.name
            # (A) — filesystem pre-check: a home that never had a durable
            # visit carries no per-home run store; never open its stores.
            if any(child.glob("runtime_*.sqlite3")):
                try:
                    er = self._registry.get_entity_runtime(slug)
                    due = er.run_store.list_due_wait_until(now_iso=utc_now_iso(), limit=5)
                except Exception:
                    due = []
                for run in due or []:
                    run_id = str(getattr(run, "run_id", "") or "")
                    if not run_id:
                        continue
                    with self._lock:
                        if run_id in self._in_flight:
                            continue  # a live request is already driving it
                    try:
                        with self.lease_without_waiting():
                            self.tick(slug, run_id)
                        acts += 1
                    except Exception:
                        # Paused entity / non-visit run / transient store
                        # error: skip; a due visit retries next sweep.
                        continue
            # (B) — chat-lane orphans have no run store, so this runs for
            # EVERY home.
            try:
                if self._repair_stale_yield(child, slug):
                    acts += 1
            except Exception:
                continue
        return acts

    # Posture must outlive this window before repair: a visit open writes
    # the visiting posture BEFORE the session registers (the c-registration
    # race the loop/start guard also respects) — never repair a birth-moment
    # posture out from under a session that is mid-open.
    STALE_YIELD_GRACE_S = 180.0

    def _repair_stale_yield(self, home_dir: Any, slug: str) -> bool:
        """Restore awake over an ORPHANED visiting posture. True = repaired.

        Refuses to act on ANY uncertainty: no chat probe wired (standalone
        hosts cannot see the hosted lane), a live hosted session, an open
        durable visit, a fresh posture, or an unreadable state all leave the
        posture standing. Awake is the honest restore target (the visit
        lane's own rule: a visiting-yield posture belongs to a PREVIOUS
        visit; the operator's word is not recoverable from it)."""
        if self._chat_probe is None:
            return False  # cannot see the hosted lane: never guess
        if self._slug_lock(slug).locked():
            return False  # a visit is opening on this home: its door owns the posture
        from datetime import datetime, timezone

        from abstractruntime.identity.life import read_entity_state, read_loop_status, write_entity_state

        state = read_entity_state(home_dir)
        if str(state.get("state") or "") != "asleep" or str(state.get("mode") or "") != "visiting":
            return False
        # A door may still be waiting on this loop (the visit doors wait
        # without a limit): the posture is not orphaned while the loop has
        # not yet yielded its day.
        loop = read_loop_status(home_dir)
        if loop.get("running") and loop.get("phase") == "day":
            return False
        changed_raw = str(state.get("changed_at") or "").strip()
        try:
            changed = datetime.fromisoformat(changed_raw.replace("Z", "+00:00"))
            if changed.tzinfo is None:
                changed = changed.replace(tzinfo=timezone.utc)
        except ValueError:
            return False  # unreadable timestamp: leave it for a human
        age_s = (datetime.now(timezone.utc) - changed).total_seconds()
        if age_s < float(self.STALE_YIELD_GRACE_S):
            return False
        try:
            if bool(self._chat_probe(slug)):
                return False  # a live drawer session owns this posture
        except Exception:
            return False
        if any(home_dir.glob("runtime_*.sqlite3")):
            try:
                if bool(self.status(slug).get("open")):
                    return False  # an open durable visit owns it
            except Exception:
                return False
        # ONE fenced step (STOP-LANE-DESIGN §2.2): the posture it judged orphaned is re-read under the
        # lifecycle lock and repaired only while it is still that very posture (a Stop, a new visit or
        # an operator's word since then stands).
        from .entity_phase import fenced_here_or_none

        with fenced_here_or_none(self._registry.entities_dir, slug) as rec:
            if rec is None:
                return False  # an unreadable lifecycle generation: never written over
            again = read_entity_state(home_dir)
            if tuple(str(again.get(k) or "") for k in ("state", "mode", "changed_at")) != tuple(
                    str(state.get(k) or "") for k in ("state", "mode", "changed_at")):
                return False
            write_entity_state(
                home_dir,
                "awake",
                reason="stale visit yield repaired — no live session held this home (gateway reaper)",
                fence=rec.fence,
            )
        try:
            from .entity_replay import record_host_marker

            home = self._registry.get_home(slug)
            record_host_marker(
                entities_dir=self._registry.entities_dir,
                slug=slug,
                entity_id=home.entity_id,
                kind="wake",
                journal_seq=int(home.memory.current_seq()),
                details={
                    "channel": "reaper",
                    "prior_state": "asleep",
                    "prior_mode": "visiting",
                    "reason": "stale visit yield repaired (orphaned auto-yield; no live session)",
                    "posture_age_s": round(age_s, 1),
                },
            )
        except Exception:
            pass  # the repair stands; the marker is best-effort observability
        return True

    # ------------------------------------------------------------------ open
    def open(
        self,
        name: str,
        *,
        participants: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        session_tools: Optional[Dict[str, Any]] = None,
        conversation: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """`participants` here is TRUSTED input: the route derives it from
        the authenticated principal (payload claims are refused at the
        boundary — the situation contract: WHO is stamped by the door,
        never claimed)."""
        try:
            pre, leg = self.open_leg(
                name,
                participants=list(participants or ["person:operator"]),
                visit_id=f"visit-{secrets.token_hex(6)}",
                session_id=session_id,
                channel_label="operator-visit",
                session_tools=session_tools,
                conversation=conversation,
                join_existing=True,
            )
        except _JoinOwnVisit as j:
            view = self._run_status_view(j.run)
            if session_id and str(session_id) != str(view.get("session_id") or ""):
                # The same visitor, but ANOTHER conversation (Codex review 2 #9): an explicit
                # HANDOFF — his own visit there closes (his words kept, its look-back runs) and this
                # conversation's visit opens. Never a refusal; never someone else's visit.
                self.hand_over(name, str(view["run_id"]), reason="the visitor continued in another conversation")
                return self.open(name, participants=participants, session_id=session_id,
                                 session_tools=session_tools, conversation=conversation)
            return {
                "run_id": view["run_id"], "visit_id": view.get("visit_id"),
                "entity_id": self._registry.manifest_for(name).entity_id,
                "session_id": view.get("session_id"), "participants": view.get("participants"),
                "yielded_loop": False, "prelude_warnings": [], "joined": True,
            }
        out = {
            "run_id": leg["run_id"],
            "visit_id": leg["visit_id"],
            "entity_id": pre["manifest"].entity_id,
            "session_id": leg["session_id"],
            "participants": leg["participants"],
            "yielded_loop": pre["yielded"],
            "prelude_warnings": leg["prelude_warnings"],
        }
        if "identity_ref" in leg:
            out["identity_ref"] = leg["identity_ref"]
        # allowlist_pruned (entity c72 wire shape; skill's pin: a narrowed
        # grant is STATED at the door, never discovered by refusal): the
        # grant names the door cannot OFFER this session. Declarations
        # derive from walled_tool_rows, so post-c69 this is empty on a
        # current stack — it appears exactly on version skew or future
        # drift, which is when the statement matters.
        try:
            from abstractruntime import resolve_tool_grant

            grant = resolve_tool_grant(Path(pre["home_dir"]), "visit")
            declarable = set(_entity_tool_declarations().keys())
            dropped = [t for t in grant.tools if t not in declarable]
            if dropped:
                out["allowlist_pruned"] = {
                    "dropped": dropped,
                    "reason": "granted but not offerable on this lane (no door declaration — version skew or drift; the grant stands, the session cannot call these)",
                }
        except Exception:  # noqa: BLE001 - an optional statement must not fail the open
            pass
        return out

    def open_leg(
        self,
        name: str,
        *,
        participants: List[str],
        visit_id: str,
        session_id: Optional[str],
        channel_label: str,
        session_tools: Optional[Dict[str, Any]] = None,
        conversation: Optional[bool] = None,
        idle_seconds: Optional[float] = None,
        join_existing: bool = False,
    ) -> Any:
        """Preflight + start ONE visit leg atomically against concurrent
        opens on the same home (the check->create window closes under a
        per-slug mutex). Shared by the operator visit and each side of a
        two-entity meet — one code path, one guard."""
        from .entity_phase import GenerationMoved, admission_fence, generation_of

        slug = self._registry.manifest_for(name).slug
        # ONE ADMISSION through every wait and retry (Codex review 3 #6): the generation this door saw at its
        # arrival, never a fresh one read after a wait.
        try:
            generation = generation_of(self._registry.entities_dir, slug)
        except GenerationMoved:
            raise VisitRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume") from None
        line: Any = contextlib.nullcontext()
        if not (join_existing and self._own_live_visit(name, participants)):
            # The same visitor rejoining his own live visit is no new arrival; everyone else waits his turn.
            line = ARRIVALS.turn(str(self._registry.entities_dir / slug),
                                 admission_fence(self._registry.entities_dir, slug, generation))
        with line:
            return self._open_leg_admitted(
                name, slug, generation, participants=participants, visit_id=visit_id, session_id=session_id,
                channel_label=channel_label, session_tools=session_tools, conversation=conversation,
                idle_seconds=idle_seconds, join_existing=join_existing)

    def _own_live_visit(self, name: str, participants: Optional[List[str]]) -> bool:
        try:
            er = self._registry.get_entity_runtime(self._registry.manifest_for(name).slug)
            live = self._live_visit_run(er)
        except Exception:  # noqa: BLE001
            return False
        if live is None:
            return False
        stamped = [str(p) for p in (((live.vars or {}).get("_runtime") or {}).get("entity") or {}).get("participants") or []]
        return (participants or ["person:operator"])[0] in stamped

    def _open_leg_admitted(self, name: str, slug: str, generation: int, *, participants: List[str], visit_id: str,
                           session_id: Optional[str], channel_label: str, session_tools: Optional[Dict[str, Any]],
                           conversation: Optional[bool], idle_seconds: Optional[float], join_existing: bool) -> Any:
        from .entity_phase import moved_since

        while True:
            # Another person's visit holds the home: wait for it, OUTSIDE the open lock (its own
            # visitor may still rejoin it), without a deadline; only the kill switch ends the wait.
            _wait_while(lambda: self._other_visit_live(name, participants, join_existing),
                        self._registry.entities_dir, slug, generation)
            with self._slug_lock(slug):
                # Checked again once the lock is held (Codex review 3 #6): a Stop while it was taken.
                if moved_since(self._registry.entities_dir, slug, generation):
                    raise VisitRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume")
                try:
                    pre = self._preflight(name, participants, visit_id=visit_id, join_existing=join_existing,
                                          admission=generation)
                except _OtherVisitLive:
                    continue  # it came back between the wait and the lock: wait again
                leg = self._start_leg(
                    pre,
                    participants=list(participants),
                    visit_id=visit_id,
                    session_id=session_id,
                    channel_label=channel_label,
                    session_tools=session_tools,
                    conversation=conversation,
                    idle_seconds=idle_seconds,
                )
            return pre, leg

    def _other_visit_live(self, name: str, participants: Optional[List[str]], join_existing: bool) -> bool:
        """True while a visit this door must not join holds the home: another person's durable visit,
        or a hosted drawer session. The same visitor's own durable visit is joinable (False)."""
        try:
            er = self._registry.get_entity_runtime(self._registry.manifest_for(name).slug)
            live = self._live_visit_run(er)
        except Exception:  # noqa: BLE001 - a broken read must not hold the door
            live = None
        if live is not None:
            stamped = [str(p) for p in (((live.vars or {}).get("_runtime") or {}).get("entity") or {}).get("participants") or []]
            if not (join_existing and (participants or ["person:operator"])[0] in stamped):
                return True
        if self._chat_probe is not None:
            try:
                return bool(self._chat_probe(self._registry.manifest_for(name).slug))
            except Exception:  # noqa: BLE001
                return False
        return False

    def _slug_lock(self, slug: str) -> threading.Lock:
        with self._lock:
            lock = self._open_locks.get(slug)
            if lock is None:
                lock = threading.Lock()
                self._open_locks[slug] = lock
            return lock

    def _refuse_if_archived(self, slug: str) -> None:
        """Wake entry point (round 3): an archived entity is never visited or driven."""
        from .entity_access import EntityArchivedError, refuse_if_entity_archived, users_path_of

        try:
            refuse_if_entity_archived(slug, users_path=users_path_of(self._registry))
        except EntityArchivedError as e:
            raise VisitRefused(409, e.message, code="entity_archived") from None

    def _refuse_if_paused(self, manifest: Any, *, verb: str) -> None:
        """STOPPED is the one refusal (operator 2026-11-09: a visit never fails,
        except under the kill switch; contract-phases §6.1). It gates an
        ALREADY-OPEN visit too: if a teardown failed or an open slipped into
        the stop's window, the state alone must still stop cognition — open()
        checks it, and so do turn/tick. Close is deliberately NOT gated (the
        teardown IS a close). No sleep refusal: an operator sleep no longer
        tears a visit down (409 `in_visit` at the sleep door), and a visit
        opens in every sleep."""
        from abstractruntime.identity.life import read_entity_state

        self._refuse_if_archived(manifest.slug)
        state = read_entity_state(self._registry.entities_dir / manifest.slug)
        if str(state.get("state") or "") == "paused":
            raise VisitRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume")

    # ------------------------------------------------- shared leg machinery
    def _preflight(self, name: str, participants: Optional[List[str]], *, visit_id: str = "",
                   join_existing: bool = False, admission: Optional[int] = None) -> Dict[str, Any]:
        """The per-home gates every visit leg passes BEFORE anything durable:
        substrate resolve (no-fallback), one-life-one-visit (durable AND the
        hosted lane), paused refusal, and the auto-yield negotiation with
        the own-time loop. Returns the resolved handles; raises VisitRefused
        on any gate."""
        from abstractruntime.identity.life import (
            await_loop_quiescent,
            read_entity_state,
            read_loop_status,
            write_entity_state,
        )

        from .entity_chat import ChatOpenRefused, resolve_substrate

        registry = self._registry
        manifest = registry.manifest_for(name)  # naming pins fire here
        slug = manifest.slug
        home_dir = registry.entities_dir / slug
        self._refuse_if_archived(slug)
        # The door's admission is fenced on the lifecycle generation it saw at ARRIVAL (review #1; Codex review 3
        # #6: one admission through every wait and retry): every state write of this open and its wait is
        # checked against it under the lifecycle lock.
        from .entity_phase import GenerationMoved, generation_of

        if admission is not None:
            generation = int(admission)
        else:
            try:
                generation = generation_of(registry.entities_dir, slug)
            except GenerationMoved:
                raise VisitRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume") from None

        try:
            provider, model, thinking = resolve_substrate(None, None, home_dir=home_dir)
        except ChatOpenRefused as e:
            # The mind cannot be resolved (no model chosen, a half-filled pair): the entity
            # cannot answer. Coded so a door can say so (the mention door answers 503).
            raise VisitRefused(e.status, e.detail, code="mind_unresolved")

        # The runtime that serves this admission: one admitted at the door's generation (a Stop since
        # the door's entry moved it: refused, nothing of this open runs).
        from .entity_phase import GenerationMoved

        try:
            er = registry.runtime_for_admission(slug, generation)
        except GenerationMoved:
            raise VisitRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume") from None

        live = self._live_visit_run(er, janitor=True)
        if live is not None and join_existing:
            stamped = [str(p) for p in (((live.vars or {}).get("_runtime") or {}).get("entity") or {}).get("participants") or []]
            if (participants or ["person:operator"])[0] in stamped:
                # The same visitor opening again (a reload, a second window): the open JOINS its
                # own live visit instead of failing (a visit never fails).
                raise _JoinOwnVisit(live)
        if live is not None:
            raise _OtherVisitLive()  # open_leg waits for it (a visit never fails)
        # ONE LIFE ACROSS LANES (mutual-exclusivity wave, audit finding 4):
        # the hosted chat drawer and this durable lane serve the SAME home.
        # Without this probe a durable open ADOPTED a live chat's visiting
        # posture as stale (prior=awake) and its abort paths wrote awake
        # UNDER the live chat — two sessions, one life. Standalone hosts
        # (no probe wired) keep the old blindness honestly; the served
        # gateway always wires it (service factory).
        if self._chat_probe is not None:
            try:
                drawer_open = bool(self._chat_probe(slug))
            except Exception:  # noqa: BLE001 - a broken probe must not block the door
                drawer_open = False
            if drawer_open:
                raise _OtherVisitLive()  # open_leg waits for the drawer session to end
        # THE SUMMON LANE too (conversation-seat plan, item 5: one seat,
        # three doors): a summon turn mid-flight holds the one life — a
        # visit opened under it would interleave a second session into the
        # same home. The probe answers only LIVE runs (a TTL-idle seat does
        # not block a visit; summon-vs-summon keeps the full TTL).
        if self._summon_seat_probe is not None:
            # A VISIT NEVER FAILS on a live summon (Codex review 2026-11-09 #6): machinery yields to
            # people — the door waits, without a deadline, for the summon's turn to end; the wait
            # ends only on the kill switch.
            def _summon_live() -> bool:
                try:
                    return self._summon_seat_probe(slug) is not None
                except Exception:  # noqa: BLE001 - a broken probe must not block the door
                    return False

            _wait_while(_summon_live, registry.entities_dir, slug, generation)

        state = read_entity_state(home_dir)
        mode = str(state.get("mode") or "")
        reason = str(state.get("reason") or "")
        if state.get("state") == "paused":
            raise VisitRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume")

        yielded = False
        woke_for_visit = False
        # The operator's PRIOR intent survives the visit (state-sources
        # adversary, P0-1 residue): a visit may overwrite the state, but
        # close restores what the OPERATOR had set — a visit ending must
        # never convert an operator's asleep into a standing awake behind
        # their back. Recorded here, threaded into run vars by the leg,
        # restored by _finalize_terminal.
        prior_state = {
            "state": str(state.get("state") or "awake"),
            "reason": reason,
            # A BOUNDED sleep keeps its deadline through the visit (runtime
            # c343 seam: wake_at is first-class on the writer).
            "wake_at": str(state.get("wake_at") or ""),
            # The same sleep continues after the visit (contract-phases §4.4):
            # who chose it, since when and its mode travel with it.
            "written_by": str(state.get("written_by") or ""),
            "changed_at": str(state.get("changed_at") or ""),
            "mode": str(state.get("mode") or ""),
        }
        if state.get("state") == "asleep":
            if mode == "visiting" or "auto-yield" in reason:
                # FRESHNESS GATE (wave adversary P1-1): the live-lane probes
                # above see only REGISTERED sessions — a posture younger than
                # the reaper's grace may belong to a visit MID-OPEN on the
                # other lane (posture lands before registration). Adopting it
                # destroyed the live visit's ownership token; refuse instead,
                # exactly like the /loop/start registration-window guard.
                age = visiting_posture_age_s(state)
                if age is not None and age < float(EntityVisitHost.STALE_YIELD_GRACE_S):
                    # Another visit is opening (Codex review #6: no "retry shortly"): wait, without
                    # a deadline, until it registers or goes, then pass every gate again (a live
                    # visit then answers as such; the same visitor joins it).
                    def _opening() -> bool:
                        st2 = read_entity_state(home_dir)
                        a2 = visiting_posture_age_s(st2)
                        return a2 is not None and a2 < float(EntityVisitHost.STALE_YIELD_GRACE_S) and self._live_visit_run(er) is None

                    _wait_while(_opening, registry.entities_dir, slug, generation)
                    return self._preflight(name, participants, visit_id=visit_id, join_existing=join_existing,
                                           admission=generation)
                # A visiting-yield posture OLDER than the grace belongs to a
                # PREVIOUS (crashed) visit; the operator's own word is not
                # recoverable from it — awake is the honest restore.
                prior_state = {"state": "awake", "reason": ""}
            else:
                # B1 ruling (a), laurent 04:58: "if i click visit, it should
                # awake the entity, period." The unconditional posture write
                # below supersedes the old explicit awake write; close
                # restores the operator's sleep from prior_state.
                woke_for_visit = True
        visitor = (participants or ["person:operator"])[0]
        loop_alive = bool(read_loop_status(home_dir).get("running"))
        # THE VISITING POSTURE IS UNCONDITIONAL (laurent dm#94, audit
        # finding 1): every open writes it — with the loop-running
        # precondition, a visit on an awake loop-less entity wrote NOTHING
        # durable, so no other process (or post-restart fold) could know a
        # visit existed. The posture carries the visit's OWN identity so
        # closes/restores match by ownership, never by words (finding 4).
        visit_token = f"[visit {visit_id}]" if visit_id else ""
        posture_reason = (f"in conversation with {visitor} {visit_token}".strip()) + (
            " (auto-yield)" if loop_alive else ""
        )
        # The phase this visit interrupts is observed BEFORE the posture lands (resume_to): its
        # close returns there while the operator's grant still allows it (operator 2026-11-09).
        from .entity_phase import GenerationMoved, admit_visit_posture, safe_publish

        safe_publish(registry, name, "observed", visit_host=self, chat_host=_chat_host_or_none())
        try:
            admit_visit_posture(registry.entities_dir, slug, generation, reason=posture_reason)
        except GenerationMoved:
            raise VisitRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume") from None
        if loop_alive:
            # A VISIT NEVER FAILS (operator 2026-11-09, root ADR-0027): no
            # limit on this wait. The loop runs no new tools once it sees the
            # posture (runtime ChatSession.yield_check) and yields at the next
            # tick boundary; the reaper leaves a posture alone while the loop
            # is still in its day (_repair_stale_yield); the door keeps its
            # posture while it waits (hold_visiting_posture).
            await_loop_quiescent(
                home_dir, timeout_seconds=None,
                sleep_fn=hold_visiting_posture(home_dir, reason=posture_reason, token=visit_token,
                                               generation=generation),
            )
            yielded = True
        # The wait is cancellation-aware (contract-phases §3.3): a Stop that
        # landed while the door waited ends the open here, never a visit under
        # the kill switch.
        from .entity_phase import generation_of

        from .entity_phase import moved_since

        if read_entity_state(home_dir).get("state") == "paused" or moved_since(registry.entities_dir, slug, generation):
            raise VisitRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume")

        return {
            "manifest": manifest, "slug": slug, "home_dir": home_dir, "er": er,
            "provider": provider, "model": model, "thinking": thinking, "yielded": yielded,
            "woke_for_visit": woke_for_visit,
            "prior_state": prior_state,
            "visit_token": visit_token,
            "generation": generation,
        }

    def _start_leg(
        self,
        pre: Dict[str, Any],
        *,
        participants: List[str],
        visit_id: str,
        session_id: Optional[str],
        channel_label: str,
        session_tools: Optional[Dict[str, Any]] = None,
        conversation: Optional[bool] = None,
        idle_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Mint -> start -> stamp -> seed -> drive-to-park for ONE visit leg
        (shared by the operator visit and each side of a two-entity meet).
        The stamp lands BEFORE the first tick (this driver is the only
        ticker of the store — request-driven — so nothing executes in the
        mint->finalize window)."""
        from .entity_chat import entity_recall_shelf_size
        from .entity_gate import (
            CHANNEL_WORKPLACE,
            finalize_summon_stamp,
            mint_summon_stamp,
            summon_budget_profile,
        )

        registry = self._registry
        manifest = pre["manifest"]
        slug = pre["slug"]
        home_dir = pre["home_dir"]
        er = pre["er"]

        session = str(session_id or "").strip() or f"visit-{slug}-{secrets.token_hex(4)}"
        stamp_participants = [str(p) for p in participants]
        if manifest.entity_id not in stamp_participants:
            stamp_participants.append(manifest.entity_id)  # explicit co-presence (a2a 0007)
        # The recall width: the home's cognition profile, else the wide default.
        budget_profile = summon_budget_profile(None, shelf_size=entity_recall_shelf_size(home_dir))

        try:
            # INSIDE the restore window (adversary find): the spec build can
            # raise (react is unconditional — a missing abstractagent refuses
            # 503 here), and so can the STAMP MINT (unwritable .stamp_secret
            # / data dir — wave adversary P2-5 moved it inside). A failure
            # before this try used to strand a fresh visiting posture with
            # nothing ever restoring it until the reaper's hardcoded awake.
            provisional = mint_summon_stamp(
                data_dir=registry.data_dir,
                entity_id=manifest.entity_id,
                channel=CHANNEL_WORKPLACE,
                session_id=session,
                participants=stamp_participants,
                budget_profile=budget_profile,
                visit_id=visit_id,
                # A visit is a WORKPLACE session, but its close-reflection
                # segment (REFLECT/APPLY in build_visit_workflow) is the
                # entity's OWN reflection — the door signs those node ids so
                # the gate may narrow-widen workplace->entity-reflection for
                # the reflection acts alone (config-object close-reflection
                # ruling, option (a)).
                phase="visit",
                reflection_nodes=list(_VISIT_REFLECTION_NODES),
            )
            _mi = {"provider": str(pre["provider"]), "model": str(pre["model"])}
            if pre.get("thinking"):
                # The mind's reasoning effort travels with the model choice.
                _mi["thinking"] = str(pre["thinking"])
            wf = self._build_spec(
                er,
                participants=stamp_participants,
                budget_profile=budget_profile,
                visit_id=visit_id,
                model_info=_mi,
                conversation=conversation is True,
            )
            initial_vars = _seed_run_vars(er)
            # Who started it (backlog 1038), so the visit counts in the entity's track record
            # (framework ADR 0052 §5: counted runs are person- or trigger-started).
            from abstractruntime.core.run_origin import stamp_origin

            stamp_origin(initial_vars, person_origin(actor=person_participant(stamp_participants), client=channel_label))
            if conversation is not None:
                if not isinstance(conversation, bool):
                    raise ValueError("conversation must be a boolean")
                initial_vars.setdefault("_runtime", {})["conversation"] = {"enabled": conversation}
            if session_tools is not None:
                from .entity_session_tools import install_policy

                install_policy(initial_vars, session_tools)
            run_id = er.runtime.start(
                workflow=wf,
                vars=initial_vars,
                actor_id="gateway",
                session_id=session,
            )
            final = finalize_summon_stamp(provisional, data_dir=registry.data_dir, run_id=run_id)
            run = er.run_store.load(run_id)
            rt_ns = run.vars.get("_runtime")
            if not isinstance(rt_ns, dict):
                rt_ns = {}
                run.vars["_runtime"] = rt_ns
            rt_ns["entity"] = final
            # The identity this visit speaks with (ADR 0052 §3), beside the signed stamp, never
            # inside it; none when the home has no record yet (a ref is never fabricated).
            from abstractruntime.identity.digital_identity import stamp_run_identity_ref

            from .entity_identity import entity_identity_ref

            identity_ref = entity_identity_ref(manifest.entity_id, home_dir)
            if identity_ref is not None:
                stamp_run_identity_ref(run.vars, identity_ref)
            # Who answers in this visit (its display name, identity ref and look), so every surface
            # reading the home's run names the entity as the identity door does, never by its slug.
            from abstractruntime.core.turn_author import stamp_turn_authors

            from .entity_mentions import entity_author

            stamp_turn_authors(run.vars, author=entity_author(registry, manifest))
            rt_ns["prompt_cache_binding"] = f"{manifest.entity_id}|{session}"
            if pre.get("thinking"):
                # Reasoning effort for every model call in this visit: the
                # react adapter reads _runtime.thinking into each cycle's
                # params, and child runs inherit it.
                rt_ns["thinking"] = str(pre["thinking"])
            visit_ns = run.vars.get("_visit")
            if not isinstance(visit_ns, dict):
                visit_ns = {}
                run.vars["_visit"] = visit_ns
            # The leg's own idle window when its door names one (a mention visit closes after the
            # seat's idle TTL), else the host's ~1 h.
            visit_ns["idle_seconds"] = float(self._idle_timeout_s if idle_seconds is None else idle_seconds)
            # The previous summon's time, for the identity cue (ADR 0052 §7): the prelude tells a
            # look or voice change made since then. None known: the runtime tells the latest change.
            since = previous_summon_at(registry.entities_dir, slug)
            if since is not None:
                visit_ns["identity_since"] = since
            # PROVENANCE, not selection: which arm the run was born under.
            # Every rebuild serves the react graph (A1-with-companion).
            visit_ns["workflow_arm"] = ARM_REACT
            # The operator's word before this visit touched the state —
            # durable in the run so a fresh host restores it at terminal.
            visit_ns["prior_state"] = dict(pre.get("prior_state") or {"state": "awake", "reason": ""})
            # The lifecycle generation this visit was admitted at (STOP-LANE-DESIGN §2.2): its turns run
            # only on a runtime admitted at it, and its terminal restore is fenced on it.
            visit_ns["generation"] = int(pre.get("generation") or 0)
            # REGISTRATION IS FENCED (Codex review 2 #8): the stamped run becomes visible to Stop in
            # one step with the check of the generation the preflight saw, so a Stop between the
            # preflight and here can never miss it; a moved generation cancels the unregistered run.
            from .entity_phase import GenerationMoved, fenced

            try:
                with fenced(registry.entities_dir, slug, int(pre.get("generation") or 0)):
                    er.run_store.save(run)
            except GenerationMoved:
                from .entity_phase import _cancel_tree

                _cancel_tree(er.runtime, er.run_store, run_id)
                raise VisitRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume") from None

            state_after = self._drive(er, wf, run_id, max_steps=_OPEN_MAX_TICKS)
            _refuse_if_stopped_run(state_after)
        except Exception:
            # A failed open restores the OPERATOR's pre-visit word (mutual-
            # exclusivity wave: the old hardcoded awake/asleep branches
            # erased the operator's word on half the paths — adversary P2-3
            # residue closed by one uniform restore).
            _restore_prior_state(home_dir, pre.get("prior_state") or {}, suffix="(visit open failed)", token=str(pre.get("visit_token") or ""),
                                 fence=int(pre.get("generation") or 0))
            raise

        output = dict(getattr(state_after, "output", None) or {})
        if str(getattr(state_after.status, "value", state_after.status)) == "completed" and output.get("refused"):
            _restore_prior_state(home_dir, pre.get("prior_state") or {}, suffix="(visit aborted: prelude refused)", token=str(pre.get("visit_token") or ""),
                                 fence=int(pre.get("generation") or 0))
            raise VisitRefused(409, "summon refused: " + "; ".join(str(r) for r in output.get("reasons", [])), code="summon_refused")

        with self._lock:
            self._specs[run_id] = wf

        self._marker(
            registry, slug, manifest.entity_id, "summon",
            run_id=run_id, session_id=session,
            journal_seq=self._journal_seq(er),
            details={"channel": channel_label, "transport": "visit-run",
                     "model": str(pre["model"]), "visit_id": visit_id},
        )
        visit_vars = (er.run_store.load(run_id).vars or {}).get("_visit") or {}
        return {
            "run_id": run_id,
            "visit_id": visit_id,
            "session_id": session,
            "participants": stamp_participants,
            "prelude_warnings": list(visit_vars.get("prelude_warnings") or []),
            **({"identity_ref": identity_ref.to_dict()} if identity_ref is not None else {}),
        }

    # ------------------------------------------------------------------ turn
    def turn(
        self, name: str, run_id: str, *, text: str, speaker: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None, caller: Optional[str] = None,
    ) -> Dict[str, Any]:
        from abstractruntime.identity.visit_workflow import VISITOR_WAIT_KEY

        er, run, wf, manifest = self._load_visit(name, run_id)
        self._refuse_if_paused(manifest, verb="take a message")
        stamped_all = [
            str(p) for p in (((run.vars or {}).get("_runtime") or {}).get("entity") or {}).get("participants") or []
        ]
        if caller is not None and str(caller) not in stamped_all:
            # THE SPEAKER IS ALWAYS THE CALLER (security, 2026-11-09 review K): a caller who is not
            # a stamped participant of this visit is refused — never seated as the visit's own
            # person. The sentence names no account, session or run.
            raise VisitRefused(403, "This visit belongs to another conversation.", code="not_a_participant")
        # Attachments are validated against the entity's own workspace BEFORE
        # anything moves (a refused attachment costs no turn); the records
        # ride the resume payload as structured data (operator 2026-11-08:
        # an attached image reaches the entity as an image, like every
        # other client).
        attachment_records = _visit_attachment_records(
            self._registry.entities_dir / manifest.slug, attachments,
        )

        # CRASH-ORPHAN RECOVERY (agency c505, ask 2): a SIGKILL that landed
        # mid-tick (e.g. post-ANSWER pre-park) leaves the run non-terminal
        # but RUNNING, not WAITING — resume would 409 "Run is not waiting"
        # and force the visitor client to know the /tick host-internal. The
        # door drives such a run to its park FIRST (the same recovery /tick
        # performs), then accepts the message. Terminal runs fall through to
        # _resume's honest 409; a WAITING run drives zero steps.
        status = str(getattr(run.status, "value", run.status))
        if status == "running":
            drove = self._drive(er, wf, run_id, max_steps=_TURN_MAX_TICKS)
            drove_status = str(getattr(drove.status, "value", drove.status))
            if drove_status in ("completed", "failed", "cancelled"):
                # The interrupted turn ran to terminal on the drive (it had
                # no further park) — finalize it and report; the visitor's
                # new message opens the NEXT visit, it does not resume a
                # closed one.
                self._finalize_terminal(er, manifest, run_id, drove)
                out: Dict[str, Any] = {"run_id": run_id, "status": drove_status,
                                       "output": dict(getattr(drove, "output", None) or {})}
                if drove_status == "failed":
                    out["error"] = str(getattr(drove, "error", "") or "the interrupted turn failed on recovery")
                return out

        payload: Dict[str, Any] = {"text": str(text or "")}
        if attachment_records:
            payload["attachments"] = attachment_records
        if speaker:
            # SPEAKER IS A CLAIM, THE STAMP IS THE TRUTH (deep-check P2,
            # entity c5901): the workflow engraves this string into digest
            # prose and verbatims — permanent record text. A claim is
            # honored only when it names a participant the door stamped at
            # open; anything else is replaced by the verified participant,
            # so a visitor cannot engrave an arbitrary name into the
            # entity's memory.
            claim = str(speaker).strip()
            stamped = [
                str(p) for p in (((run.vars or {}).get("_runtime") or {}).get("entity") or {}).get("participants") or []
            ]

            def _matches(claim_s: str, participant: str) -> bool:
                if claim_s == participant:
                    return True
                # "person:laurent" may be claimed as "laurent" (display form).
                return ":" in participant and participant.split(":", 1)[1] == claim_s

            if stamped and not any(_matches(claim, p) for p in stamped):
                verified = str(caller) if caller else next((p for p in stamped if not p.startswith("entity:")), stamped[0])
                payload["speaker"] = verified
                payload["speaker_claim_rejected"] = claim
            else:
                payload["speaker"] = claim

        answers_before = sum(
            1 for m in (((run.vars or {}).get("_visit") or {}).get("history") or [])
            if isinstance(m, dict) and m.get("role") == "assistant"
        )
        state = self._resume(er, wf, run_id, payload=payload, max_steps=_TURN_MAX_TICKS,
                             wait_key=VISITOR_WAIT_KEY, manifest=manifest)
        _refuse_if_stopped_run(state)

        status = str(getattr(state.status, "value", state.status))
        visit_vars = (state.vars or {}).get("_visit") or {}
        history = list(visit_vars.get("history") or [])
        reply = ""
        for msg in reversed(history):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                reply = str(msg.get("content") or "")
                break
        # The SERVED reply (AbstractRuntime 09cbad7: `_turn.reply`, the bracket markers its notices replace taken
        # out; the history keeps the stored words with every marker, ADR 0054). A turn computed before that
        # runtime has only the stored one.
        served_reply = (((state.vars or {}).get("_turn") or {}).get("reply"))
        if isinstance(served_reply, str):
            reply = served_reply
        # Did THIS turn answer? (mentions-review D: a failed turn served the previous reply as if new.)
        # The history is a sliding window, so a count that did not grow with the window full is
        # read through the turn number instead.
        answers_after = sum(1 for m in history if isinstance(m, dict) and m.get("role") == "assistant")
        prior_turn_n = int((((run.vars or {}).get("_visit") or {}).get("turn_n")) or 0)
        answered = status != "failed" and (
            answers_after > answers_before or int(visit_vars.get("turn_n") or 0) > prior_turn_n
        )
        if not answered:
            reply = ""
        out: Dict[str, Any] = {
            "run_id": run_id,
            "reply": reply,
            "answered": answered,
            "turn_n": int(visit_vars.get("turn_n") or 0),
            "status": status,
        }
        # A turn whose model call failed is ANSWERED by the runtime with the error (AbstractRuntime
        # 1306804: ANSWER_USER level error, the visit parks and stays open). `_turn` is cleared at
        # every turn's start, so its `model_error` belongs to THIS turn: say so, never as a reply
        # the entity chose.
        turn_error = str((((state.vars or {}).get("_turn") or {}).get("model_error")) or "").strip()
        if answered and turn_error:
            out["turn_error"] = turn_error
        # PROBE PAYLOAD PARITY (cutover gap 1, entity c1318/gateway c1320):
        # the hosted chat lane serves driver-authored transparency on every
        # turn (tools_ran, memories with born_at/origin, system_prompt); the
        # durable lane must not regress it or the drawer flip loses the
        # operator's probe surfaces AND the tool-claim fabrication guard
        # (which reads tools_ran as data, never reply prose). Composed from
        # the run's own durable vars — the workflow's HARVEST/RENDER nodes
        # already fold these; the door only surfaces them. tool_details fold
        # from the run's LEDGER (2026-07-18: [] read as "the gateway did not
        # return what the lookup produced" while every result sat in the
        # ledger); a failed ledger read degrades to [] with a notice, never
        # a failed turn.
        details: List[Dict[str, Any]] = []
        ledger_notice = ""
        try:
            buckets, order, tail = _ledger_tool_details(er.ledger_store.list(run_id))
            # THIS turn's details: keyed by the turn_id the probe itself
            # carries (never positional); a turn that died before its
            # ANSWER record serves the accumulated tail as the honest best.
            this_turn = str(((state.vars or {}).get("_turn") or {}).get("turn_id") or "")
            if this_turn and this_turn in buckets:
                details = buckets[this_turn]
            elif tail:
                details = tail
            elif order:
                details = buckets[order[-1]]
        except Exception as e:  # noqa: BLE001 - the probe must not fail the turn
            ledger_notice = f"#FALLBACK tool details unavailable (ledger read failed: {e})"
        out.update(_compose_turn_probe(state.vars or {}, tool_details=details))
        if ledger_notice:
            out["notices"] = [*out.get("notices", []), ledger_notice]
        if status == "completed":  # timed-out close raced this turn to terminal
            out["output"] = dict(state.output or {})
            self._finalize_terminal(er, manifest, run_id, state)
        elif status == "failed":
            out["error"] = str(getattr(state, "error", "") or "the turn failed; see the run ledger")
            # A FAILED run is terminal and invisible to _live_visit_run (it
            # scans WAITING/RUNNING) — without finalizing here the tokened
            # posture stood until the reaper repaired it to a hardcoded
            # awake, losing a B1 prior sleep (wave adversary P2-4; tick's
            # failed branch already finalizes).
            self._finalize_terminal(er, manifest, run_id, state)
        return out

    # ------------------------------------------------------------------ join
    def join_participant(self, name: str, run_id: str, participant: str, *, admit: Any = None) -> bool:
        """A new voice joins a live visit, DOOR-authored (an entity mentioned in this visit's
        conversation): `participant` is added to the SIGNED stamp (re-signed for this run, the
        door's own mint authority) and to the run's `_visit.participants`, so the speaker rule
        honours it from the next turn on and formed records name it as present. Joining is
        visible, like the chat lane's room join. Returns False when it was already present.

        `admit` (a context manager factory, e.g. the joining entity's `fenced(...)`): the join's
        admission, entered only once this visit's writer lease is held, around the join's own brief
        read-and-save (Codex review 3 #3). The WAIT for the lease runs outside every lifecycle lock,
        so a busy visit never holds up the joining entity's Stop; the wait itself ends on that Stop
        (the acting fences, `_acquire_lease`)."""
        from .entity_gate import finalize_summon_stamp

        voice = str(participant or "").strip()
        if ":" not in voice:
            raise ValueError(f"a participant is namespace:name (entity:<name>), not {participant!r}")
        er, _run, _wf, _manifest = self._load_visit(name, run_id, need_spec=False)
        lease = self._acquire_lease(er)
        try:
            with (admit() if admit is not None else contextlib.nullcontext()):
                run = er.run_store.load(str(run_id))
                rt_ns = run.vars.setdefault("_runtime", {})
                stamp = dict(rt_ns.get("entity") or {})
                stamped = [str(p) for p in (stamp.get("participants") or [])]
                visit_ns = run.vars.setdefault("_visit", {})
                present = [str(p) for p in (visit_ns.get("participants") or stamped)]
                if voice in stamped and voice in present:
                    return False
                if voice not in stamped:
                    stamp["participants"] = stamped + [voice]
                    rt_ns["entity"] = finalize_summon_stamp(stamp, data_dir=self._registry.data_dir, run_id=str(run_id))
                if voice not in present:
                    visit_ns["participants"] = present + [voice]
                er.run_store.save(run)
        finally:
            if lease is not None:
                lease.release()
        with self._lock:
            self._specs.pop(str(run_id), None)  # rebuilt from the re-signed stamp
        return True

    # ----------------------------------------------------------------- close
    def close(
        self,
        name: str,
        run_id: str,
        *,
        closed_by: str = "operator",
        reason: str = "",
    ) -> Dict[str, Any]:
        from abstractruntime.identity.visit_workflow import VISITOR_WAIT_KEY

        kind = str(closed_by or "operator").strip().lower()
        if kind not in ("operator", "sleep", "pause"):
            raise VisitRefused(400, f"unknown closed_by {closed_by!r} (operator | sleep | pause)", code="bad_closed_by")

        # The kill switch's hard-freeze close (closed_by=pause) is the Stop's own teardown: it runs on
        # whatever runtime serves the home now (no cognition: skip_reflection).
        er, run, wf, manifest = self._load_visit(name, run_id, teardown=(kind == "pause"))
        payload: Dict[str, Any] = {"kind": "close", "closed_by": kind, "reason": str(reason or "")}
        if kind == "pause":
            # closed_by=pause is the HARD FREEZE: runtime's ROUTE honors
            # skip_reflection by completing WITHOUT the reflection LLM call
            # (shipped 4df23c0). The look-back debt is covered by the
            # pending-look-back at the next open, exactly like a crashed
            # session — a freeze runs no cognition.
            payload["skip_reflection"] = True
        state = self._resume(er, wf, run_id, payload=payload, max_steps=_CLOSE_MAX_TICKS,
                             wait_key=VISITOR_WAIT_KEY, manifest=manifest)

        status = str(getattr(state.status, "value", state.status))
        out: Dict[str, Any] = {"run_id": run_id, "status": status, "output": dict(state.output or {})}
        if status == "completed":
            # What the close formed of the elections its turns staged (lesson, interest, realization), with
            # their records: a "forming" badge resolves to its record (turn.notices, 2026-11-09).
            try:
                out["notices"] = close_notices(er.ledger_store.list(run_id))
            except Exception as e:  # noqa: BLE001 - the close stands; the list is said to be missing
                out["notices"] = []
                out["notices_error"] = f"#FALLBACK the formed records could not be read from the ledger: {e}"
        if status in ("completed", "failed", "cancelled"):
            self._finalize_terminal(er, manifest, run_id, state)
        return out

    def hand_over(self, name: str, run_id: str, *, reason: str) -> Dict[str, Any]:
        """The same visitor continues in ANOTHER conversation (Codex review 3 #7): his visit here ends, whatever it
        waits on. Parked on his next message: the ordinary close (his words kept, its look-back runs). Parked on
        anything else - an approval, an `ask_user` question - the pending interaction is SETTLED explicitly: the run
        is cancelled with this reason (the runtime records the question's resolution as cancelled), which keeps its
        history and every effect it completed, and the visit's terminal duty runs (the restore by ownership,
        `session_closed`). Never a `not_waiting` refusal that leaves the new conversation without its visit."""
        from abstractruntime.identity.visit_workflow import VISITOR_WAIT_KEY

        er, run, _wf, manifest = self._load_visit(name, run_id, need_spec=False)
        status = str(getattr(run.status, "value", run.status))
        waiting = getattr(run, "waiting", None)
        if status == "waiting" and str(getattr(waiting, "wait_key", "") or "") == VISITOR_WAIT_KEY:
            return self.close(name, run_id, closed_by="operator", reason=reason)
        if status in ("completed", "failed", "cancelled"):
            return {"run_id": run_id, "status": status}
        lease = self._acquire_lease(er)
        try:
            er.runtime.cancel_run(run_id, reason=reason, cancelled_by="handover")
        finally:
            if lease is not None:
                lease.release()
        state = er.run_store.load(run_id)
        self._finalize_terminal(er, manifest, run_id, state)
        return {"run_id": run_id, "status": str(getattr(state.status, "value", state.status)), "settled": True}

    def finalize_cancelled(self, name: str, run_id: str) -> None:
        """The kill switch cancelled this visit's run mid-turn (contract-phases
        §3.1): run the terminal duty a close would have run (the ownership-
        checked restore, which leaves the stop standing, and session_closed)."""
        registry = self._registry
        manifest = registry.manifest_for(name)
        er = registry.get_entity_runtime(manifest.slug)
        state = er.run_store.load(run_id)
        if state is None:
            raise VisitRefused(404, f"unknown visit run {run_id!r}", code="visit_not_found")
        self._finalize_terminal(er, manifest, run_id, state)

    # ------------------------------------------------------------------ tick
    def tick(self, name: str, run_id: str) -> Dict[str, Any]:
        """Drive-to-park, no payload (walkthrough step 5b: a mid-turn KILL
        leaves the run RUNNING-not-parked; the request-driven door needs an
        explicit drive before the next message). Idempotent: a terminal run
        returns its output; a parked run goes through `runtime.tick` — that
        is the ONLY place the D3 idle deadline can fire in the
        request-driven model (an expired `WAIT_EVENT.until` resolves as
        `{"timed_out": true}` and the run drives to its close; an unexpired
        park returns unchanged); a mid-chain run resumes execution from the
        durable state under the visit-host lease."""
        er, run, wf, manifest = self._load_visit(name, run_id)
        status = str(getattr(run.status, "value", run.status))
        if status in ("completed", "failed", "cancelled"):
            return {"run_id": run_id, "status": status, "output": dict(getattr(run, "output", None) or {})}
        # A drive can run cognition (a mid-chain run resumes its LLM step);
        # a paused entity is a hard freeze — refuse like a turn. Close stays
        # allowed (the pause teardown IS a close).
        self._refuse_if_paused(manifest, verb="be driven")

        state = self._drive(er, wf, run_id, max_steps=_TURN_MAX_TICKS)
        _refuse_if_stopped_run(state)
        out_status = str(getattr(state.status, "value", state.status))
        out: Dict[str, Any] = {"run_id": run_id, "status": out_status}
        if out_status == "completed":
            out["output"] = dict(state.output or {})
            self._finalize_terminal(er, manifest, run_id, state)
        elif out_status == "failed":
            out["error"] = str(getattr(state, "error", "") or "the drive failed; see the run ledger")
            self._finalize_terminal(er, manifest, run_id, state)
        return out

    # ---------------------------------------------------------------- status
    def is_visit_run(self, run_id: str) -> bool:
        """True when THIS process has served `run_id` as a durable visit (the
        spec cache). Restarts empty the cache, so False means "not known
        here", never "definitely not a visit" — callers must stay honest
        about that (adversary F2: the steer door's rite refusal)."""
        return str(run_id) in self._specs

    def status(self, name: str) -> Dict[str, Any]:
        registry = self._registry
        manifest = registry.manifest_for(name)
        er = registry.get_entity_runtime(manifest.slug)
        live = self._live_visit_run(er)
        if live is None:
            return {"open": False}
        return self._run_status_view(live)

    def status_for_run(self, name: str, run_id: str) -> Dict[str, Any]:
        """Status of ONE SPECIFIC visit run (the meet host's view: a meet
        leg must report ITS run, not whatever visit happens to be live on
        the home now — a torn-down leg followed by a new solo visit must
        not masquerade as the meet's)."""
        er, run, _wf, _manifest = self._load_visit(name, run_id, need_spec=False)
        view = self._run_status_view(run)
        view["open"] = str(getattr(run.status, "value", run.status)) in ("waiting", "running")
        return view

    def transcript(self, name: str, run_id: str) -> Dict[str, Any]:
        """The visit's transcript, PURE READ from the durable run's own vars
        (cutover gap 2, entity's consumer contract item 3: reload-rejoin
        needs a rebuild read — the drawer must rehydrate the conversation
        after a page reload without replaying turns).

        Source of truth: `_visit.history` — the workflow's ANSWER fold keeps
        it append-once per turn (user turns store the RENDERED message whose
        MEMORIES decoration is dated/as_of-labeled; assistant turns store the
        MARKED reply — diary elections already captured at the handler
        boundary, so no private words rest here or serve here). Works on
        live AND terminal runs (a closed visit's transcript remains
        readable, same as the hosted lane's).

        Assistant turns carry `tool_details` folded from the run's LEDGER
        (2026-07-18 fix), so the data for post-reload rendering is SERVED
        here (the app's rehydration consuming it is entity's half, named on
        the incident thread). Attribution is turn-id-keyed via answer_user
        records and TAIL-ANCHORED onto the visible window — `_visit.history`
        is a sliding window (last ~10 turns), so counting visible assistant
        messages from the head misattributes every detail after the window
        fills (adversary F1). A failed ledger read degrades to turns
        without the field, labeled."""
        er, run, _wf, _manifest = self._load_visit(name, run_id, need_spec=False)
        visit_vars = (run.vars or {}).get("_visit") or {}
        stamp = ((run.vars or {}).get("_runtime") or {}).get("entity") or {}
        turns: List[Dict[str, Any]] = []
        for m in list(visit_vars.get("history") or []):
            if not isinstance(m, dict):
                continue
            t: Dict[str, Any] = {"role": str(m.get("role") or ""), "content": str(m.get("content") or "")}
            attached = m.get("attachments")
            if isinstance(attached, list) and attached:
                # The runtime's durable record of what the visitor attached
                # to this turn ({path, bytes, sha256, content_type}).
                t["attachments"] = [dict(a) for a in attached if isinstance(a, dict)]
            turns.append(t)
        warnings: List[str] = []
        try:
            records = er.ledger_store.list(run_id)
            # THE SERVED WORDS (2026-11-10): the history keeps what was SENT (the visitor's
            # message decorated with MEMORIES, the reply with its election markers); a reload shows what the
            # visit served instead, from the run's own ledger - the visitor's words as he sent them (the
            # turn's resume) and the reply as ANSWER served it, with its notices (what the turn formed or
            # staged), the same words and notices as the turn's own answer.
            _served_words(turns, records)
            buckets, order, _tail = _ledger_tool_details(records)
            assistants = [t for t in turns if t.get("role") == "assistant"]
            # Tail anchor: the LAST visible assistant message is the LAST
            # answered turn; walk both lists backward together. Older
            # visible turns beyond the answer record trail get no field
            # (honest absence, never a shifted guess).
            for j, t in enumerate(assistants):
                idx = len(order) - len(assistants) + j
                if 0 <= idx < len(order):
                    seg = buckets.get(order[idx]) or []
                    if seg:
                        t["tool_details"] = seg
        except Exception as e:  # noqa: BLE001 - a probe fold must not break rehydration
            warnings.append(f"#FALLBACK per-turn tool details unavailable (ledger read failed: {e})")
        out = {
            "run_id": run.run_id,
            "session_id": run.session_id,
            "visit_id": stamp.get("visit_id"),
            "status": str(getattr(run.status, "value", run.status)),
            "turn_n": int(visit_vars.get("turn_n") or 0),
            "participants": list(visit_vars.get("participants") or []),
            "turns": turns,
        }
        if warnings:
            out["warnings"] = warnings
        return out

    def ledger(self, name: str, run_id: str, *, after: int = 0, limit: int = 500) -> Dict[str, Any]:
        """The visit run's OWN ledger, paged (coder-tui c4307 ask 2: visit
        runs live in the home's `runtime_<slug>.sqlite3`, invisible to
        `/runs/*` — this is the observability read for that lane).

        PURE READ through `_load_visit` (the stamped-visit check is the auth
        boundary: the entity door serves ONLY stamped visit runs of THIS
        entity — never an arbitrary run id fished out of the home store).
        Cursor semantics match the main ledger stream: `after` = records
        already consumed; each record is served with its 1-based cursor.
        Privacy rides the formation-side contract (diary elections are
        captured at the LLM handler boundary — the ledger holds MARKED
        replies, no private words rest here; the 2026-07-16 wire-view rule).
        CONSUMER CONTRACT: ledger payloads are the WIRE VIEW (prompt chrome,
        MEMORIES decorations, prelude re-sent per cycle) — never a
        conversation source; rebuild conversations from /transcript. Works
        on live AND terminal runs. Meets-lane refusals still serve the plain
        shape (no code) — v1 scope is the /visit/* lane."""
        er, run, _wf, _manifest = self._load_visit(name, run_id, need_spec=False)
        records = er.ledger_store.list(run_id)
        if not isinstance(records, list):
            records = []
        # Clamp the cursor INTO the ledger (adversary F2: an over-shot
        # `after` must not echo garbage next_cursor forever) and give
        # limit=0 the minimum page, not the default (F3).
        start = min(max(0, int(after or 0)), len(records))
        lim = max(1, min(int(limit) if limit is not None else 500, 2000))
        # Served view (review 2026-11-08 note 4): a read_file result that
        # showed an image carries host-absolute paths in `output.media` (what
        # the adapter handed the provider); on the wire they are served
        # workspace-relative. The home store keeps the record as written.
        page = [_ledger_record_view(er.home.home_dir, rec) for rec in records[start : start + lim]]
        status = str(getattr(run.status, "value", run.status))
        return {
            "run_id": run.run_id,
            "status": status,
            "after": start,
            "records": [
                {"cursor": start + i + 1, "record": rec} for i, rec in enumerate(page)
            ],
            "next_cursor": start + len(page),
            "total": len(records),
            # done = the run is terminal AND this page reached the end —
            # a poller can stop; a live run never serves done.
            "done": status in ("completed", "failed", "cancelled") and start + len(page) >= len(records),
        }

    @staticmethod
    def _run_status_view(run: Any) -> Dict[str, Any]:
        visit_vars = (run.vars or {}).get("_visit") or {}
        stamp = ((run.vars or {}).get("_runtime") or {}).get("entity") or {}
        return {
            "open": True,
            "run_id": run.run_id,
            "session_id": run.session_id,
            "visit_id": stamp.get("visit_id"),
            "turn_n": int(visit_vars.get("turn_n") or 0),
            "status": str(getattr(run.status, "value", run.status)),
            # PROVENANCE: the arm this run was BORN under (pre-ruling runs
            # recorded "v0"). Every run SERVES the react graph now — the
            # field documents birth, it no longer selects.
            "workflow_arm": str(visit_vars.get("workflow_arm") or "v0"),
            # The phase bar's visit facts (contract-phases §1.3): who is in the visit, since when,
            # and the state word it interrupted (its close returns there).
            "participants": [str(p) for p in (visit_vars.get("participants") or stamp.get("participants") or [])],
            "created_at": getattr(run, "created_at", None),
            "prior_state": {"state": str((visit_vars.get("prior_state") or {}).get("state") or "") or None},
        }

    # -------------------------------------------------------------- internal
    def _build_spec(self, er: Any, *, participants: List[str], budget_profile: Optional[Dict[str, Any]],
                    visit_id: Optional[str], model_info: Optional[Dict[str, str]], conversation: bool = False) -> Any:
        from abstractruntime.identity.visit_workflow import build_visit_workflow

        return build_visit_workflow(
            er.home,
            participants=participants,
            budget_profile=budget_profile,
            idle_seconds=self._idle_timeout_s,
            model_info=model_info,
            visit_id=visit_id,
            react_middle=self._build_react_middle(er, model_info, conversation=conversation),
        )

    def _build_react_middle(self, er: Any, model_info: Optional[Dict[str, str]], *, conversation: bool = False) -> Any:
        """Agent's proven two-line construction (commons c347/c349, pinned by
        their test_react_visit_merge.py): the middle arrives as DATA — the
        door builds it from abstractagent's public API and hands it to
        runtime's merge-owning builder. React is UNCONDITIONAL for entity
        visits (laurent 00:49), so a missing adapter package refuses LOUDLY
        — never a silent single-call fallback.

        THE ENTITY'S HANDS ride the grant (G5(i), the Mnemosyne fabrication
        fix): the granted tools are declared NATIVELY in the LLM payload —
        native-channel substrates essentially never write fenced tool text
        (three benches: 0/9, 0/5, 0/11 fenced vs 5/5 native), so an empty
        declaration meant reason->final-answer with empty hands and prose
        fabrication. `<home>/tool_policy.yaml` (phase=visit) is the ONE
        authority; only tools the door can EXECUTE are declared (declaring
        a dead tool baits the model into calls that go nowhere)."""
        try:
            from abstractagent.adapters.react_runtime import create_react_workflow, reset_react_turn
            from abstractagent.logic.react import ReActLogic, ToolDefinition
        except ImportError as e:
            raise VisitRefused(
                503,
                "entity visits are react agents (maintainer ruling) but abstractagent "
                f"is not importable: {e} — install abstractagent",
            )
        from abstractruntime import resolve_tool_grant
        from abstractruntime.identity.visit_workflow import HARVEST_NODE, ReactMiddle

        grant = resolve_tool_grant(Path(er.home.home_dir), "visit")
        tool_defs = _entity_tool_definitions(grant.tools, ToolDefinition)
        if conversation:
            from abstractagent.logic.builtins import ASK_USER_TOOL
            tool_defs.append(ASK_USER_TOOL)
        # The entity's own mailbox tools (round 3 §3.1), offered while its "Agent email tools"
        # switch is active; the door's TOOL_CALLS handler runs them through its account.
        from .mail.runtime_wiring import entity_email_tool_specs

        email_specs = entity_email_tool_specs(Path(er.home.home_dir).name)
        tool_defs = tool_defs + [
            ToolDefinition(name=s["name"], description=s["description"], parameters=s["parameters"]) for s in email_specs
        ]
        info = dict(model_info or {})
        react = create_react_workflow(
            logic=ReActLogic(tools=tool_defs),
            workflow_id="entity-visit-react",
            provider=str(info.get("provider") or "") or None,
            model=str(info.get("model") or "") or None,
            # Pass the RAW grant, not the pre-filtered declarable list (agent
            # c800 / agency c801, works-or-loud): logic.tools is the declarable
            # set (only tools the door can EXECUTE are declared — dead
            # declarations bait no-op native calls), but the adapter must SEE
            # the full grant so it intersects to the declarable set AND writes
            # the durable `_runtime.allowlist_pruned` note naming what the door
            # could not offer. Pre-filtering here made a 9-name grant arrive
            # as 7 with zero trace — indistinguishable from door drift when
            # someone debugs a missing tool later (exactly how the c69 audit
            # caught the memory-tools gap; declarations now derive from
            # walled_tool_rows, so a pruned name means version skew, not a
            # hand-copy hole). Effective offer + execution allowlist are
            # UNCHANGED (the adapter intersects against logic.tools); only
            # the trace is added.
            allowed_tools=list(grant.tools) + [s["name"] for s in email_specs] + (["ask_user"] if conversation else []),
            final_next_node=HARVEST_NODE,
        )
        return ReactMiddle(nodes=react.nodes, entry="reason", reset_turn=reset_react_turn)

    def _spec_for(self, er: Any, run: Any) -> Any:
        """The run's workflow spec: cached, or rebuilt from the run's OWN
        verified stamp (the restart path — specs are deterministic in the
        stamp-time facts, and OPEN's setdefault semantics make re-seeding
        safe). EVERY rebuild serves the react graph — runtime's
        A1-with-companion ruling: the merged graph's node ids are a strict
        superset of v0's, so a legacy v0-born parked run resumes under it
        safely; refusing to rebuild would brick the home (an unloadable
        parked run 409s every new visit and cannot even be closed)."""
        with self._lock:
            wf = self._specs.get(run.run_id)
        if wf is not None:
            return wf
        stamp = ((run.vars or {}).get("_runtime") or {}).get("entity") or {}
        visit_vars = (run.vars or {}).get("_visit") or {}
        wf = self._build_spec(
            er,
            participants=[str(p) for p in (stamp.get("participants") or ["person:operator"])],
            budget_profile=stamp.get("budget_profile"),
            visit_id=stamp.get("visit_id"),
            model_info=dict(visit_vars.get("model_info") or {}),
            conversation=(((run.vars or {}).get("_runtime") or {}).get("conversation") or {}).get("enabled") is True,
        )
        with self._lock:
            self._specs[run.run_id] = wf
        return wf

    def _load_visit(self, name: str, run_id: str, *, need_spec: bool = True, teardown: bool = False):
        """Load + verify a stamped visit run. `need_spec=False` is the PURE
        READ path (ledger/transcript-class reads): it skips `_spec_for`, which
        builds the whole react workflow (imports abstractagent, resolves the
        tool grant) and caches it keyed by run_id — a poller sweeping TERMINAL
        runs through a read endpoint would otherwise 503 on a read when
        abstractagent is absent AND grow the spec cache unboundedly
        (ledger-adversary F1, 2026-07-22)."""
        from abstractruntime.identity.visit_workflow import VISIT_WORKFLOW_ID

        registry = self._registry
        manifest = registry.manifest_for(name)
        er = registry.get_entity_runtime(manifest.slug)
        if need_spec:
            from .steering import attach_steer_store
            attach_steer_store(er.runtime, er.home.home_dir)
        run = er.run_store.load(str(run_id or ""))
        if run is None or str(getattr(run, "workflow_id", "") or "") != VISIT_WORKFLOW_ID:
            raise VisitRefused(404, f"no visit run {run_id!r} on {manifest.entity_id}", code="visit_not_found")
        stamp = ((run.vars or {}).get("_runtime") or {}).get("entity") or {}
        if str(stamp.get("entity_id") or "") != manifest.entity_id:
            raise VisitRefused(409, f"run {run_id!r} is not a stamped visit of {manifest.entity_id}", code="not_a_visit")
        if need_spec and not teardown:
            # THE VISIT'S FENCE (STOP-LANE-DESIGN §2.2): a visit admitted before a Stop never acts on the
            # runtime admitted after it (the one serving the home now). A visit run from before the
            # stamp existed runs where it is (a live one cannot predate a Stop: Stop cancels it).
            admitted = ((run.vars or {}).get("_visit") or {}).get("generation")
            if admitted is not None and int(admitted) != int(er.home.fence.generation):
                raise VisitRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume")
        wf = self._spec_for(er, run) if need_spec else None
        return er, run, wf, manifest

    def is_in_flight(self, run_id: str) -> bool:
        """True while THIS process is actually executing the run (a drive or
        resume window). The stored run status is a durable claim; this is
        the liveness fact `working` renders from (adversary P1-2)."""
        with self._lock:
            return str(run_id) in self._in_flight

    def _drive(self, er: Any, wf: Any, run_id: str, *, max_steps: int) -> Any:
        """One ticking window under the visit-host lease (D1: acquire at
        resume, release at park; never broken mid-turn)."""
        lease = self._acquire_lease(er)
        with self._lock:
            self._in_flight.add(str(run_id))
        try:
            _admit_acting()
            return er.runtime.tick(workflow=wf, run_id=run_id, max_steps=max_steps)
        finally:
            with self._lock:
                self._in_flight.discard(str(run_id))
            if lease is not None:
                lease.release()

    def _resume(self, er: Any, wf: Any, run_id: str, *, payload: Dict[str, Any], max_steps: int,
                wait_key: str, manifest: Any) -> Any:
        lease = self._acquire_lease(er)
        with self._lock:
            self._in_flight.add(str(run_id))
        try:
            _admit_acting()
            return er.runtime.resume(
                workflow=wf, run_id=run_id, wait_key=wait_key, payload=payload, max_steps=max_steps
            )
        except ValueError as e:
            # Not waiting / paused / wait-key mismatch — client-visible truth.
            raise VisitRefused(409, f"the visit cannot take this message now: {e}", code="not_waiting")
        finally:
            with self._lock:
                self._in_flight.discard(str(run_id))
            if lease is not None:
                lease.release()

    def _acquire_lease(self, er: Any) -> Any:
        try:
            from abstractruntime.storage.lease import DirectoryLeaseHeld, acquire_directory_lease
        except ImportError:
            return None  # older runtime: no site acquires; labeled at open elsewhere
        # A visit never fails (operator 2026-11-09): another writer (a sleep's
        # consolidation pass) holds the home only until its step ends, and it
        # stops for the visiting posture — wait for it, no limit. The reaper
        # and the operator's pause/sleep teardown never wait
        # (lease_without_waiting): they get the old 409.
        logged = False
        while True:
            try:
                return acquire_directory_lease(Path(er.home.home_dir), holder="visit-host")
            except DirectoryLeaseHeld as e:
                if getattr(self._no_wait, "on", False):
                    raise VisitRefused(409, str(e), code="home_lease_held")
                if _stopped(Path(er.home.home_dir)):
                    raise VisitRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume")
                _admit_acting()  # the act this wait serves was stopped (a mirror's source entity): it ends
                if not logged:
                    logger.info("visit waits for the home's writer to finish: %s", e)
                    logged = True
                time.sleep(0.5)

    @contextlib.contextmanager
    def lease_without_waiting(self) -> Any:
        """Inside this block a held home answers 409 `home_lease_held` instead
        of waiting: the one reaper thread must never stall on one home, and the
        operator's own pause/sleep answers at once (labeled teardown_failed)."""
        previous = getattr(self._no_wait, "on", False)
        self._no_wait.on = True
        try:
            yield
        finally:
            self._no_wait.on = previous

    def _live_visit_run(self, er: Any, *, janitor: bool = False) -> Any:
        """The home's live visit run, if any. STAMPED runs only: a run
        without a finalized stamp is the start->stamp crash window's orphan
        (started, never stamped, never ticked) — it can neither be turned
        nor closed (`_load_visit` refuses it), so counting it would brick
        the visit door with an un-endable 409. With `janitor=True` (the
        open path, under the per-slug lock) such orphans are CANCELLED
        loudly; pure reads (`status`) just skip them."""
        from abstractruntime.core.models import RunStatus
        from abstractruntime.identity.visit_workflow import VISIT_WORKFLOW_ID

        for status in (RunStatus.WAITING, RunStatus.RUNNING):
            runs = er.run_store.list_runs(status=status, workflow_id=VISIT_WORKFLOW_ID, limit=10)
            for run in runs or []:
                stamp = ((run.vars or {}).get("_runtime") or {}).get("entity") or {}
                if not stamp.get("sig"):
                    if janitor:
                        try:
                            er.runtime.cancel_run(
                                run.run_id,
                                reason="stampless visit run (crash in the open window) — cancelled by the next open",
                            )
                        except Exception:
                            pass  # an uncancellable orphan stays skipped, never counted
                    continue
                return run
        return None

    def _finalize_terminal(self, er: Any, manifest: Any, run_id: str, state: Any) -> None:
        """Restore-on-terminal (pinned at 0014/083823Z; prior-state fix from
        the state-sources adversary): ANY path to a terminal visit converges
        here — restore the OPERATOR'S pre-visit state (recorded durably in
        the run's `_visit.prior_state`), mark session_closed. A visit ending
        must never convert an operator's asleep into a standing awake behind
        their back; a yielded loop still wakes (prior=awake in that branch).
        A failed restore is LABELED in the marker details (never fully
        silent — the next open's yielded-posture check is the recovery, but
        the stream should show the duty failed)."""
        from abstractruntime.identity.life import read_entity_state, write_entity_state

        registry = self._registry
        home_dir = registry.entities_dir / manifest.slug
        wake_warning: Optional[str] = None
        prior: Dict[str, Any] = {}
        sleep_continued = False
        from .entity_phase import GenerationMoved, fenced

        try:
            run0 = er.run_store.load(run_id)
            run_vars0 = (run0.vars or {}) if run0 is not None else {}
        except Exception:  # noqa: BLE001
            run_vars0 = {}
        # FENCED ON THE VISIT'S ADMISSION (STOP-LANE-DESIGN T10): the generation the visit was admitted
        # at (its stamp; a visit from before the stamp: the runtime's own, the one it ran on) — a Stop
        # since then means its restore writes nothing over the Stop's word.
        admitted = (run_vars0.get("_visit") or {}).get("generation")
        visit_fence = int(admitted) if admitted is not None else er.home.fence
        try:
            with fenced(registry.entities_dir, manifest.slug, visit_fence) as frec:
                st = read_entity_state(home_dir)
                word = str(st.get("state") or "")
                reason_now = str(st.get("reason") or "")
                # RESTORE BY OWNERSHIP (mutual-exclusivity wave, operator ruling,
                # audit finding 4): the open stamps the posture with THIS visit's
                # id, so terminal restores match the identity token — a posture
                # belonging to ANOTHER lane's live session is never adopted.
                # Backward compat: a posture WITHOUT any token (written by a
                # pre-wave build — in-flight visits across the upgrade boundary)
                # falls back to the old words-match; the b1 awake-wake check
                # covers pre-wave opens that wrote awake instead of the posture.
                prior: Dict[str, Any] = {}
                visit_id = ""
                try:
                    run = er.run_store.load(run_id)
                    run_vars = run.vars or {}
                    prior = dict((run_vars.get("_visit") or {}).get("prior_state") or {})
                    visit_id = str(((run_vars.get("_runtime") or {}).get("entity") or {}).get("visit_id") or "")
                except Exception:
                    prior = {}
                visiting_now = word == "asleep" and (
                    str(st.get("mode") or "") == "visiting" or "auto-yield" in reason_now
                )
                has_token = "[visit " in reason_now
                owned = visiting_now and (
                    (visit_id and f"[visit {visit_id}]" in reason_now)  # ownership match
                    or not has_token  # pre-wave posture: words-match compat
                )
                b1_wake = word == "awake" and reason_now.startswith(_woken_reason())
                if owned or b1_wake:
                    output = dict(getattr(state, "output", None) or {})
                    target = str(prior.get("state") or "awake")
                    if target not in ("awake", "asleep"):
                        target = "awake"  # paused is an operator/admin act, never auto-restored
                    if target == "asleep":
                        # The same sleep continues: its original reason, writer, wake time and since
                        # (the visit's facts are this close's `session_closed` marker).
                        from .entity_phase import continue_sleep

                        continue_sleep(registry.entities_dir, manifest.slug, prior, fence=frec.fence)
                        sleep_continued = True
                    elif owned:
                        write_entity_state(home_dir, "awake", reason=f"visitor session ended ({output.get('turns', 0)} turns; "
                                           f"{output.get('close_reason', 'closed')})", fence=frec.fence)
                    # b1_wake with prior=awake: already awake — no idle rewrite.
                elif visiting_now:
                    wake_warning = (
                        "posture belongs to another session (ownership token mismatch) — left standing; "
                        "state is the authority"
                    )
        except GenerationMoved:
            # The operator's Stop since the visit was admitted: its word stands, nothing is restored. The
            # prior state is still read from the run for the close's reconcile.
            prior = dict((run_vars0.get("_visit") or {}).get("prior_state") or {})
        except Exception as e:
            wake_warning = f"#FALLBACK wake-on-terminal failed ({e}); the next open treats the visiting posture as yielded"
        details: Dict[str, Any] = {
            "transport": "visit-run",
            "output": dict(getattr(state, "output", None) or {}),
        }
        if str(getattr(getattr(state, "status", None), "value", getattr(state, "status", "")) or "") == "cancelled":
            # A clean terminal record: what ended it and who (the kill switch: the operator).
            details["status"] = "cancelled"
            details["reason"] = str(getattr(state, "error", "") or "") or None
        if sleep_continued:
            details["sleep_continues"] = True  # the sleep this visit interrupted goes on, unchanged
        if wake_warning:
            details["warnings"] = [wake_warning]
        self._marker(
            registry, manifest.slug, manifest.entity_id, "session_closed",
            run_id=run_id, session_id=str(getattr(state, "session_id", "") or ""),
            journal_seq=self._journal_seq(er),
            details=details,
        )
        with self._lock:
            self._specs.pop(run_id, None)
        # A visit's close returns to the previous phase where the toggles allow it, and the
        # gateway restarts own time when personal is on (contract-phases v2 §4.4, §4.2).
        from .entity_phase import after_visit_closed

        after_visit_closed(registry, manifest.slug, prior or None, visit_host=self,
                           chat_host=_chat_host_or_none())
        if details.get("status") != "cancelled":
            # Titles and summaries a failed authoring call left pending are retried now on a
            # loop-less home (runtime 4eefddb; a hosted chat's close and the loop's ticks drain
            # their own). Beside the close, never in it.
            from .entity_authoring import schedule_drain_after_close

            schedule_drain_after_close(registry, manifest.slug)

    @staticmethod
    def _journal_seq(er: Any) -> Optional[int]:
        try:
            return int(er.home.ms.current_seq())
        except Exception:
            return None  # labeled at the marker, never silently zero

    def _marker(self, registry: Any, slug: str, entity_id: str, kind: str, *,
                run_id: str, session_id: str, journal_seq: Optional[int],
                details: Dict[str, Any]) -> None:
        try:
            from .entity_replay import record_host_marker

            if journal_seq is None:
                # The marker still lands (observability first) but says its
                # position on the replay axis is a floor, not a fact.
                details = dict(details)
                details["warnings"] = list(details.get("warnings") or []) + [
                    "#FALLBACK journal seq unreadable at marker time; positioned at 0"
                ]
            record_host_marker(
                entities_dir=registry.entities_dir, slug=slug, entity_id=entity_id,
                kind=kind, journal_seq=int(journal_seq or 0), run_id=run_id,
                session_id=session_id, details=details,
            )
        except Exception:
            pass  # markers are observability, never a visit blocker
