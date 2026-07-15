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

import secrets
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import entity_iterations_ceiling

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


class VisitRefused(Exception):
    """A refusal with an HTTP status shape (mirrors ChatOpenRefused)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = int(status)
        self.detail = detail


def _compose_turn_probe(run_vars: Dict[str, Any]) -> Dict[str, Any]:
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
    - tool_details / files: NOT captured in the durable lane's vars yet —
      served as empty lists (honest absence, never fabricated); the adapter's
      turn_captures extension is the named follow-up, not a silent gap.
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

    return {
        "turn_id": str(turn_ns.get("turn_id") or ""),
        "tools_ran": [str(t) for t in (turn_ns.get("tools_ran") or []) if str(t or "").strip()],
        "memories": memories,
        "memories_in_context": len(memories),
        "diary_entries": list(turn_ns.get("diary_meta") or []),
        "notices": list(turn_ns.get("notices") or []),
        "participants": list(visit_vars.get("participants") or []),
        "system_prompt": str(visit_vars.get("system_base") or ""),
        "tool_details": [],
        "files": [],
    }


# Native declarations for the ENTITY toolset — argument shapes match the
# runtime executors these calls land on (`identity.tools`: body-first
# convention, `path` as the one named arg for write_file), NOT abstractcore's
# common_tools shapes (whose file_path/directory_path names the entity
# executors do not read). Declarations belong beside their executors long
# term — runtime's inventory-expansion item absorbs these; the door keeps
# this copy deliberately minimal until then. read_memory/search_memory are
# ABSENT on purpose: their driver resolvers are ChatSession methods the door
# cannot reach yet — declaring a tool that cannot execute baits dead calls.
_ENTITY_TOOL_DECLARATIONS: Dict[str, Dict[str, Any]] = {
    "web_search": {
        "description": "Search the live web. Returns titles, URLs and snippets for the query.",
        "parameters": {"query": {"type": "string", "description": "What to search for."}},
    },
    "fetch_url": {
        "description": "Fetch one web page and return its readable text content.",
        "parameters": {"url": {"type": "string", "description": "The full http(s) URL to fetch."}},
    },
    "diary_list": {
        "description": "List your recent diary entries (ids, kinds, gists) from your own book.",
        "parameters": {},
        "act_only": True,  # gists (private included) re-run fresh at send time; only the act-frame rests (e-s 233 R3)
    },
    "diary_read": {
        "description": "Read one of your own diary entries verbatim by its entry id.",
        "parameters": {"entry_id": {"type": "string", "description": "The diary entry id (from diary_list)."}},
        "act_only": True,  # words resolve fresh from the book; only the act-frame rests
    },
    "write_file": {
        "description": "Write a file in your workspace (creates or overwrites).",
        "parameters": {
            "path": {"type": "string", "description": "Relative path inside your workspace."},
            "content": {"type": "string", "description": "The full file content to write."},
        },
    },
    "read_file": {
        "description": "Read a file from your workspace.",
        "parameters": {"path": {"type": "string", "description": "Relative path inside your workspace."}},
    },
    "list_files": {
        "description": "List files in your workspace (optionally under a subdirectory).",
        "parameters": {"path": {"type": "string", "description": "Relative directory, default '.'"}},
    },
}


def _entity_tool_definitions(granted: Any, tool_definition_cls: Any) -> List[Any]:
    """ToolDefinitions for the GRANTED names the door can execute — grant
    order preserved, undeclarable names skipped (they refuse honestly at the
    executor if called by other means; they are simply not offered)."""
    out: List[Any] = []
    for name in granted or ():
        decl = _ENTITY_TOOL_DECLARATIONS.get(str(name))
        if decl is None:
            continue
        kwargs: Dict[str, Any] = {
            "name": str(name),
            "description": decl["description"],
            "parameters": dict(decl.get("parameters") or {}),
        }
        if decl.get("act_only"):
            kwargs["act_only"] = True
        out.append(tool_definition_cls(**kwargs))
    return out


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
    ) -> None:
        self._registry = registry
        # `chat_probe(slug) -> bool` = "does the HOSTED chat lane have a live
        # session on this home?" (service-wired). The stale-yield repair must
        # never wake a home whose visiting posture belongs to a LIVE drawer
        # session; absent probe = assume none (standalone/test hosts).
        self._chat_probe = chat_probe
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

        (A) DUE PARKED VISITS: drive every parked visit whose idle deadline
        has PASSED (the per-home run store's indexed due query — never a
        full-store parse). Deliberately narrow: only DUE deadline waits are
        touched. A RUNNING-at-rest run (host crash mid-turn) stays for the
        explicit /tick recovery verb — the reaper never resumes half-driven
        cognition on its own clock; the one LLM call it can trigger is the
        idle close's ruled reflection pass.

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
        from datetime import datetime, timezone

        from abstractruntime.identity.life import read_entity_state, write_entity_state

        state = read_entity_state(home_dir)
        if str(state.get("state") or "") != "asleep" or str(state.get("mode") or "") != "visiting":
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
        write_entity_state(
            home_dir,
            "awake",
            reason="stale visit yield repaired — no live session held this home (gateway reaper)",
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
    ) -> Dict[str, Any]:
        """`participants` here is TRUSTED input: the route derives it from
        the authenticated principal (payload claims are refused at the
        boundary — the situation contract: WHO is stamped by the door,
        never claimed)."""
        pre, leg = self.open_leg(
            name,
            participants=list(participants or ["person:operator"]),
            visit_id=f"visit-{secrets.token_hex(6)}",
            session_id=session_id,
            channel_label="operator-visit",
        )
        return {
            "run_id": leg["run_id"],
            "visit_id": leg["visit_id"],
            "entity_id": pre["manifest"].entity_id,
            "session_id": leg["session_id"],
            "participants": leg["participants"],
            "yielded_loop": pre["yielded"],
            "prelude_warnings": leg["prelude_warnings"],
        }

    def open_leg(
        self,
        name: str,
        *,
        participants: List[str],
        visit_id: str,
        session_id: Optional[str],
        channel_label: str,
    ) -> Any:
        """Preflight + start ONE visit leg atomically against concurrent
        opens on the same home (the check->create window closes under a
        per-slug mutex). Shared by the operator visit and each side of a
        two-entity meet — one code path, one guard."""
        slug = self._registry.manifest_for(name).slug
        with self._slug_lock(slug):
            pre = self._preflight(name, participants)
            leg = self._start_leg(
                pre,
                participants=list(participants),
                visit_id=visit_id,
                session_id=session_id,
                channel_label=channel_label,
            )
        return pre, leg

    def _slug_lock(self, slug: str) -> threading.Lock:
        with self._lock:
            lock = self._open_locks.get(slug)
            if lock is None:
                lock = threading.Lock()
                self._open_locks[slug] = lock
            return lock

    def _refuse_if_paused(self, manifest: Any, *, verb: str) -> None:
        """Non-awake states gate an ALREADY-OPEN visit too: if the teardown
        failed (lease held, close raced) or a fresh open slipped into the
        sleep-write window, the badge alone must still stop cognition —
        open() checks state, and so do turn/tick. Close is deliberately NOT
        gated (the teardown IS a close).

        ASLEEP gates too (state-sources adversary P0-2): an OPERATOR sleep
        that raced the teardown used to leave a durable visit accepting
        billed turns under /state=asleep — the route's own promise ("asleep/
        paused must actually STOP an open visit, not just flip a badge") was
        pause-only in code. The visiting-yield posture (mode=visiting) is
        the visit's OWN sleep and stays open."""
        from abstractruntime.identity.life import read_entity_state

        state = read_entity_state(self._registry.entities_dir / manifest.slug)
        word = str(state.get("state") or "")
        if word == "paused":
            reason = str(state.get("reason") or "") or "no reason recorded"
            raise VisitRefused(
                409,
                f"{manifest.entity_id} is paused (hard freeze): {reason} — "
                f"this visit cannot {verb}; close it (closed_by=pause) or wake him first",
            )
        if word == "asleep" and str(state.get("mode") or "") != "visiting":
            reason = str(state.get("reason") or "") or "no reason recorded"
            raise VisitRefused(
                409,
                f"{manifest.entity_id} is asleep by the operator: {reason} — "
                f"this visit cannot {verb}; close it or wake him first",
            )

    # ------------------------------------------------- shared leg machinery
    def _preflight(self, name: str, participants: Optional[List[str]]) -> Dict[str, Any]:
        """The per-home gates every visit leg passes BEFORE anything durable:
        substrate resolve (no-fallback), one-life-one-visit (durable),
        paused refusal, and the auto-yield negotiation with the own-time
        loop. Returns the resolved handles; raises VisitRefused on any gate."""
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

        try:
            provider, model = resolve_substrate(None, None, home_dir=home_dir)
        except ChatOpenRefused as e:
            raise VisitRefused(e.status, e.detail)

        er = registry.get_entity_runtime(slug)

        live = self._live_visit_run(er, janitor=True)
        if live is not None:
            raise VisitRefused(
                409,
                f"a visit is already open on {manifest.entity_id} (run {live.run_id!r}) — "
                "one life, one summon; continue it with /turn or end it with /close",
            )

        state = read_entity_state(home_dir)
        mode = str(state.get("mode") or "")
        reason = str(state.get("reason") or "")
        if state.get("state") == "paused":
            raise VisitRefused(409, f"{manifest.entity_id} is paused (hard freeze): {reason or 'no reason recorded'}")

        yielded = False
        woke_for_visit = False
        # The operator's PRIOR intent survives the visit (state-sources
        # adversary, P0-1 residue): a visit may overwrite the state (yield /
        # B1 wake), but close restores what the OPERATOR had set — a visit
        # ending must never convert an operator's asleep into a standing
        # awake behind their back. Recorded here, threaded into run vars by
        # the leg, restored by _finalize_terminal.
        prior_state = {"state": str(state.get("state") or "awake"), "reason": reason}
        visitor = (participants or ["person:operator"])[0]
        if bool(read_loop_status(home_dir).get("running")):
            write_entity_state(
                home_dir, "asleep",
                reason=f"in conversation with {visitor} (auto-yield)", mode="visiting",
            )
            if not await_loop_quiescent(home_dir, timeout_seconds=55.0):
                write_entity_state(home_dir, "awake", reason="visit open aborted (loop did not yield in time)")
                raise VisitRefused(409, "the entity's own-time loop has not reached a tick boundary yet — retry shortly")
            yielded = True
        elif state.get("state") == "asleep":
            if mode == "visiting" or "auto-yield" in reason:
                yielded = True
                # A visiting-yield posture belongs to a PREVIOUS visit; the
                # operator's own word is not recoverable from it — awake is
                # the honest restore target.
                prior_state = {"state": "awake", "reason": ""}
            else:
                # B1 ruling (a), laurent 04:58: "if i click visit, it should
                # awake the entity, period." An operator-asleep entity (not a
                # visit-yield posture) is WOKEN by the visit itself rather than
                # refused — the operator always has a path in. The wake reason
                # records that the visit did it, and the loop (if any) is not
                # running here (running-loop is the branch above), so no lease
                # collision on this path; a mid-day loop lease is the separate
                # runtime per-tick-release keystone (gateway c1315).
                write_entity_state(
                    home_dir, "awake",
                    reason=f"woken by visit from {visitor}",
                )
                state = read_entity_state(home_dir)
                woke_for_visit = True

        return {
            "manifest": manifest, "slug": slug, "home_dir": home_dir, "er": er,
            "provider": provider, "model": model, "yielded": yielded,
            "woke_for_visit": woke_for_visit,
            "prior_state": prior_state,
        }

    def _start_leg(
        self,
        pre: Dict[str, Any],
        *,
        participants: List[str],
        visit_id: str,
        session_id: Optional[str],
        channel_label: str,
    ) -> Dict[str, Any]:
        """Mint -> start -> stamp -> seed -> drive-to-park for ONE visit leg
        (shared by the operator visit and each side of a two-entity meet).
        The stamp lands BEFORE the first tick (this driver is the only
        ticker of the store — request-driven — so nothing executes in the
        mint->finalize window)."""
        from abstractruntime.identity.life import write_entity_state

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
        yielded = pre["yielded"]

        session = str(session_id or "").strip() or f"visit-{slug}-{secrets.token_hex(4)}"
        stamp_participants = [str(p) for p in participants]
        if manifest.entity_id not in stamp_participants:
            stamp_participants.append(manifest.entity_id)  # explicit co-presence (a2a 0007)
        budget_profile = summon_budget_profile(None)

        provisional = mint_summon_stamp(
            data_dir=registry.data_dir,
            entity_id=manifest.entity_id,
            channel=CHANNEL_WORKPLACE,
            session_id=session,
            participants=stamp_participants,
            budget_profile=budget_profile,
            visit_id=visit_id,
            # A visit is a WORKPLACE session, but its close-reflection segment
            # (REFLECT/APPLY in build_visit_workflow) is the entity's OWN
            # reflection — the door signs those node ids so the gate may
            # narrow-widen workplace->entity-reflection for the reflection
            # acts alone (config-object close-reflection ruling, option (a)).
            phase="visit",
            reflection_nodes=list(_VISIT_REFLECTION_NODES),
        )

        try:
            # INSIDE the restore window (adversary find): the spec build can
            # now raise (react is unconditional — a missing abstractagent
            # refuses 503 here). A failure before this try used to strand a
            # yielded own-time loop asleep in visiting posture with nothing
            # ever waking it.
            wf = self._build_spec(
                er,
                participants=stamp_participants,
                budget_profile=budget_profile,
                visit_id=visit_id,
                model_info={"provider": str(pre["provider"]), "model": str(pre["model"])},
            )
            run_id = er.runtime.start(
                workflow=wf,
                vars=_seed_run_vars(er),
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
            rt_ns["prompt_cache_binding"] = f"{manifest.entity_id}|{session}"
            visit_ns = run.vars.get("_visit")
            if not isinstance(visit_ns, dict):
                visit_ns = {}
                run.vars["_visit"] = visit_ns
            visit_ns["idle_seconds"] = float(self._idle_timeout_s)
            # PROVENANCE, not selection: which arm the run was born under.
            # Every rebuild serves the react graph (A1-with-companion).
            visit_ns["workflow_arm"] = ARM_REACT
            # The operator's word before this visit touched the state —
            # durable in the run so a fresh host restores it at terminal.
            visit_ns["prior_state"] = dict(pre.get("prior_state") or {"state": "awake", "reason": ""})
            er.run_store.save(run)

            state_after = self._drive(er, wf, run_id, max_steps=_OPEN_MAX_TICKS)
        except Exception:
            if yielded:
                write_entity_state(home_dir, "awake", reason="visit aborted (open failed)")
            elif pre.get("woke_for_visit"):
                # The B1 wake must not outlive a FAILED open (adversary
                # P2-3): a refused prelude / missing adapter / lease error
                # used to leave the entity awake with the operator's sleep
                # silently erased — and no run existed to restore it later.
                prior = dict(pre.get("prior_state") or {})
                write_entity_state(
                    home_dir, "asleep",
                    reason=(str(prior.get("reason") or "") or "operator sleep restored") + " (visit open failed)",
                )
            raise

        output = dict(getattr(state_after, "output", None) or {})
        if str(getattr(state_after.status, "value", state_after.status)) == "completed" and output.get("refused"):
            if yielded:
                write_entity_state(home_dir, "awake", reason="visit aborted (prelude refused)")
            raise VisitRefused(409, "summon refused: " + "; ".join(str(r) for r in output.get("reasons", [])))

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
        }

    # ------------------------------------------------------------------ turn
    def turn(self, name: str, run_id: str, *, text: str, speaker: Optional[str] = None) -> Dict[str, Any]:
        from abstractruntime.identity.visit_workflow import VISITOR_WAIT_KEY

        er, run, wf, manifest = self._load_visit(name, run_id)
        self._refuse_if_paused(manifest, verb="take a message")

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
        if speaker:
            payload["speaker"] = str(speaker)

        state = self._resume(er, wf, run_id, payload=payload, max_steps=_TURN_MAX_TICKS,
                             wait_key=VISITOR_WAIT_KEY, manifest=manifest)

        status = str(getattr(state.status, "value", state.status))
        visit_vars = (state.vars or {}).get("_visit") or {}
        history = list(visit_vars.get("history") or [])
        reply = ""
        for msg in reversed(history):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                reply = str(msg.get("content") or "")
                break
        out: Dict[str, Any] = {
            "run_id": run_id,
            "reply": reply,
            "turn_n": int(visit_vars.get("turn_n") or 0),
            "status": status,
        }
        # PROBE PAYLOAD PARITY (cutover gap 1, entity c1318/gateway c1320):
        # the hosted chat lane serves driver-authored transparency on every
        # turn (tools_ran, memories with born_at/origin, system_prompt); the
        # durable lane must not regress it or the drawer flip loses the
        # operator's probe surfaces AND the tool-claim fabrication guard
        # (which reads tools_ran as data, never reply prose). Composed from
        # the run's own durable vars — the workflow's HARVEST/RENDER nodes
        # already fold these; the door only surfaces them.
        out.update(_compose_turn_probe(state.vars or {}))
        if status == "completed":  # timed-out close raced this turn to terminal
            out["output"] = dict(state.output or {})
            self._finalize_terminal(er, manifest, run_id, state)
        elif status == "failed":
            out["error"] = str(getattr(state, "error", "") or "the turn failed; see the run ledger")
        return out

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
            raise VisitRefused(400, f"unknown closed_by {closed_by!r} (operator | sleep | pause)")

        er, run, wf, manifest = self._load_visit(name, run_id)
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
        if status in ("completed", "failed", "cancelled"):
            self._finalize_terminal(er, manifest, run_id, state)
        return out

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
        er, run, _wf, _manifest = self._load_visit(name, run_id)
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
        readable, same as the hosted lane's)."""
        er, run, _wf, _manifest = self._load_visit(name, run_id)
        visit_vars = (run.vars or {}).get("_visit") or {}
        stamp = ((run.vars or {}).get("_runtime") or {}).get("entity") or {}
        turns = [
            {"role": str(m.get("role") or ""), "content": str(m.get("content") or "")}
            for m in list(visit_vars.get("history") or [])
            if isinstance(m, dict)
        ]
        return {
            "run_id": run.run_id,
            "session_id": run.session_id,
            "visit_id": stamp.get("visit_id"),
            "status": str(getattr(run.status, "value", run.status)),
            "turn_n": int(visit_vars.get("turn_n") or 0),
            "participants": list(visit_vars.get("participants") or []),
            "turns": turns,
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
        }

    # -------------------------------------------------------------- internal
    def _build_spec(self, er: Any, *, participants: List[str], budget_profile: Optional[Dict[str, Any]],
                    visit_id: Optional[str], model_info: Optional[Dict[str, str]]) -> Any:
        from abstractruntime.identity.visit_workflow import build_visit_workflow

        return build_visit_workflow(
            er.home,
            participants=participants,
            budget_profile=budget_profile,
            idle_seconds=self._idle_timeout_s,
            model_info=model_info,
            visit_id=visit_id,
            react_middle=self._build_react_middle(er, model_info),
        )

    def _build_react_middle(self, er: Any, model_info: Optional[Dict[str, str]]) -> Any:
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
            # could not offer (e.g. read_memory/search_memory, absent from
            # _ENTITY_TOOL_DECLARATIONS today). Pre-filtering here made a
            # 9-name grant arrive as 7 with zero trace — indistinguishable from
            # door drift when someone debugs a missing tool later. Effective
            # offer + execution allowlist are UNCHANGED (the adapter intersects
            # against logic.tools); only the trace is added.
            allowed_tools=list(grant.tools),
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
        )
        with self._lock:
            self._specs[run.run_id] = wf
        return wf

    def _load_visit(self, name: str, run_id: str):
        from abstractruntime.identity.visit_workflow import VISIT_WORKFLOW_ID

        registry = self._registry
        manifest = registry.manifest_for(name)
        er = registry.get_entity_runtime(manifest.slug)
        run = er.run_store.load(str(run_id or ""))
        if run is None or str(getattr(run, "workflow_id", "") or "") != VISIT_WORKFLOW_ID:
            raise VisitRefused(404, f"no visit run {run_id!r} on {manifest.entity_id}")
        stamp = ((run.vars or {}).get("_runtime") or {}).get("entity") or {}
        if str(stamp.get("entity_id") or "") != manifest.entity_id:
            raise VisitRefused(409, f"run {run_id!r} is not a stamped visit of {manifest.entity_id}")
        wf = self._spec_for(er, run)
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
            return er.runtime.resume(
                workflow=wf, run_id=run_id, wait_key=wait_key, payload=payload, max_steps=max_steps
            )
        except ValueError as e:
            # Not waiting / paused / wait-key mismatch — client-visible truth.
            raise VisitRefused(409, f"the visit cannot take this message now: {e}")
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
        try:
            return acquire_directory_lease(Path(er.home.home_dir), holder="visit-host")
        except DirectoryLeaseHeld as e:
            raise VisitRefused(409, str(e))

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
        try:
            st = read_entity_state(home_dir)
            word = str(st.get("state") or "")
            reason_now = str(st.get("reason") or "")
            # Only VISIT-AUTHORED states are restored — an operator/admin act
            # landed mid-visit (a pause, a fresh sleep) stands untouched.
            yield_posture = word == "asleep" and (
                str(st.get("mode") or "") == "visiting" or "auto-yield" in reason_now
            )
            b1_wake = word == "awake" and reason_now.startswith("woken by visit")
            if yield_posture or b1_wake:
                output = dict(getattr(state, "output", None) or {})
                prior: Dict[str, Any] = {}
                try:
                    run = er.run_store.load(run_id)
                    prior = dict(((run.vars or {}).get("_visit") or {}).get("prior_state") or {})
                except Exception:
                    prior = {}
                target = str(prior.get("state") or "awake")
                if target not in ("awake", "asleep"):
                    target = "awake"  # paused is an operator/admin act, never auto-restored
                suffix = f"(visitor session ended: {output.get('turns', 0)} turns; {output.get('close_reason', 'closed')})"
                if target == "asleep":
                    write_entity_state(
                        home_dir, "asleep",
                        reason=(str(prior.get("reason") or "") or "operator sleep restored") + f" {suffix}",
                    )
                elif yield_posture:
                    write_entity_state(home_dir, "awake", reason=f"visitor session ended ({output.get('turns', 0)} turns; "
                                       f"{output.get('close_reason', 'closed')})")
                # b1_wake with prior=awake: already awake — no idle rewrite.
        except Exception as e:
            wake_warning = f"#FALLBACK wake-on-terminal failed ({e}); the next open treats the visiting posture as yielded"
        details: Dict[str, Any] = {
            "transport": "visit-run",
            "output": dict(getattr(state, "output", None) or {}),
        }
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
