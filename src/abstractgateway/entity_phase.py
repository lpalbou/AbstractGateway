"""Entity phases: one derivation, the kill switch, the toggles and the gateway's own-time reconcile.

Operator ruling 2026-11-08 (root untracked/entity-phases-2026-11-08/TARGET.md, CONTRACT-phases.md
v2). An entity is in exactly one of four activity phases, `visit`, `work`, `personal`, `sleep`, or
outside the machine: `idle` (awake, reachable, nothing of its own running) or `stopped` (the kill
switch, the state word `paused` on the wire).

- `work` and `personal` are TOGGLES (permissions, pressed = allowed). `personal` is the runtime's
  personal grant (`phases.yaml`); `work` lives in this module's sidecar until the runtime owns a
  `work` bucket (contract §6.4).
- `derive` folds the state file, the loop status, the grant, the open visit lanes and the summon
  seat into ONE snapshot (`/life_state`, `/cognition`, the tile and `POST /state` all read it).
- `publish` versions the snapshot (`rev`, monotonic per home, stored in the sidecar) and appends
  one `phase_changed` host marker per new rev (the Entity app's live surface is the replay stream).
- `stop` / `resume` are the kill switch. Stop bumps the home's `generation` FIRST (the fence), so
  a writer that started before the stop (a maintenance pass, a reconcile spawn, a visit restore, a
  mention persist) re-checks it and gives up instead of undoing the stop.
- `reconcile` RESTORES the phase an interruption took over (`resume_to`), and only while that
  phase's grant is still on (operator 2026-11-09: "own time can only be given and removed by the
  operator ... if granted, when there are transitions to other states, it can go back to that
  state"). It never starts own time from idle by itself; only the operator's own act does (his
  toggle turning personal on, his loop/start, his retry). A crashed loop is never restarted
  automatically: the read shows `blocked {loop_crashed}` with a `retry` action.

The sidecar `<entities_dir>/.life_phase/<slug>.json` is gateway bookkeeping, outside the home (the
`.host_stream` / `.live_summons` class). It is written atomically under a per-home flock, and the
flock is held only for read-check-write, never across a model call, a spawn or a teardown.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

SIDECAR_DIRNAME = ".life_phase"
PHASES = ("visit", "work", "personal", "sleep", "stopped", "idle")
ACTIVITY_PHASES = ("visit", "work", "personal", "sleep")
# Sleeps the entity chose itself (its loop's own writers).
SELF_SLEEP_WRITERS = ("self", "personal-cycle", "sleep-bound")
# The idle sweep's sleep (contract v3 D2): his own sleep, entered by the gateway from idle.
IDLE_SWEEP_WRITER = "idle-sweep"
# Sleeps the old loop gate wrote when it found nothing to do (root 1178): until the runtime batch
# they are not sleep at all — the entity is awake and reachable (contract §1.2).
GATE_SLEEP_WRITERS = ("grant-gate", "day-gate", "need-check")
MAINTENANCE_ENDS = "when maintenance is done"
# The one wording of a kill-switch ending on every record it leaves (runs, visits, chats, passes).
KILL_SWITCH_REASON = "kill_switch: stopped by the operator"
# The Stop receipt's own-time row (v3 D3): the kill switch takes his own time back.
OWN_TIME_REVOKED_SENTENCE = "Own time was revoked."
# Homes whose own-time spawn reconcile decided and has not yet handed to the runtime (this process
# only, cleared in a finally: no clock). From the runtime's reservation on, loop_status phase
# `starting` is the one truth (runtime 86666af; STOP-LANE-DESIGN §2.4).
_STARTING: set = set()
_STARTING_GUARD = threading.Lock()


def own_time_starting(entities_dir: Path, slug: str, loop: Optional[Dict[str, Any]] = None) -> bool:
    """A loop is being started for this home: the runtime's spawn reservation (loop_status phase
    `starting`, running while its spawner or child lives) or a reconcile of this process between
    its decision and that reservation."""
    with _STARTING_GUARD:
        if str(Path(entities_dir) / slug) in _STARTING:
            return True
    if loop is None:
        from .entity_loop import loop_status

        try:
            loop = loop_status(Path(entities_dir) / slug)
        except Exception:  # noqa: BLE001 - unreadable status: a start may be under way (fail closed)
            return True
    return str(loop.get("phase") or "") == "starting" and bool(loop.get("running"))
# loop_status.stopped_by values that mean "the loop ended because it was asked to".
REQUESTED_STOPS = ("stop_command", "stop_file", "operator", "personal_disarmed", "personal_off", "kill_switch", "settled_desk",
                   # the runtime batch's own words (2026-11-09): work turned off, a removed edge, and a
                   # loop that found no own time to return to (it exits idle) — none of them a crash.
                   "work_off", "edge_removed", "no_return",
                   # v3 (runtime ed32f0e): all jobs done and no own time to return to — a clean idle.
                   "jobs_done")

_DEFAULT_RECORD: Dict[str, Any] = {
    "rev": 0,
    "published_rev": 0,
    "generation": 0,
    "snapshot": None,
    "since": None,
    "last_activity_phase": None,
    "own_time_params": None,
    "start_error": None,
    "stop_outstanding": [],
    "maintenance": None,
    "sleep_since_override": None,
    # The phase to return to after an interruption (operator 2026-11-09): "personal" or "work" while
    # that own-time phase was running when a visit, a sleep, a request or a Stop interrupted it;
    # None otherwise. ONE writer: `_note_resume_to` (publish's observation, the operator's toggles,
    # a deferred start). Reconcile only ever RESTORES it, and only while its grant is still on.
    "resume_to": None,
    # A gateway restart is not a Stop (operator decision D5, 2026-11-09): the start pass leaves
    # `{generation, at}` here for a home whose grant is armed and whose loop is down, so the restore
    # it asks for (now, or when the interruption standing at the start ends) is not refused as a
    # crash: the restart is what took the loop down. Only valid under the generation it names (a
    # Stop moves it); cleared once a start is attempted or nothing is left to restore.
    "restart_resume": None,
}


class GenerationMoved(Exception):
    """A writer's captured generation is stale: a Stop landed after it began (or the generation
    cannot be read: fail closed, nothing is admitted on an unknown generation)."""


class FencedRecord(dict):
    """The sidecar record as `fenced()` / `fenced_here()` read it under the lifecycle lock, carrying
    the fence its writes pass (STOP-LANE-DESIGN §2.2): `rec.fence` is the runtime LifecycleFence of
    the admission (or KILL_SWITCH / RESUME for the Stop's and Resume's own writes), handed to every
    runtime state writer inside the block. `write_record` accepts only this, and only while its
    block is open: an unfenced sidecar write is a TypeError, never a silent write over a Stop."""

    __slots__ = ("fence", "slug", "open")

    def __init__(self, data: Dict[str, Any], *, fence: Any, slug: str) -> None:
        super().__init__(data)
        self.fence = fence
        self.slug = slug
        self.open = True


# --------------------------------------------------------------------------- sidecar + locks
def sidecar_path(entities_dir: Path, slug: str) -> Path:
    return Path(entities_dir) / SIDECAR_DIRNAME / f"{slug}.json"


_LOCKS: Dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_HELD = threading.local()


def _rlock(key: str) -> threading.RLock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


@contextlib.contextmanager
def phase_lock(entities_dir: Path, slug: str) -> Iterator[None]:
    """The per-home lifecycle lock: a thread RLock plus a cross-process flock on a sidecar lockfile.
    Re-entrant in one thread (the flock is taken once, by the outermost holder)."""
    path = sidecar_path(entities_dir, slug)
    key = str(path)
    lock = _rlock(key)
    with lock:
        held: Dict[str, Any] = getattr(_HELD, "files", None) or {}
        _HELD.files = held
        if key in held:
            held[key][0] += 1
            try:
                yield
            finally:
                held[key][0] -= 1
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path.with_suffix(".lock"), "a+")
        try:
            try:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass  # non-POSIX: the in-process lock still serializes this gateway
            held[key] = [1, fh]
            try:
                # ONE cross-process lock with the runtime (Codex review 2 #1; runtime f881cac): the
                # home's state lock is taken INSIDE this one (the runtime never takes ours), so every
                # gateway check-then-write is serialized with the loop's own state and phases.yaml
                # writes, which take it themselves. No limit: a Stop never fails.
                with _home_state_lock(Path(entities_dir) / slug):
                    yield
            finally:
                held.pop(key, None)
        finally:
            fh.close()


def read_record(entities_dir: Path, slug: str) -> Dict[str, Any]:
    path = sidecar_path(entities_dir, slug)
    rec = json.loads(json.dumps(_DEFAULT_RECORD))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            rec.update(raw)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        logger.warning("entity phase sidecar %s unreadable (%s); starting from defaults", path, e)
    # `resume_to` is the RUNTIME's bucket (contract v3; runtime ed32f0e/901d9c5 write it too: a job
    # interrupting own time, an idle exit, the kill switch). Read through its reader — before the
    # bucket exists it answers the sidecar's value; afterwards the bucket wins.
    rec["resume_to"] = _bucket_resume_to(Path(entities_dir) / slug, rec.get("resume_to"))
    # The lifecycle generation is the runtime's too (Codex review 2 #1; runtime f881cac): ONE number
    # both sides fence on, bumped by every Stop (the gateway's and hard_stop_loop's).
    home_dir = Path(entities_dir) / slug
    if home_dir.is_dir():
        from abstractruntime.identity.life import read_lifecycle_generation

        try:
            rec["generation"] = int(read_lifecycle_generation(home_dir))
        except ValueError as e:
            # FAIL CLOSED (STOP-LANE-DESIGN §6.3): a corrupt phases.yaml is an UNKNOWN generation,
            # never the sidecar's old number — `fenced()` refuses every write admitted on it.
            logger.warning("lifecycle generation of %s unreadable (%s): nothing is admitted", slug, e)
            rec["generation"] = None
            rec["generation_unreadable"] = str(e)
    return rec


def _bucket_resume_to(home_dir: Path, fallback: Optional[str]) -> Optional[str]:
    if not home_dir.is_dir():
        return fallback if fallback in ("personal", "work") else None
    from abstractruntime.identity.life import read_resume_to

    phase = read_resume_to(home_dir).get("phase")
    return phase if phase in ("personal", "work") else None


def write_record(entities_dir: Path, slug: str, rec: Dict[str, Any]) -> None:
    """Write the sidecar record. ONLY the record a `fenced()` / `fenced_here()` block yielded, while
    that block is open (the lifecycle lock held, the generation checked there): anything else is a
    TypeError (STOP-LANE-DESIGN §2.2: the unfenced write does not run)."""
    if not isinstance(rec, FencedRecord) or not rec.open or rec.slug != slug:
        raise TypeError("write_record: only the record a fenced()/fenced_here() block yielded, inside that block "
                        "(the sidecar is written under the lifecycle lock, fenced on the admission's generation)")
    if rec.pop("_resume_to_written", False):
        # The gateway's own word goes to the runtime's bucket, its one writer (the sidecar keeps a
        # projection only).
        home_dir = Path(entities_dir) / slug
        if home_dir.is_dir():
            from abstractruntime.identity.life import write_resume_to

            write_resume_to(home_dir, rec.get("resume_to"), written_by="gateway", fence=rec.fence)
    path = sidecar_path(entities_dir, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp-{secrets.token_hex(4)}")
    data = {k: v for k, v in rec.items() if k != "generation_unreadable"}
    tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _note_resume_to(rec: Dict[str, Any], value: Optional[str]) -> bool:
    """THE gateway's one writer of `resume_to` (callers hold the phase lock and write the record;
    `write_record` carries it to the runtime's bucket)."""
    value = value if value in ("personal", "work") else None
    if rec.get("resume_to") == value:
        return False
    rec["resume_to"] = value
    rec["_resume_to_written"] = True
    return True


def set_resume_to(registry: Any, slug: str, value: Optional[str]) -> None:
    """The operator's own acts (a toggle, a deferred start) name the phase to return to."""
    entities_dir = Path(registry.entities_dir)
    with fenced_here(entities_dir, slug) as rec:
        if _note_resume_to(rec, value):
            write_record(entities_dir, slug, rec)


def admit_visit_posture(entities_dir: Path, slug: str, generation: int, *, reason: str,
                        only_if_not_ours: str = "") -> bool:
    """THE visit door's state write, under the per-home arbitration (review 2026-11-09 #1/#9): the
    generation captured at the door's entry is checked, and the visiting posture written, in ONE
    locked step — a Stop between the check and the write can no longer be overwritten. Taking the
    posture also moves `admission_seq`, which invalidates an own-time spawn in progress.
    `only_if_not_ours`: rewrite only when the standing posture is not this visit's (the wait's
    hold). Raises GenerationMoved when a Stop landed since the door's entry. Returns True when it
    wrote."""
    from abstractruntime.identity.life import read_entity_state, write_entity_state

    with fenced(entities_dir, slug, generation) as rec:
        home_dir = Path(entities_dir) / slug
        now = read_entity_state(home_dir)
        if str(now.get("state") or "") == "paused":
            raise GenerationMoved("stopped")
        if only_if_not_ours and (only_if_not_ours in str(now.get("reason") or "") or now.get("mode") == "visiting"):
            return False
        write_entity_state(home_dir, "asleep", reason=reason, mode="visiting", written_by="visit-door", fence=rec.fence)
        rec["admission_seq"] = int(rec.get("admission_seq") or 0) + 1
        write_record(entities_dir, slug, rec)
        return True


def continue_sleep(entities_dir: Path, slug: str, prior: Dict[str, Any], *, fence: Any) -> None:
    """THE write of a sleep a visit interrupted, at the visit's end (every lane: the durable visit's
    close, the chat's close, an aborted open). The same sleep continues (contract §4.4): its ORIGINAL
    reason, who chose it and its wake time are written back unchanged, and the sidecar keeps its
    original start, so `since` stays where the sleep began however many visits came in between. The
    visits themselves are in the ledger (`summon` at the open, `session_closed` at the end), never in
    the sleep's reason, so the published snapshot is the one from before the visit: no `phase_changed`
    sleep -> sleep (the contract has no such edge). A standing state that already IS that sleep is
    left alone (nothing written). Callers hold the phase lock and have checked the posture is
    theirs. `fence` (required): the visit's admission (a LifecycleFence or its generation) — the
    restore never lands over a Stop since the visit was admitted (GenerationMoved, nothing written)."""
    from abstractruntime.identity.life import read_entity_state, write_entity_state

    home_dir = Path(entities_dir) / slug
    reason = str(prior.get("reason") or "") or "operator sleep restored"
    mode = str(prior.get("mode") or "")
    mode = "" if mode == "visiting" else mode
    wake_at = str(prior.get("wake_at") or "")  # bounded sleeps keep their deadline (runtime c343)
    written_by = str(prior.get("written_by") or "operator")  # who chose it travels (contract-phases §4.4)
    with fenced(entities_dir, slug, fence) as rec:
        now = read_entity_state(home_dir)
        if (str(now.get("state") or ""), str(now.get("reason") or ""), str(now.get("mode") or ""),
                str(now.get("wake_at") or ""), str(now.get("written_by") or "operator")) == (
                "asleep", reason, mode, wake_at, written_by):
            return  # already this sleep: asleep is never written over the same asleep
        override = rec.get("sleep_since_override") if isinstance(rec.get("sleep_since_override"), dict) else None
        if override and prior.get("changed_at") and str(override.get("changed_at") or "") == str(prior.get("changed_at")):
            since = override.get("since")  # the prior state was itself a continued sleep: its origin
        else:
            since = prior.get("changed_at")
        write_entity_state(home_dir, "asleep", reason=reason, mode=mode, wake_at=wake_at, written_by=written_by,
                           fence=rec.fence)
        if since:
            rec["sleep_since_override"] = {"changed_at": read_entity_state(home_dir).get("changed_at"), "since": since}
            write_record(entities_dir, slug, rec)


def write_if_still_ours(entities_dir: Path, slug: str, predicate: Callable[[Dict[str, Any]], bool],
                        write: Callable[[Any], Any], *, fence: Any) -> bool:
    """An ownership-checked restore (a visit's close, an aborted open) as ONE locked step: the
    standing state is re-read under the lifecycle lock and `write(fence)` runs only while
    `predicate` still holds and the admission's generation stands — a Stop (which writes under the
    same lock) is never overwritten. False when either refused (nothing written)."""
    from abstractruntime.identity.life import read_entity_state

    try:
        with fenced(entities_dir, slug, fence) as rec:
            if not predicate(read_entity_state(Path(entities_dir) / slug)):
                return False
            write(rec.fence)
            return True
    except GenerationMoved:
        return False


def generation_of(entities_dir: Path, slug: str) -> int:
    """The home's lifecycle generation NOW (a door's admission). Raises GenerationMoved when it
    cannot be read (fail closed: nothing is admitted on an unknown generation)."""
    rec = read_record(entities_dir, slug)
    if rec.get("generation") is None:
        raise GenerationMoved(f"the lifecycle generation of {slug} cannot be read "
                              f"({rec.get('generation_unreadable') or 'unknown'})")
    return int(rec.get("generation") or 0)


def admission_fence(entities_dir: Path, slug: str, generation: Any) -> Any:
    """The runtime LifecycleFence of an admission: `generation` an int (a door's), a LifecycleFence
    (returned as is) or None (admitted NOW, GenerationMoved when unreadable)."""
    from abstractruntime.identity.lifecycle_fence import LifecycleFence

    if isinstance(generation, LifecycleFence):
        return generation
    if generation is None:
        generation = generation_of(entities_dir, slug)
    return LifecycleFence(Path(entities_dir) / slug, int(generation))


# THE ACTING FENCES (STOP-LANE-DESIGN §2.2, gateway half): the gateway's shared, cached handles (the
# registry's EntityHome, the entity runtimes' door-wrapped effects) are not opened per admission, so
# the lane that acts through them names its fences for the duration of the act: every commit through
# the shared home's memory and diary, and every effect of a door-wrapped entity runtime run in this
# context, is refused once ANY of them moved. A mention mirrored into another entity's visit acts
# under the source's fence beside the target's (Codex review 3 #2). Context-local (one thread's act;
# a thread started inside does not inherit it — the acts here run in the caller's thread).
_ACTING: "contextvars.ContextVar[Tuple[Any, ...]]" = contextvars.ContextVar("entity_acting_fences", default=())


@contextlib.contextmanager
def acting_under(*fences: Any) -> Iterator[Tuple[Any, ...]]:
    """Run an act under these LifecycleFences (added to any already acting)."""
    from abstractruntime.identity.lifecycle_fence import LifecycleFence

    for fence in fences:
        if not isinstance(fence, LifecycleFence):
            raise TypeError(f"acting_under: a LifecycleFence, not {fence!r}")
    token = _ACTING.set(_ACTING.get() + tuple(fences))
    try:
        yield _ACTING.get()
    finally:
        _ACTING.reset(token)


def acting_fences() -> Tuple[Any, ...]:
    return _ACTING.get()


def check_acting(where: str) -> None:
    """Raise StoppedByOperator(where) when a fence of the act in progress moved (no act: nothing to
    check — a write at an operator's own door, admitted by that door)."""
    for fence in _ACTING.get():
        fence.check(where)


def acting_moved() -> bool:
    return any(fence.moved() for fence in _ACTING.get())


def moved_since(entities_dir: Path, slug: str, generation: Any) -> bool:
    """True once a Stop moved the generation an admission saw (fail closed: unknown = moved). The
    runtime fence's compare (plus one stat of the signal file), never a sidecar + yaml re-parse."""
    try:
        return admission_fence(entities_dir, slug, generation).moved()
    except GenerationMoved:
        return True


@contextlib.contextmanager
def _home_state_lock(home_dir: Path) -> Iterator[None]:
    if not home_dir.is_dir():
        yield  # no home yet (creation, a test registry): nothing of the runtime's to serialize
        return
    from abstractruntime.identity.life import home_state_lock

    with home_state_lock(home_dir, timeout_s=None):
        yield


@contextlib.contextmanager
def fenced(entities_dir: Path, slug: str, generation: Any) -> Iterator[FencedRecord]:
    """Hold the phase lock and raise GenerationMoved when a Stop landed since `generation` was
    captured (an int, or the admission's LifecycleFence). Every lifecycle writer wraps its state
    write in this (contract §3.3); the yielded record carries the fence (`rec.fence`) every runtime
    writer inside the block is handed, and only it may be written (`write_record`).

    `generation` KILL_SWITCH / RESUME (the runtime sentinels): the Stop's and the Resume's own
    writes, admitted whatever the generation (the runtime writers accept them only for what a Stop
    or a Resume writes)."""
    from abstractruntime.identity.lifecycle_fence import LifecycleFence, is_sentinel

    with phase_lock(entities_dir, slug):
        rec = read_record(entities_dir, slug)
        if is_sentinel(generation):
            fence: Any = generation
        else:
            expected = generation.generation if isinstance(generation, LifecycleFence) else int(generation)
            if rec.get("generation") is None:
                raise GenerationMoved(f"the lifecycle generation of {slug} cannot be read "
                                      f"({rec.get('generation_unreadable') or 'unknown'}): nothing admitted")
            if int(rec.get("generation") or 0) != expected:
                raise GenerationMoved(f"generation moved ({expected} -> {rec.get('generation')})")
            fence = (generation if isinstance(generation, LifecycleFence)
                     else LifecycleFence(Path(entities_dir) / slug, expected))
        frec = FencedRecord(rec, fence=fence, slug=slug)
        try:
            yield frec
        finally:
            frec.open = False


@contextlib.contextmanager
def fenced_here_or_none(entities_dir: Path, slug: str) -> Iterator[Optional[FencedRecord]]:
    """`fenced_here()`, yielding None (the lock still held) when the generation cannot be read:
    for a step that refuses or records nothing then, without an exception."""
    with phase_lock(entities_dir, slug):
        probe = read_record(entities_dir, slug)
        if probe.get("generation") is None:
            yield None
            return
        with fenced(entities_dir, slug, int(probe["generation"])) as rec:
            yield rec


@contextlib.contextmanager
def fenced_here(entities_dir: Path, slug: str) -> Iterator[FencedRecord]:
    """`fenced()` admitted at the generation read HERE, under the lock: for a write decided inside
    this one locked step (publish's rev, an operator's toggle, reconcile's own claim) — nothing it
    decided can predate a Stop, which takes the same lock. GenerationMoved when the generation
    cannot be read (fail closed)."""
    with phase_lock(entities_dir, slug):
        with fenced(entities_dir, slug, generation_of(entities_dir, slug)) as rec:
            yield rec


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    s = str(value).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


# --------------------------------------------------------------------------- hosts
def default_hosts() -> Tuple[Any, Any]:
    """(chat host, visit host) of the serving gateway; None for a host this service shape lacks."""
    chat = visit = None
    try:
        from .routes.entities import _chat_host

        chat = _chat_host()
    except Exception:  # noqa: BLE001 - a hand-built service has no chat host
        chat = None
    try:
        from .routes.entities import _visit_host

        visit = _visit_host()
    except Exception:  # noqa: BLE001
        visit = None
    return chat, visit


def _seat_probe(chat_host: Any, visit_host: Any) -> Optional[Callable[[str], Optional[Dict[str, Any]]]]:
    for host in (chat_host, visit_host):
        probe = getattr(host, "_summon_seat_probe", None) if host is not None else None
        if callable(probe):
            return probe
    return None


# --------------------------------------------------------------------------- facts
def gather(registry: Any, name: str, *, chat_host: Any = None, visit_host: Any = None) -> Dict[str, Any]:
    """Every input of the derivation, read once (pure reads; failures are labelled warnings)."""
    from abstractruntime.identity.life import (
        personal_grant_refusal,
        read_entity_state,
        read_personal_grant,
        read_work_order,
    )

    from .entity_loop import loop_status

    manifest = registry.manifest_for(name)
    slug = manifest.slug
    entities_dir = Path(registry.entities_dir)
    home_dir = entities_dir / slug
    warnings: List[str] = []
    state = read_entity_state(home_dir)
    try:
        loop = loop_status(home_dir)
    except Exception as e:  # noqa: BLE001
        loop = {"running": False, "phase": "stopped"}
        warnings.append(f"#FALLBACK loop status unreadable: {e}")
    grant = read_personal_grant(home_dir)
    personal = personal_grant_refusal(grant) is None
    expires_at = _iso(grant.get("expires_at")) if personal and str(grant.get("mode") or "") == "timer" else None
    try:
        work_order = bool(read_work_order(home_dir))
    except Exception:  # noqa: BLE001
        work_order = False
    # The job queue is the runtime's word (read_job_queue: the work order, then taken and pending
    # tasks — the order he works them).
    from abstractruntime.identity.life import read_job_queue

    job_queue = read_job_queue(home_dir)
    jobs_queued = int(job_queue.get("jobs_queued") or 0)
    if job_queue.get("note"):
        warnings.append(f"#FALLBACK job queue: {job_queue['note']}")
    chat: Dict[str, Any] = {"open": False}
    if chat_host is not None:
        try:
            chat = chat_host.status(name)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"#FALLBACK chat status unavailable: {e}")
    durable: Dict[str, Any] = {"open": False}
    if visit_host is not None:
        try:
            durable = visit_host.status(name)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"#FALLBACK durable-visit status unavailable: {e}")
    seat: Optional[Dict[str, Any]] = None
    probe = _seat_probe(chat_host, visit_host)
    if probe is not None:
        try:
            seat = probe(slug)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"#FALLBACK summon seat unreadable: {e}")
    archived = False
    try:
        from .entity_access import entity_archived, users_path_of

        archived = bool(entity_archived(slug, users_path=users_path_of(registry)))
    except Exception:  # noqa: BLE001 - an unreadable users file is not an archive
        archived = False
    return {
        "manifest": manifest,
        "slug": slug,
        "entities_dir": entities_dir,
        "home_dir": home_dir,
        "state": state,
        "loop": loop,
        "personal": personal,
        "personal_expires_at": expires_at,
        "work_order": work_order,
        "jobs_queued": jobs_queued,
        "chat": chat,
        "durable": durable,
        "seat": seat,
        "archived": archived,
        "warnings": warnings,
    }


def _person_name(principal: str) -> str:
    p = str(principal or "")
    kind, _, ident = p.partition(":")
    if kind == "entity":
        return ident.split("@", 1)[0].capitalize() or "an entity"
    if kind == "person" and ident:
        try:
            from .users import GatewayUserRegistry

            rec = GatewayUserRegistry().get_user(ident, tenant_id="default")
            name = str(getattr(rec, "display_name", "") or "").strip() if rec is not None else ""
            if name:
                return name
        except Exception:  # noqa: BLE001
            pass
        return "you" if ident in ("operator", "admin") else ident.capitalize()
    return p or "someone"


def _visit_with(participants: List[str]) -> str:
    names = [_person_name(p) for p in participants if p]
    if not names:
        return "In a visit"
    if names == ["you"]:
        return "In a visit with you"
    return "In a visit with " + ", ".join(names)


_STAMP_RE = re.compile(r"\s*\[by [^\]]+ via [^\]]+\]")


def _plain_reason(reason: Any) -> Optional[str]:
    s = _STAMP_RE.sub("", str(reason or "")).strip()
    return s or None


def is_human_seat(seat: Optional[Dict[str, Any]]) -> bool:
    """An INTERACTIVE human-held summon: the holder is a person principal AND declared human.
    The declaration alone never qualifies (contract §6.2)."""
    if not seat:
        return False
    holder = str(seat.get("holder") or "")
    return str(seat.get("holder_kind") or "") == "human" and holder.startswith("person:") and not seat.get("automation")


def _sleep_kind(state: Dict[str, Any], loop_alive: bool) -> Optional[str]:
    if str(state.get("state") or "") != "asleep" or str(state.get("mode") or "") == "visiting":
        return None
    writer = str(state.get("written_by") or "operator")
    if writer == IDLE_SWEEP_WRITER:
        return "self"  # the idle thresholds' sleep is his own, with no loop running (contract v3 D2)
    if writer == "operator":
        return "operator"
    if writer in SELF_SLEEP_WRITERS and loop_alive:
        return "self"
    return None  # a gate sleep (1178) or a self sleep left by a dead loop: idle


def running_sleep(facts: Dict[str, Any]) -> Optional[str]:
    return _sleep_kind(facts["state"], bool(facts["loop"].get("running")))


def visit_live(facts: Dict[str, Any]) -> bool:
    st = facts["state"]
    return (
        bool(facts["chat"].get("open"))
        or bool(facts["durable"].get("open"))
        or (str(st.get("state") or "") == "asleep" and str(st.get("mode") or "") == "visiting")
        or is_human_seat(facts["seat"])
    )


def work_live(facts: Dict[str, Any]) -> bool:
    loop = facts["loop"]
    seat = facts["seat"]
    if seat is not None and not is_human_seat(seat):
        return True
    return bool(loop.get("running")) and str(loop.get("phase") or "") == "day" and _work_day(facts)


def _work_day(facts: Dict[str, Any]) -> bool:
    kind = str(facts["loop"].get("day_kind") or "")
    if kind:
        return kind == "work"
    return bool(facts["work_order"])


# --------------------------------------------------------------------------- derive
def derive(facts: Dict[str, Any], rec: Dict[str, Any]) -> Dict[str, Any]:
    """The snapshot (contract §1). Pure: facts + the sidecar record in, the semantic snapshot out
    (rev/generation/since are added by `publish`)."""
    manifest = facts["manifest"]
    name = str(getattr(manifest, "name", "") or manifest.slug)
    state = facts["state"]
    loop = facts["loop"]
    chat = facts["chat"]
    durable = facts["durable"]
    seat = facts["seat"]
    word = str(state.get("state") or "awake")
    mode = str(state.get("mode") or "")
    loop_alive = bool(loop.get("running"))
    # Only own time is a switch (contract v3 D1): work starts when a job is queued.
    toggles = {"personal": bool(facts["personal"])}
    stopped = word == "paused"
    maint = rec.get("maintenance") if isinstance(rec.get("maintenance"), dict) else None
    maintenance_running = bool(maint and maint.get("status") == "running")

    visit = work = sleep = None
    if stopped:
        phase = "stopped"
    elif visit_live(facts):
        phase = "visit"
        waiting = None
        if bool(chat.get("open")):
            visit = {"lane": "chat", "run_id": None, "chat_id": chat.get("chat_id"),
                     "session_id": chat.get("session_id"),
                     "participants": list(chat.get("participants") or []),
                     "since": _iso(chat.get("opened_at"))}
        elif bool(durable.get("open")):
            visit = {"lane": "visit", "run_id": durable.get("run_id"), "chat_id": None,
                     "session_id": durable.get("session_id"),
                     "participants": list(durable.get("participants") or []),
                     "since": _iso(durable.get("created_at"))}
        elif is_human_seat(seat):
            visit = {"lane": "summon", "run_id": seat.get("run_id"), "chat_id": None,
                     "session_id": seat.get("session_id"),
                     "participants": [str(seat.get("holder"))],
                     "since": _iso(seat.get("held_since"))}
        else:
            # A visit is opening (posture written, the session not registered yet): it waits for
            # the loop or the maintenance pass to yield (a visit never fails, ADR-0027).
            if maintenance_running:
                waiting = "maintenance"
            elif loop_alive and str(loop.get("phase") or "") == "day":
                waiting = "loop"
            m = re.search(r"in conversation with (\S+)", str(state.get("reason") or ""))
            visit = {"lane": "visit", "run_id": None, "chat_id": None, "session_id": None,
                     "participants": [m.group(1)] if m else [], "since": _iso(state.get("changed_at"))}
        visit["waiting_for"] = waiting
        # The visitors only: the entity's own stamp is not someone it is with.
        visit["participants"] = [p for p in visit["participants"] if str(p) != str(manifest.entity_id)
                                 and not str(p).startswith(f"entity:{manifest.slug}")]
    elif work_live(facts):
        phase = "work"
        if seat is not None and not is_human_seat(seat):
            kind = "automation" if seat.get("automation") else "summon"
            work = {"kind": kind, "run_id": seat.get("run_id"), "since": _iso(seat.get("held_since"))}
        else:
            work = {"kind": "work_order" if facts["work_order"] else "task", "run_id": None, "since": None}
    elif _sleep_kind(state, loop_alive):
        phase = "sleep"
        kind = _sleep_kind(state, loop_alive)
        if mode == "dreaming" or maintenance_running:
            ends: Optional[str] = MAINTENANCE_ENDS
        elif state.get("wake_at"):
            ends = _iso(state.get("wake_at"))
        elif kind == "self":
            # His own sleep carries no clock (runtime a5ac083): he wakes when its maintenance is done.
            ends = MAINTENANCE_ENDS
        else:
            ends = None
        since = _iso(state.get("changed_at"))
        override = rec.get("sleep_since_override") if isinstance(rec.get("sleep_since_override"), dict) else None
        if override and str(override.get("changed_at") or "") == str(state.get("changed_at") or ""):
            since = _iso(override.get("since")) or since
        sleep = {"kind": kind, "reason": _plain_reason(state.get("reason")), "since": since, "ends": ends}
    elif loop_alive:
        phase = "personal"
    else:
        phase = "idle"

    # The job queue (v3 D1): always present; `current` is the job running now.
    work = {"jobs_queued": int(facts["jobs_queued"]), "current": work}
    blocked = _blocked(facts, rec, phase, toggles)
    snap: Dict[str, Any] = {
        "entity_id": manifest.entity_id,
        "name": manifest.slug,
        "phase": phase,
        "stopped": stopped,
        "toggles": toggles,
        "personal_expires_at": facts["personal_expires_at"],
        "sentence": _sentence(name, phase, visit, work, sleep, loop, blocked),
        "next": _next(phase, toggles, sleep, work, rec.get("resume_to"), _visit_prior(chat, durable)),
        "blocked": blocked,
        "can": _can(facts, rec, phase, stopped),
        "visit": visit,
        "work": work,
        "sleep": sleep,
        # The existing life_state fields, unchanged in meaning (posture gains "idle").
        "posture": _posture(phase, state, loop, chat, durable),
        "chat_open": bool(chat.get("open")),
        "chat_id": chat.get("chat_id"),
        # The state word follows the phase (FINDINGS 2026-11-09 §(1)/(2)): a visit's door writes
        # `asleep` + mode `visiting` as its posture and a gate sleep (1178) is not a sleep, so the
        # stored word read "asleep" beside phase=visit or idle. Served: paused while stopped, asleep
        # in a sleep, awake otherwise (the written record stays GET /state's; `state_mode` and
        # `state_reason` are still the file's).
        "state": {"stopped": "paused", "sleep": "asleep"}.get(phase, "awake"),
        "state_mode": state.get("mode"),
        "state_reason": state.get("reason"),
        "own_time_running": loop_alive,
        "own_time_phase": (loop.get("phase") or None),
        "visit_run_id": durable.get("run_id") if durable.get("open") else None,
    }
    return snap


def _posture(phase: str, state: Dict[str, Any], loop: Dict[str, Any], chat: Dict[str, Any], durable: Dict[str, Any]) -> str:
    if phase == "stopped":
        return "paused"
    if phase == "visit":
        if str(state.get("mode") or "") == "visiting" and not chat.get("open") and not durable.get("open"):
            return "yielded"
        return "visiting"
    if phase == "sleep":
        return "asleep"
    if phase in ("work", "personal"):
        return "day" if str(loop.get("phase") or "") == "day" else "resting"
    return "idle"


def _blocked(facts: Dict[str, Any], rec: Dict[str, Any], phase: str, toggles: Dict[str, bool]) -> Optional[Dict[str, Any]]:
    if phase != "idle" or not toggles["personal"]:
        return None
    if facts["archived"]:
        return {"code": "entity_archived", "sentence": "He is archived; restore him from the roster first.", "action": None}
    err = str(rec.get("start_error") or "").strip()
    if err:
        return {"code": "own_time_start_failed", "sentence": f"His own time could not start: {err}", "action": "retry"}
    loop = facts["loop"]
    crashed = "process is gone" in str(loop.get("note") or "") or str(loop.get("stopped_by") or "") == "failures"
    if crashed:
        return {"code": "loop_crashed", "sentence": "His own time stopped unexpectedly. Retry to start it again.", "action": "retry"}
    return {"code": "own_time_not_running", "sentence": "His own time is not running. Retry to start it.", "action": "retry"}


def _sentence(name: str, phase: str, visit: Any, work: Any, sleep: Any, loop: Dict[str, Any], blocked: Any) -> str:
    if phase == "stopped":
        return "Stopped. Nothing runs until you resume him."
    if phase == "visit":
        base = _visit_with(list((visit or {}).get("participants") or []))
        if (visit or {}).get("waiting_for"):
            return base + "; he is finishing what he was doing."
        return base + "."
    if phase == "work":
        kind = ((work or {}).get("current") or {}).get("kind")
        base = {"work_order": "Working on his work order", "task": "Working on a task you left him",
                "automation": "Working on an automation"}.get(str(kind), "Working on a request from an agent")
        return base + "."
    if phase == "personal":
        return "On his own time." if str(loop.get("phase") or "") == "day" else "On his own time, resting."
    if phase == "sleep":
        ends = (sleep or {}).get("ends")
        if ends == MAINTENANCE_ENDS:
            return "Resting for maintenance; wakes when done."
        if ends:
            return "Napping; wakes on his own."
        return "Asleep until you wake him."
    if blocked and blocked.get("code") in ("loop_crashed", "own_time_start_failed", "own_time_not_running"):
        return "Awake and reachable; his own time stopped unexpectedly." if blocked["code"] == "loop_crashed" else (
            "Awake and reachable; his own time is not running."
        )
    return "Awake and reachable; nothing of his own is running."


def _visit_prior(chat: Dict[str, Any], durable: Dict[str, Any]) -> Optional[str]:
    """The state word the open visit interrupted (recorded by the door at open), or None."""
    for status in (chat, durable):
        if status.get("open") and isinstance(status.get("prior_state"), dict):
            return str(status["prior_state"].get("state") or "") or None
    return None


def _back_to(resume_to: Optional[str], toggles: Dict[str, bool]) -> Tuple[str, Optional[str]]:
    """Where an interruption returns (the wire face of the sidecar's `resume_to`): the remembered
    own-time phase while its grant is on; "idle" otherwise, with the sentence when the operator
    revoked it meanwhile."""
    if resume_to == "personal":
        return ("personal", None) if toggles["personal"] else ("idle", "Own time is off; he stays awake.")
    if resume_to == "work":
        return "work", None  # his jobs (no switch: v3 D1)
    return "idle", None


def _next(phase: str, toggles: Dict[str, bool], sleep: Any, work: Any, resume_to: Optional[str] = None,
          visit_prior: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """What happens next without anyone acting (contract-phases v2 §1.1). During an interruption
    (a visit, a sleep, a request) `phase` is where it returns: `resume_to` while its grant is on."""
    back, revoked = _back_to(resume_to, toggles)
    if phase == "visit":
        if visit_prior == "asleep":
            return {"phase": "sleep", "sentence": "When the visit closes, he goes back to sleep."}
        if revoked:
            return {"phase": "idle", "sentence": revoked}
        return {"phase": back, "sentence": {
            "personal": "When the visit closes, his own time resumes.",
            "work": "When the visit closes, he goes back to his work.",
        }.get(back, "When the visit closes, he stays awake and reachable.")}
    if phase == "work":
        # Where he returns when all his jobs are done (v3 D1/D4): own time while it is still
        # given, else idle.
        if revoked:
            return {"phase": "idle", "sentence": revoked}
        back = "personal" if back == "personal" else "idle"
        return {"phase": back, "sentence": {
            "personal": "When his jobs are done, his own time resumes.",
        }.get(back, "When his jobs are done, he stays awake and reachable.")}
    if phase == "sleep":
        ends = (sleep or {}).get("ends")
        if revoked:
            return {"phase": "idle", "sentence": revoked}
        if not ends:
            return None  # asleep until a person wakes him
        when = "When maintenance is done he wakes" if ends == MAINTENANCE_ENDS else "When he wakes"
        return {"phase": back, "sentence": {
            "personal": f"{when} and his own time resumes.",
            "work": f"{when} and goes back to his work.",
        }.get(back, f"{when}, awake and reachable.")}
    return None


def _can(facts: Dict[str, Any], rec: Dict[str, Any], phase: str, stopped: bool) -> Dict[str, Dict[str, Any]]:
    yes = {"allowed": True, "sentence": None}

    def no(sentence: str) -> Dict[str, Any]:
        return {"allowed": False, "sentence": sentence}

    archived = "He is archived; restore him from the roster first."
    if stopped:
        return {
            "visit": no("Stopped. Resume to visit."),
            "sleep_now": no("Stopped. Resume him first."),
            "toggle_personal": no("Stopped. Resume him first, then choose what he may do."),
        }
    if facts["archived"]:
        return {"visit": no(archived), "sleep_now": no(archived), "toggle_personal": no(archived)}
    if phase == "visit":
        sleep_now = no("He is in a visit; end it to let him sleep.")
    elif phase == "sleep":
        # A sleep is never followed by another sleep: refused while asleep. Once he is awake
        # (idle included) the operator may ask for maintenance again; the loop's own chained-sleep
        # refusal is the runtime's (contract-phases v2 §10.5).
        sleep_now = no("He is already resting; a sleep cannot follow a sleep.")
    else:
        sleep_now = yes
    return {"visit": dict(yes), "sleep_now": sleep_now, "toggle_personal": dict(yes)}


def _first_since(phase: str, snap: Dict[str, Any], facts: Dict[str, Any]) -> Optional[str]:
    """`since` on a home's first derive (no recorded transition yet): the source fact."""
    sub = snap.get(phase) if phase in ("visit", "work", "sleep") else None
    if phase == "work" and isinstance(sub, dict):
        sub = sub.get("current")
    if isinstance(sub, dict) and sub.get("since"):
        return sub["since"]
    return _iso(facts["state"].get("changed_at")) or _iso(facts["loop"].get("updated_at"))


# --------------------------------------------------------------------------- publish
_SEMANTIC_EXCLUDE = ("warnings",)
# Jobs the own-time loop works on (a loop work day), as opposed to a request holding the seat.
LOOP_JOB_KINDS = ("work_order", "task")


def _work_kind(snap: Optional[Dict[str, Any]]) -> Optional[str]:
    """The running job's kind in a snapshot (v3 shape `work.current.kind`; a v2 snapshot recorded
    before the change kept it at `work.kind`)."""
    work = (snap or {}).get("work") or {}
    if not isinstance(work, dict):
        return None
    current = work.get("current") if "current" in work else work
    return (current or {}).get("kind") if isinstance(current, dict) else None


def publish(
    registry: Any,
    name: str,
    cause: str = "observed",
    *,
    chat_host: Any = None,
    visit_host: Any = None,
    hosts: bool = True,
) -> Dict[str, Any]:
    """Derive, version and announce. Returns the full `/life_state` body.

    Under the phase lock: gather + derive; when the semantic snapshot differs from the recorded
    one, `rev + 1` is written with the new snapshot (and `since` when the phase changed); then any
    rev newer than `published_rev` is announced as a `phase_changed` host marker (dedup by rev)
    and `published_rev` advances. A crash between the two writes is recovered by the next call."""
    if hosts and chat_host is None and visit_host is None:
        chat_host, visit_host = default_hosts()
    manifest = registry.manifest_for(name)
    slug = manifest.slug
    entities_dir = Path(registry.entities_dir)
    follow_up: Optional[str] = None
    with _publish_record(entities_dir, slug) as rec:
        facts = gather(registry, name, chat_host=chat_host, visit_host=visit_host)
        if not isinstance(rec, FencedRecord):
            facts["warnings"].append(f"#FALLBACK lifecycle generation unreadable "
                                     f"({rec.get('generation_unreadable')}); nothing recorded, nothing admitted")
        snap = derive(facts, rec)
        prev = rec.get("snapshot") if isinstance(rec.get("snapshot"), dict) else None
        prev_phase = (prev or {}).get("phase")
        if prev is None:
            since = _first_since(snap["phase"], snap, facts)
        elif prev_phase != snap["phase"]:
            # A sleep began when it began: after a visit the same sleep continues from its origin.
            since = ((snap.get("sleep") or {}).get("since") if snap["phase"] == "sleep" else None) or _now_iso()
        else:
            since = rec.get("since")
        semantic = {k: v for k, v in snap.items() if k not in _SEMANTIC_EXCLUDE}
        semantic["since"] = since
        semantic["generation"] = int(rec.get("generation") or 0)
        # resume_to follows what own time is doing: running personal/work names itself; own time
        # ending WITHOUT an interruption (personal/work straight to idle: its own end, the operator's
        # toggle, a crash) clears it; an interruption (visit, sleep, a request, a Stop) keeps it.
        new_phase = snap["phase"]
        # Only own time names itself (v3; Codex review 2 #6): a work day the queue drives is not a
        # place to return to, and it must never overwrite the personal time a job interrupted.
        own_kind = "personal" if new_phase == "personal" else None
        resume_changed = False
        # Work that INTERRUPTED own time (a summon, an automation) is not own time ending (review
        # #10): only an own-time work day (kind work_order) ending clears the target.
        prev_work_kind = _work_kind(prev)
        prev_was_own_time = prev_phase == "personal" or (prev_phase == "work" and prev_work_kind in LOOP_JOB_KINDS)
        prev_was_interruption = prev_phase in ("visit", "sleep") or (prev_phase == "work" and prev_work_kind not in LOOP_JOB_KINDS)
        if own_kind:
            resume_changed = _note_resume_to(rec, own_kind)
        elif new_phase == "idle" and prev_was_own_time:
            resume_changed = _note_resume_to(rec, None)
        elif (new_phase == "idle" and prev_was_interruption and rec.get("resume_to")
              and cause in ("observed", "grant_expired")):
            # An interruption ended where the gateway has no write point (a self sleep's end, a
            # request's end seen on read): restore what it interrupted.
            follow_up = "restore"
        if resume_changed and prev == semantic:
            _write_published(entities_dir, slug, rec)
        if prev != semantic:
            rec["rev"] = int(rec.get("rev") or 0) + 1
            rec["snapshot"] = semantic
            rec["since"] = since
            if snap["phase"] in ACTIVITY_PHASES:
                rec["last_activity_phase"] = snap["phase"]
            rec["last_cause"] = str(cause or "observed")
            rec["last_from"] = prev_phase
            _write_published(entities_dir, slug, rec)
            # A loop that ended because it was asked to (graceful stop, personal off) hands the
            # phase back to the gateway: reconcile repairs a gate sleep and restarts own time when
            # personal is still on. A crash never does (no auto-respawn in v1).
            if prev and prev.get("own_time_running") and not snap["own_time_running"] and snap["phase"] != "stopped":
                if str(facts["loop"].get("stopped_by") or "") in REQUESTED_STOPS and "process is gone" not in str(
                    facts["loop"].get("note") or ""
                ):
                    follow_up = "own_time_ended"
        if isinstance(rec, FencedRecord) and int(rec.get("rev") or 0) > int(rec.get("published_rev") or 0):
            _announce(registry, manifest, rec)
        _arm_expiry_timer(registry, name, facts.get("personal_expires_at"))
        body = dict(rec["snapshot"])
        body["rev"] = int(rec["rev"])
        body["warnings"] = list(facts["warnings"])
    if follow_up:
        try:
            reconcile(registry, name, follow_up, chat_host=chat_host, visit_host=visit_host)
            return publish(registry, name, "own_time_started" if follow_up == "restore" else follow_up,
                           chat_host=chat_host, visit_host=visit_host)
        except Exception:  # noqa: BLE001 - the read stays served
            logger.exception("reconcile after own time ended failed for %s", slug)
    return body


@contextlib.contextmanager
def _publish_record(entities_dir: Path, slug: str) -> Iterator[Dict[str, Any]]:
    """publish's record: fenced at the generation read under the lock; when that generation cannot
    be read, the plain record (the read is served, `_write_published` writes nothing)."""
    with phase_lock(entities_dir, slug):
        probe = read_record(entities_dir, slug)
        if probe.get("generation") is None:
            yield probe
            return
        with fenced(entities_dir, slug, int(probe["generation"])) as rec:
            yield rec


def _write_published(entities_dir: Path, slug: str, rec: Dict[str, Any]) -> None:
    if isinstance(rec, FencedRecord):
        write_record(entities_dir, slug, rec)


def _announce(registry: Any, manifest: Any, rec: Dict[str, Any]) -> None:
    """Append the `phase_changed` host marker for the record's current rev (dedup by rev)."""
    from .entity_replay import record_host_marker

    snap = rec["snapshot"]
    try:
        home = registry.get_home(manifest.slug)
        seq = int(home.memory.current_seq())
    except Exception:  # noqa: BLE001 - position is a floor, labelled
        seq = 0
    try:
        record_host_marker(
            entities_dir=Path(registry.entities_dir),
            slug=manifest.slug,
            entity_id=manifest.entity_id,
            kind="phase_changed",
            journal_seq=seq,
            details={
                "name": manifest.slug,
                "from": rec.get("last_from"),
                "to": snap.get("phase"),
                "phase": snap.get("phase"),
                "toggles": snap.get("toggles"),
                "stopped": snap.get("stopped"),
                "since": snap.get("since"),
                "rev": int(rec["rev"]),
                "generation": int(snap.get("generation") or 0),
                "cause": rec.get("last_cause") or "observed",
                "sentence": snap.get("sentence"),
            },
            dedup_field="rev",
        )
    except Exception:  # noqa: BLE001 - the next read retries (published_rev not advanced)
        logger.exception("phase_changed marker for %s rev %s failed (the next read retries)", manifest.slug, rec.get("rev"))
        return
    rec["published_rev"] = int(rec["rev"])
    write_record(Path(registry.entities_dir), manifest.slug, rec)


_EXPIRY_TIMERS: Dict[str, Tuple[str, threading.Timer]] = {}
_EXPIRY_GUARD = threading.Lock()


def _arm_expiry_timer(registry: Any, name: str, expires_at: Optional[str]) -> None:
    """One timer per home at an armed `personal_expires_at`: the expiry is published when it
    happens, not at the next read."""
    if not expires_at:
        return
    try:
        due = datetime.fromisoformat(expires_at).timestamp() - time.time()
    except ValueError:
        return
    if due <= 0 or due > 7 * 86400:
        return
    key = f"{registry.entities_dir}:{name}"
    with _EXPIRY_GUARD:
        current = _EXPIRY_TIMERS.get(key)
        if current is not None and current[0] == expires_at:
            return
        if current is not None:
            current[1].cancel()

        def _fire() -> None:
            try:
                publish(registry, name, "grant_expired")
            except Exception:  # noqa: BLE001
                logger.exception("expiry publish failed for %s", name)

        timer = threading.Timer(due + 0.5, _fire)
        timer.daemon = True
        _EXPIRY_TIMERS[key] = (expires_at, timer)
        timer.start()


def safe_publish(registry: Any, name: str, cause: str, **kw: Any) -> None:
    """Publish from a write point; never fails the act that triggered it."""
    try:
        publish(registry, name, cause, **kw)
    except Exception:  # noqa: BLE001
        logger.exception("phase publish (%s) failed for %s", cause, name)


# --------------------------------------------------------------------------- stop / resume
def _actor() -> str:
    from .security.principal import current_gateway_principal

    principal = current_gateway_principal()
    return f"person:{principal.user_id}" if principal is not None else "person:operator"


def _marker(registry: Any, manifest: Any, kind: str, details: Dict[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    """Append one host marker; returns (its seq, None) or (None, the failure). A marker that cannot
    be written never undoes the act it records (the caller labels the gap)."""
    from . import entity_replay

    try:
        home = registry.get_home(manifest.slug)
        env = entity_replay.record_host_marker(
            entities_dir=Path(registry.entities_dir), slug=manifest.slug, entity_id=manifest.entity_id,
            kind=kind, journal_seq=int(home.memory.current_seq()), details=details,
        )
        return (env.get("seq") if isinstance(env, dict) else None), None
    except Exception as e:  # noqa: BLE001 - markers are observability
        logger.exception("%s marker for %s failed", kind, manifest.slug)
        return None, str(e)


def _item(item: str, ident: Any, outcome: str, code: Optional[str] = None, sentence: Optional[str] = None,
          *, store: Optional[str] = None) -> Dict[str, Any]:
    row = {"item": item, "id": (str(ident) if ident not in (None, "") else None), "outcome": outcome, "code": code, "sentence": sentence}
    if store:
        row["store"] = store  # where a run lives (gateway | home): a repeated Stop retries it there
    return row


def _run_owner(registry: Any, slug: str, store: str, svc: Any = None) -> Tuple[Any, Any]:
    """(runtime, run_store) of a run's home: the gateway's own store, or the entity's home store."""
    if store == "home":
        er = registry.get_entity_runtime(slug)
        return er.runtime, er.run_store
    svc = svc if svc is not None else _service()
    host = getattr(svc, "host", None)
    return getattr(host, "runtime", None), getattr(host, "run_store", None)


def stop(
    registry: Any,
    name: str,
    *,
    reason: str = "",
    actor: Optional[str] = None,
    via: str = "POST /entities/{name}/lifecycle",
    chat_host: Any = None,
    visit_host: Any = None,
    svc: Any = None,
) -> Dict[str, Any]:
    """The kill switch (contract §3.1): fence, signal, tear down, report per item. It also takes back
    his own time (v3 D3, operator 2026-11-09): the grant is revoked and nothing is remembered to
    return to — Resume brings him back idle, and own time only when the operator gives it again."""
    from abstractruntime.identity.life import (
        personal_grant_refusal,
        read_entity_state,
        read_personal_grant,
        request_loop_stop,
        write_entity_state,
        write_personal_grant,
    )

    t0 = time.monotonic()
    if chat_host is None and visit_host is None:
        chat_host, visit_host = default_hosts()
    manifest = registry.manifest_for(name)
    slug = manifest.slug
    entities_dir = Path(registry.entities_dir)
    home_dir = entities_dir / slug
    actor = actor or _actor()
    via = via.replace("{name}", slug)

    # 1. FENCE FIRST (review 2026-11-09 #4): under the lock the generation moves and paused is
    # written: from here on every door and every fenced writer refuses. Own time is taken back in
    # the same step (v3 D3): the grant is revoked and `resume_to` cleared, so nothing restarts it.
    revoked = False
    revoke_error: Optional[str] = None
    signal_error: Optional[str] = None
    from abstractruntime.identity.lifecycle_fence import KILL_SWITCH

    with fenced(entities_dir, slug, KILL_SWITCH) as rec:
        already = str(read_entity_state(home_dir).get("state") or "") == "paused"
        prior_outstanding = list(rec.get("stop_outstanding") or [])
        _note_resume_to(rec, None)
        try:
            if personal_grant_refusal(read_personal_grant(home_dir)) is None:
                write_personal_grant(home_dir, mode="disabled", granted_by="kill_switch", fence=KILL_SWITCH)
                revoked = True
        except Exception as e:  # noqa: BLE001 - reported on its row; the stop goes on
            revoke_error = str(e)
            logger.exception("stop: the personal grant of %s could not be revoked", slug)
        from abstractruntime.identity.life import bump_lifecycle_generation
        from abstractruntime.identity.lifecycle_fence import LifecycleSignalUnwritten

        try:
            rec["generation"] = int(bump_lifecycle_generation(home_dir, written_by="gateway:kill_switch", timeout_s=None))
        except LifecycleSignalUnwritten as e:
            # The Stop stands (phases.yaml moved, this process's fences see it); only the signal file
            # other processes stat is missing — they see the Stop at their next read. Said on its row.
            rec["generation"] = int(e.generation)
            signal_error = str(e)
        maint = rec.get("maintenance") if isinstance(rec.get("maintenance"), dict) else None
        if maint and maint.get("status") == "running":
            maint["status"] = "cancel_requested"
        write_record(entities_dir, slug, rec)
        if not already:
            stamp = f"[by {actor} via {via}]"
            write_entity_state(home_dir, "paused", reason=f"stopped{(': ' + reason) if reason else ''} {stamp}",
                               fence=KILL_SWITCH)
    pause_seq: Optional[float] = None
    pause_error: Optional[str] = None
    if not already:
        pause_seq, pause_error = _marker(registry, manifest, "pause",
                                         {"state": "paused", "reason": reason or "stopped", "by": actor, "channel": "kill_switch"})

    items: List[Dict[str, Any]] = []
    cancelled = {"visits": [], "chats": [], "runs": [], "loop_pid": None}
    if revoked:
        _marker(registry, manifest, "personal_grant_revoked",
                {"mode": "disabled", "revoked_by": "kill_switch", "by": actor, "channel": "kill_switch"})
        # The Entity app reads exactly this row (abstractentity 0848c19): outcome done, code revoked.
        items.append(_item("own_time", None, "done", "revoked", OWN_TIME_REVOKED_SENTENCE))
    elif revoke_error:
        items.append(_item("own_time", None, "failed", "grant_revoke_failed",
                           f"His own time could not be taken back: {revoke_error}"))
    if signal_error:
        items.append(_item("signal", None, "failed", "lifecycle_signal_unwritten",
                           "The Stop stands, but its signal file could not be written; other processes see it at "
                           f"their next read. Press Stop again. ({signal_error})"))

    # 2a. A REPEATED STOP RETRIES what the last one could not finish (review #5), in the store the
    # item came from — a cancelled root no longer appears in live discovery, its failed child does
    # here. The list is replaced only by this round's own failures.
    for old in prior_outstanding:
        if old.get("item") == "run" and old.get("id"):
            runtime, store = _run_owner(registry, slug, str(old.get("store") or "gateway"), svc)
            for o in _live_outcomes(_cancel_tree(runtime, store, str(old["id"]))):
                items.append(_item("run", o["run_id"], o["outcome"], o.get("code"), o.get("error"), store=old.get("store")))
                if o["outcome"] == "cancelled":
                    cancelled["runs"].append(o["run_id"])

    # 2. SIGNAL at once: the loop's durable stop command, live runs, queued admissions, an
    # in-flight hosted turn (its session yields at its next model/tool boundary).
    try:
        request_loop_stop(home_dir, reason="kill switch", requested_by="admin")
    except Exception:  # noqa: BLE001 - the SIGTERM below is the real stop
        pass
    seat_run = None
    probe = _seat_probe(chat_host, visit_host)
    try:
        from .entity_seat import read_seat

        seat = read_seat(entities_dir, slug)
        seat_run = str((seat or {}).get("run_id") or "") or None
    except Exception:  # noqa: BLE001
        seat_run = None
    if seat_run:
        runtime, store = _run_owner(registry, slug, "gateway", svc)
        outcomes = _live_outcomes(_cancel_tree(runtime, store, seat_run))
        for o in outcomes:
            items.append(_item("run", o["run_id"], o["outcome"], o.get("code"), o.get("error"), store="gateway"))
            if o["outcome"] == "cancelled":
                cancelled["runs"].append(o["run_id"])
    del probe
    durable = {"open": False}
    if visit_host is not None:
        try:
            durable = visit_host.status(name)
        except Exception:  # noqa: BLE001
            durable = {"open": False}
    visit_run = str(durable.get("run_id") or "") if durable.get("open") else ""
    visit_cancelled = False
    if visit_run:
        # The open visit ends CANCELLED by the operator, in flight or parked (a clean terminal
        # record, never a completed close nor a failure), then its terminal duty runs.
        if True:
            try:
                er = registry.get_entity_runtime(slug)
                for o in _live_outcomes(_cancel_tree(er.runtime, er.run_store, visit_run)):
                    items.append(_item("run", o["run_id"], o["outcome"], o.get("code"), o.get("error"), store="home"))
                    if o["outcome"] == "cancelled":
                        cancelled["runs"].append(o["run_id"])
                        visit_cancelled = True
            except Exception as e:  # noqa: BLE001
                items.append(_item("run", visit_run, "failed", "run_cancel_failed", f"His visit turn could not be cancelled: {e}",
                                   store="home"))
    chat_row = _stop_chat(chat_host, name, cancelled)
    items.append(_queue_stop(entities_dir, slug))
    maint = read_record(entities_dir, slug).get("maintenance")
    if isinstance(maint, dict) and maint.get("status") == "cancel_requested":
        items.append(_item("maintenance", None, "cancel_requested", "maintenance_still_running",
                           "His maintenance pass is finishing; it will not wake him."))
    else:
        items.append(_item("maintenance", None, "not_running"))

    # 3. TEAR DOWN, each step on its own.
    loop_row = _kill_loop(home_dir, cancelled, reason)
    items.append(loop_row)
    if loop_row["outcome"] in ("killed_sigterm", "killed_sigkill"):
        # His own time was cut off: a moment of his biography he could not write himself.
        _marker(registry, manifest, "personal_frozen", {"channel": "kill_switch", "by": actor, "reason": reason or "stopped",
                                                        "pid": loop_row.get("id"), "escalated_to_sigkill": loop_row["outcome"] == "killed_sigkill"})
    items.append(_close_durable(registry, visit_host, name, visit_run, visit_cancelled, cancelled))
    items.append(chat_row)

    outstanding = [i for i in items if i["outcome"] == "failed"]
    with fenced(entities_dir, slug, KILL_SWITCH) as rec:
        rec["stop_outstanding"] = outstanding
        write_record(entities_dir, slug, rec)
    body = publish(registry, name, "stop", chat_host=chat_host, visit_host=visit_host)
    complete = not outstanding and not any(i["outcome"] == "cancel_requested" for i in items)
    loop_item = next((i for i in items if i["item"] == "loop"), {})
    body.update({
        "action": "stop",
        "already": already,
        "took_ms": int((time.monotonic() - t0) * 1000),
        "complete": complete,
        "items": items,
        "cancelled": cancelled,
        "loop_signal": {"killed_sigterm": "sigterm", "killed_sigkill": "sigkill"}.get(str(loop_item.get("outcome") or "")),
        "started": {"own_time": False, "pid": None},
        "marker_seq": pause_seq,
        "own_time_revoked": revoked,
    })
    if pause_error:
        body["warning"] = f"#FALLBACK state applied but the host marker failed: {pause_error}"
    return body


def _service() -> Any:
    from .service import get_gateway_service

    return get_gateway_service()


def _cancel_tree(runtime: Any, run_store: Any, run_id: str) -> List[Dict[str, Any]]:
    if runtime is None or run_store is None:
        return [{"run_id": run_id, "outcome": "failed", "code": "run_cancel_failed", "error": "no runtime to cancel it on"}]
    from .entity_seat import cancel_run_tree_outcomes

    # A CLEAN TERMINAL RECORD (operator 2026-11-09: "the ledger must be kept clean"): every run the
    # kill switch ends is CANCELLED, reason kill_switch, by the operator — the runtime stops an
    # effect in flight and closes its ledger record as `cancelled` naming who asked.
    return cancel_run_tree_outcomes(runtime, run_store, run_id, reason=KILL_SWITCH_REASON, cancelled_by="operator")


def _live_outcomes(outcomes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The Stop receipt lists the runs the Stop found LIVE only: the ones it cancelled and the ones
    it could not (a failure the next Stop retries). A run that had already finished (the summon
    seat outlives its run; a visit's completed turns) or that is gone is not part of the receipt:
    it was never stopped."""
    return [o for o in outcomes if o.get("outcome") in ("cancelled", "failed")]


def _queue_stop(entities_dir: Path, slug: str) -> Dict[str, Any]:
    """Queued admissions never start while stopped: they are failed with the sentence (the queue's
    own rule for a refusal that can never admit)."""
    try:
        from .entity_queue import read_queue, write_queue
        from .entity_seat import summon_guard_lock

        with summon_guard_lock(slug):
            entries = read_queue(entities_dir, slug)
            n = 0
            for e in entries:
                if str(e.get("state") or "") == "queued":
                    e["state"] = "failed"
                    e["failed_reason"] = "Stopped. Resume him first."
                    n += 1
            if n:
                write_queue(entities_dir, slug, entries)
        return _item("queue", None, "cleared" if n else "not_open")
    except Exception as e:  # noqa: BLE001
        return _item("queue", None, "failed", "queue_clear_failed", f"His waiting requests could not be cleared: {e}")


_KILL_LOCKS: Dict[str, threading.Lock] = {}
_KILL_LOCKS_GUARD = threading.Lock()


@contextlib.contextmanager
def _kill_lock(home_dir: Path) -> Iterator[None]:
    """ONE hard stop per home at a time, across threads and processes (review 2026-11-09 #3: two
    concurrent Stops both passed the liveness check, and the second hard stop read the first one's
    status stamp — the gateway's own pid — as the loop and signalled the gateway)."""
    key = str(Path(home_dir).resolve())
    with _KILL_LOCKS_GUARD:
        lock = _KILL_LOCKS.setdefault(key, threading.Lock())
    with lock:
        lock_path = Path(home_dir).parent / SIDECAR_DIRNAME / f"{Path(home_dir).name}.kill.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+") as fh:
            try:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass
            yield


def _loop_target(home_dir: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    """The live own-time process this home's status names, verified BEFORE any signal: the raw
    status file (never the folded `running`, which hides a live process behind a stale heartbeat),
    never this gateway's own pid, and the same incarnation (pid + its OS start time, the token
    the loop stamped). Returns (target, why-not). A process whose life or incarnation cannot be
    read is UNKNOWN (Codex review 2 #4): a target marked `unknown`, never taken for dead."""
    import os

    from abstractruntime.identity.life import _pid_start_time

    try:
        raw = json.loads((Path(home_dir) / "loop_status").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "no status"
    try:
        pid = int(raw.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0:
        return None, "no pid"
    if pid == os.getpid():
        return None, "the status names this gateway's own process"
    life = _pid_liveness(pid)
    if life == "dead":
        return None, "not alive"
    token = str(raw.get("pid_started_at") or "")
    current = _pid_start_time(pid)
    if life == "unknown":
        return {"pid": pid, "token": token or current, "unknown": "its process cannot be checked"}, ""
    if token:
        if not current:
            return {"pid": pid, "token": token, "unknown": "its start time cannot be read"}, ""
        if current != token:
            return None, "another process holds that pid now"
    elif str(raw.get("phase") or "") not in ("day", "between"):
        return None, "an unstamped status that does not say a loop is up"
    return {"pid": pid, "token": token or current}, ""


def _pid_liveness(pid: int) -> str:
    """"alive", "dead" (gone, or a zombie waiting to be reaped) or "unknown" (EPERM: it exists but
    is not ours to probe; any other error) — never a guess (Codex review 2 #4)."""
    import errno
    import os
    import subprocess

    try:
        os.waitpid(pid, os.WNOHANG)  # reap it if it is our own child
    except (ChildProcessError, OSError):
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "unknown"
    except OSError as e:
        return "dead" if e.errno == errno.ESRCH else "unknown"
    try:
        stat = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, timeout=5).stdout
        if stat.strip().startswith("Z"):
            return "dead"
    except Exception:  # noqa: BLE001 - alive by kill(0); the zombie check is the refinement
        pass
    return "alive"


def _pid_alive_strict(pid: int) -> bool:
    """Alive and not a zombie. Unknown counts as alive: a process is dead only when seen dead."""
    return _pid_liveness(pid) != "dead"


def _target_gone(target: Dict[str, Any]) -> bool:
    """True only when the death of THAT incarnation is confirmed."""
    from abstractruntime.identity.life import _pid_start_time

    life = _pid_liveness(int(target["pid"]))
    if life == "dead":
        return True
    if life == "unknown" or not target.get("token"):
        return False
    current = _pid_start_time(int(target["pid"]))
    return bool(current) and current != target["token"]  # recycled: the target is gone


def _kill_loop(home_dir: Path, cancelled: Dict[str, Any], reason: str = "") -> Dict[str, Any]:
    """The kill switch's own-time row (contract §3.1; review 2026-11-09 #3): serialized per home,
    the target verified before any signal, and the death CONFIRMED — the runtime's answer when it
    says (`killed`), else the gateway's own check of that exact incarnation. Never a success it did
    not see."""
    from .entity_loop import freeze_loop

    try:
        with _kill_lock(home_dir):
            target, why = _loop_target(home_dir)
            if target is None:
                return _item("loop", None, "not_running", None, None)
            pid = int(target["pid"])
            if target.get("unknown"):
                # Not signalled, not counted as stopped: an incomplete row the next Stop retries.
                return _item("loop", pid, "failed", "loop_unverified",
                             f"His own-time process could not be checked ({target['unknown']}); press Stop again.")
            res = freeze_loop(home_dir, reason=reason or "kill switch", requested_by="admin")
            cancelled["loop_pid"] = pid
            reported = res.get("killed") if isinstance(res, dict) else None
            gone = _target_gone(target)
            if reported is False or not gone:
                # The runtime's own refusal (it signals only a verified loop of this home) is said
                # plainly — never a success it did not see.
                why = str((res or {}).get("reason") or "").strip()
                sentence = "His own-time process did not stop; press Stop again." + (f" ({why})" if why else "")
                return _item("loop", pid, "failed", "loop_still_alive", sentence)
            return _item("loop", pid, "killed_sigkill" if (res or {}).get("escalated_to_sigkill") else "killed_sigterm")
    except Exception as e:  # noqa: BLE001
        return _item("loop", None, "failed", "loop_still_alive", f"His own-time process could not be stopped: {e}")


def _close_durable(registry: Any, visit_host: Any, name: str, run_id: str, was_cancelled: bool, cancelled: Dict[str, Any]) -> Dict[str, Any]:
    if not run_id or visit_host is None:
        return _item("visit", None, "not_open")
    try:
        if was_cancelled:
            visit_host.finalize_cancelled(name, run_id)
        else:
            # The cancel did not take (a store problem): the hard-freeze close is the fallback.
            with visit_host.lease_without_waiting():
                visit_host.close(name, run_id, closed_by="pause", reason=KILL_SWITCH_REASON)
        cancelled["visits"].append(run_id)
        return _item("visit", run_id, "closed")
    except Exception as e:  # noqa: BLE001
        return _item("visit", run_id, "failed", "visit_close_failed", f"His visit could not be closed: {getattr(e, 'detail', e)}")


def _stop_chat(chat_host: Any, name: str, cancelled: Dict[str, Any]) -> Dict[str, Any]:
    """The hosted chat under the kill switch (review #4): an in-flight turn is told to yield at
    once and is never waited on; the close follows on its own when the turn lets go."""
    if chat_host is None:
        return _item("chat", None, "not_open")
    try:
        out = chat_host.stop_open_visit(name)
    except Exception as e:  # noqa: BLE001
        return _item("chat", None, "failed", "chat_close_failed", f"His chat could not be closed: {e}")
    if out.get("outcome") in ("closed", "cancel_requested"):
        cancelled["chats"].append(str(out.get("chat_id")))
    return _item("chat", out.get("chat_id"), str(out.get("outcome") or "not_open"))


def resume(registry: Any, name: str, *, reason: str = "", actor: Optional[str] = None, chat_host: Any = None,
           visit_host: Any = None, via: str = "POST /entities/{name}/lifecycle") -> Dict[str, Any]:
    """Clear stopped and write awake (never a sleep): he comes back IDLE, always (v3 D3). Nothing
    from before the Stop is restored; own time comes back only when the operator gives it again,
    and a queued job then moves him to work (reconcile, never an operator act here). The operator's
    `reason` is kept, as Stop keeps its own: in the state's reason and on the wake marker."""
    from abstractruntime.identity.life import (
        personal_grant_refusal,
        read_entity_state,
        read_personal_grant,
        write_entity_state,
        write_personal_grant,
    )

    revoked_at_resume = False

    t0 = time.monotonic()
    if chat_host is None and visit_host is None:
        chat_host, visit_host = default_hosts()
    manifest = registry.manifest_for(name)
    slug = manifest.slug
    entities_dir = Path(registry.entities_dir)
    home_dir = entities_dir / slug
    actor = actor or _actor()
    reason = str(reason or "").strip()
    via = via.replace("{name}", slug)
    from abstractruntime.identity.lifecycle_fence import RESUME

    with fenced(entities_dir, slug, RESUME) as rec:
        already = str(read_entity_state(home_dir).get("state") or "") != "paused"
        if not already:
            write_entity_state(home_dir, "awake", reason=f"resumed{(': ' + reason) if reason else ''} [by {actor} via {via}]",
                               fence=RESUME)
            rec["maintenance"] = None
            rec["stop_outstanding"] = []
            # Resume lands in idle, always (v3 D3; Codex review 2 #7): nothing remembered from before
            # the Stop, and a grant armed meanwhile by any path is considered cancelled.
            _note_resume_to(rec, None)
            if personal_grant_refusal(read_personal_grant(home_dir)) is None:
                write_personal_grant(home_dir, mode="disabled", granted_by="kill_switch", fence=RESUME)
                revoked_at_resume = True
            write_record(entities_dir, slug, rec)
    started = {"own_time": False, "pid": None}
    if not already:
        # The runtime the gateway keeps open for this home acts again, at the generation now (runtime
        # 5ee8049 readmit; the Stop cancelled every run of the old one).
        readmit = getattr(registry, "readmit_cached_runtime", None)
        if callable(readmit):
            try:
                readmit(slug)
            except Exception:  # noqa: BLE001 - the next door admission readmits it
                logger.exception("resume: the open runtime of %s could not be readmitted", slug)
        _marker(registry, manifest, "wake", {"state": "awake", "prior_state": "paused", "reason": reason or "resumed", "by": actor})
        if revoked_at_resume:
            _marker(registry, manifest, "personal_grant_revoked",
                    {"mode": "disabled", "revoked_by": "kill_switch", "by": actor, "channel": "resume"})
        started = reconcile(registry, name, "resume", chat_host=chat_host, visit_host=visit_host)
    body = publish(registry, name, "resume", chat_host=chat_host, visit_host=visit_host)
    body.update({
        "action": "resume", "already": already, "took_ms": int((time.monotonic() - t0) * 1000),
        "complete": True, "items": [], "cancelled": {"visits": [], "chats": [], "runs": [], "loop_pid": None},
        "loop_signal": None, "started": {"own_time": bool(started.get("own_time")), "pid": started.get("pid")},
    })
    return body


# --------------------------------------------------------------------------- toggles
def set_toggles(
    registry: Any, name: str, *, personal: Optional[bool],
    actor: Optional[str] = None, chat_host: Any = None, visit_host: Any = None,
) -> Dict[str, Any]:
    """Write the one permission, own time (contract §2; v3 D1: there is no work switch). Raises
    ToggleRefused while stopped or archived."""
    from abstractruntime.identity.life import read_entity_state, write_personal_grant

    from .entity_loop import loop_status, stop_loop

    if chat_host is None and visit_host is None:
        chat_host, visit_host = default_hosts()
    manifest = registry.manifest_for(name)
    slug = manifest.slug
    entities_dir = Path(registry.entities_dir)
    home_dir = entities_dir / slug
    actor = actor or _actor()
    changed: Dict[str, Any] = {"personal": None}
    with fenced_here_or_none(entities_dir, slug) as rec:
        if rec is None:
            raise ToggleRefused("stopped_use_resume")  # an unknown generation admits nothing
        if str(read_entity_state(home_dir).get("state") or "") == "paused":
            raise ToggleRefused("stopped_use_resume")
        facts = gather(registry, name, chat_host=chat_host, visit_host=visit_host)
        if facts["archived"]:
            raise ToggleRefused("entity_archived")
        if personal is not None and bool(facts["personal"]) != bool(personal):
            changed["personal"] = {"from": bool(facts["personal"]), "to": bool(personal)}
            write_personal_grant(home_dir, mode=("until_revoked" if personal else "disabled"), granted_by=actor,
                                 fence=rec.fence)
    if changed["personal"] is not None:
        _marker(registry, manifest, "personal_granted" if personal else "personal_grant_revoked",
                {"channel": "toggle", "by": actor, "mode": "until_revoked" if personal else "disabled"})
        if not personal and bool(loop_status(home_dir).get("running")):
            # Own time ends at the loop's next boundary (the current step finishes). He is never
            # put to sleep by the toggle: reconcile repairs the gate's leftover sleep after exit.
            stop_loop(home_dir, reason=f"personal turned off by {actor}", requested_by="operator")
    if changed["personal"] is not None and personal:
        # The operator's own act names own time as the phase to return to. Turning it OFF keeps the
        # remembered phase until the interruption ends (the read says "Own time is off"); the
        # restore then finds the grant off, stays idle and retires it.
        set_resume_to(registry, slug, "personal")
    if changed["personal"] is not None:
        reconcile(registry, name, "toggle", chat_host=chat_host, visit_host=visit_host,
                  operator=bool(personal))
    body = publish(registry, name, "toggle", chat_host=chat_host, visit_host=visit_host)
    body["changed"] = changed
    return body


class ToggleRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --------------------------------------------------------------------------- reconcile
def reconcile(registry: Any, name: str, cause: str, *, chat_host: Any = None, visit_host: Any = None,
              operator: bool = False) -> Dict[str, Any]:
    """Restore own time after an interruption (contract §4; operator 2026-11-09). Idempotent;
    returns {own_time, pid, reason}.

    The target is `resume_to` (the phase the interruption took over), restored only while its
    grant is still on. `operator=True` is the operator's own act (his toggle turning personal on,
    his start, his retry): the target is then personal whenever the grant is on. Nothing else ever
    starts own time from idle, and a crashed loop is never restarted without the operator.

    JOBS (contract v3 D1/D4): a queued loop job (a standing work order, a task left with him) starts
    the loop for WORK whatever the personal grant says — there is no work switch; the loop works,
    then returns to `resume_to` (personal only while granted) or ends idle."""
    if chat_host is None and visit_host is None:
        chat_host, visit_host = default_hosts()
    manifest = registry.manifest_for(name)
    slug = manifest.slug
    entities_dir = Path(registry.entities_dir)
    try:
        decision = _reconcile_decide(registry, name, slug, cause, chat_host=chat_host, visit_host=visit_host,
                                     operator=operator)
    except GenerationMoved:
        return {"own_time": False, "pid": None, "reason": "lifecycle_unreadable"}
    if "params" not in decision:
        return decision
    key = str(entities_dir / slug)
    try:
        return _reconcile_spawn(registry, manifest, cause, decision)
    finally:
        with _STARTING_GUARD:
            _STARTING.discard(key)


def _reconcile_decide(registry: Any, name: str, slug: str, cause: str, *, chat_host: Any, visit_host: Any,
                      operator: bool) -> Dict[str, Any]:
    """reconcile's decision, ONE locked step fenced at the generation read there. Returns its
    answer, or the spawn it admitted ({params, generation, admission, return_to}); an admitted
    spawn marks the home starting in this process until the runtime's reservation takes over."""
    from abstractruntime.identity.life import write_entity_state

    entities_dir = Path(registry.entities_dir)
    home_dir = entities_dir / slug
    with fenced_here(entities_dir, slug) as rec:
        generation = int(rec.get("generation") or 0)
        facts = gather(registry, name, chat_host=chat_host, visit_host=visit_host)
        state = facts["state"]
        word = str(state.get("state") or "awake")
        if word == "paused":
            return {"own_time": False, "pid": None, "reason": "stopped"}
        loop_alive = bool(facts["loop"].get("running"))
        # The 1178 repair: a sleep the old loop gate wrote when own time ended is not a sleep.
        if (
            word == "asleep"
            and str(state.get("mode") or "") != "visiting"
            and not loop_alive
            and _sleep_kind(state, loop_alive) is None
        ):
            write_entity_state(home_dir, "awake", reason="his own time ended; awake and reachable", fence=rec.fence)
            facts = gather(registry, name, chat_host=chat_host, visit_host=visit_host)
        if facts["archived"]:
            return {"own_time": False, "pid": None, "reason": "archived"}
        if running_sleep(facts):
            return {"own_time": False, "pid": None, "reason": "asleep"}
        if visit_live(facts):
            return {"own_time": False, "pid": None, "reason": "visit"}
        if work_live(facts):
            return {"own_time": False, "pid": None, "reason": "work"}
        if loop_alive:
            return {"own_time": False, "pid": None, "reason": "running"}
        jobs = int(facts["jobs_queued"]) > 0
        target = "personal" if operator else rec.get("resume_to")
        if jobs and cause == "own_time_ended" and not operator:
            # The loop ended on its own word with jobs still queued: never a respawn loop. The next
            # trigger (a job queued, an interruption ending, the operator) starts it again.
            return {"own_time": False, "pid": None, "reason": "jobs_left_by_loop"}
        if jobs:
            target = "work"  # a queued job comes first; own time waits for it (v3 D4)
        elif target == "work":
            # A v2 "work" return (no queue left behind it) is not own time: it is never turned into
            # personal because a grant stands (Codex review 2 #6). Retired.
            target = None
            if _note_resume_to(rec, None):
                write_record(entities_dir, slug, rec)
        if target not in ("personal", "work"):
            if rec.get("restart_resume") is not None:
                rec["restart_resume"] = None
                write_record(entities_dir, slug, rec)
            return {"own_time": False, "pid": None, "reason": "idle"}  # nothing was interrupted
        if target == "personal" and not facts["personal"]:
            # The grant was revoked meanwhile: stay idle, and the remembered phase is retired with
            # the interruption (and the restart's restore with it).
            if not operator and (_note_resume_to(rec, None) | (rec.get("restart_resume") is not None)):
                rec["restart_resume"] = None
                write_record(entities_dir, slug, rec)
            return {"own_time": False, "pid": None, "reason": "personal_off"}
        loop_facts = facts["loop"]
        crashed = "process is gone" in str(loop_facts.get("note") or "") or str(loop_facts.get("stopped_by") or "") == "failures"
        if crashed and not operator and not _restart_pending(rec, generation):
            return {"own_time": False, "pid": None, "reason": "loop_crashed"}
        # ONE START AT A TIME (STOP-LANE-DESIGN §2.4): the runtime's spawn reservation (loop_status
        # `starting`) and this process's own decision-to-reservation window, never a claim with a clock.
        if own_time_starting(entities_dir, slug, loop_facts):
            return {"own_time": False, "pid": None, "reason": "spawning"}
        admission = int(rec.get("admission_seq") or 0)
        # A loop started for queued jobs is told nothing (runtime v3: its gate works the queue, then
        # returns to the remembered phase or ends idle); the operator's act names personal.
        spawn_return_to = None if target == "work" else ("personal" if operator else target)
        if rec.get("restart_resume") is not None:
            rec["restart_resume"] = None  # the restart's restore is being attempted: spent
            write_record(entities_dir, slug, rec)
        key = str(entities_dir / slug)
        with _STARTING_GUARD:
            if key in _STARTING:
                return {"own_time": False, "pid": None, "reason": "spawning"}
            _STARTING.add(key)  # reconcile discards it once the spawn returned (its finally)
        return {"params": dict(rec.get("own_time_params") or {}), "generation": generation,
                "admission": admission, "return_to": spawn_return_to}


def _reconcile_spawn(registry: Any, manifest: Any, cause: str, decision: Dict[str, Any]) -> Dict[str, Any]:
    """The admitted spawn, outside the lock (a spawn is never under it). The runtime refuses a
    second start ("already starting" / "already in progress": another start is under way, said as
    `spawning`) and a start a Stop beat (StoppedByOperator: `stopped`)."""
    from abstractruntime.identity.life import LoopAlreadyRunning
    from abstractruntime.identity.lifecycle_fence import StoppedByOperator

    slug = manifest.slug
    entities_dir = Path(registry.entities_dir)
    home_dir = entities_dir / slug
    generation = int(decision["generation"])
    admission = int(decision["admission"])
    pid = None
    error: Optional[str] = None
    try:
        # The child is told which own-time phase it returns to: personal for the operator's own act,
        # the remembered phase for a restore (runtime seam `return_to`).
        started = start_own_time(registry, manifest, decision["params"], return_to=decision["return_to"])
        pid = started.get("pid")
    except StoppedByOperator:
        return {"own_time": False, "pid": None, "reason": "stopped"}
    except LoopAlreadyRunning:
        return {"own_time": False, "pid": None, "reason": "running"}
    except Exception as e:  # noqa: BLE001 - reported as blocked, never raised at the trigger
        text = str(getattr(e, "detail", "") or e) or e.__class__.__name__
        if "already starting" in text or "already in progress" in text:
            return {"own_time": False, "pid": None, "reason": "spawning"}
        if "already running" in text:
            return {"own_time": False, "pid": None, "reason": "running"}
        error = text

    with fenced_here(entities_dir, slug) as rec:
        rec["start_error"] = error
        moved = int(rec.get("generation") or 0) != generation
        # A visit took the phase while the loop was being spawned (review #9): the visit wins.
        visited = int(rec.get("admission_seq") or 0) != admission
        write_record(entities_dir, slug, rec)
    if visited and pid and not moved:
        # A visit took the phase while the loop was being spawned (review #9): the visit wins — and
        # this is NOT a Stop (Codex review 2 #8). The new loop's own gate holds at the visiting
        # posture (a held state goes to the state gate before any leg, runtime 101ba5f), so it runs
        # nothing under the visit and its way back is kept: no hard stop, no revoke, no cleared
        # return. It resumes when the visit closes.
        _marker(registry, manifest, "personal_started", {"channel": "reconcile", "cause": cause, "pid": pid,
                                                         "held_by": "visit"})
        return {"own_time": True, "pid": pid, "reason": "held_by_visit"}
    if moved and pid:
        # A Stop landed while the loop was being spawned: the new loop never outlives the fence
        # (through the same verified, serialized hard stop as the kill switch).
        row = _kill_loop(home_dir, {}, "kill switch landed during the start")
        if row.get("outcome") == "failed":
            logger.error("a loop spawned across a Stop for %s could not be stopped: %s", slug, row.get("sentence"))
        return {"own_time": False, "pid": None, "reason": "stopped"}
    if error:
        return {"own_time": False, "pid": None, "reason": "start_failed", "error": error}
    _marker(registry, manifest, "personal_started", {"channel": "reconcile", "cause": cause, "pid": pid})
    return {"own_time": True, "pid": pid, "reason": cause}


def _restart_pending(rec: Dict[str, Any], generation: int) -> bool:
    flag = rec.get("restart_resume")
    return isinstance(flag, dict) and int(flag.get("generation") or 0) == int(generation)


# --------------------------------------------------------------------------- gateway start
_RESTART_PASSES: set = set()
_RESTART_GUARD = threading.Lock()


def resume_after_restart(registry: Any, *, chat_host: Any = None, visit_host: Any = None) -> List[Dict[str, Any]]:
    """A gateway restart is not a Stop (operator decision D5, 2026-11-09). Once per entities
    directory per process, at the gateway's start: a home whose personal grant is still armed and
    whose own-time loop is not running gets its own time back through reconcile (the phase to
    return to is own time, and the loop the restart took down is not refused as a crash). Never for
    a stopped home (the kill switch's word stands), never for an archived one, and never from idle
    without a grant: a home with no armed grant is left exactly as it is. A visit, a sleep or work
    standing at the start keeps the phase; own time resumes when it ends. Returns one row per home
    looked at (for the log and tests); never raises."""
    from abstractruntime.identity.life import personal_grant_refusal, read_entity_state, read_personal_grant

    from .entity_loop import loop_status

    entities_dir = Path(registry.entities_dir)
    key = str(entities_dir.resolve())
    with _RESTART_GUARD:
        if key in _RESTART_PASSES:
            return []
        _RESTART_PASSES.add(key)
    rows: List[Dict[str, Any]] = []
    try:
        listed = registry.list_entities()
    except Exception as e:  # noqa: BLE001
        logger.warning("restart resume could not list entities: %s", e)
        return rows
    for row in listed:
        slug = str(row.get("slug") or "")
        if not slug or row.get("error"):
            continue
        home_dir = entities_dir / slug
        try:
            with fenced_here(entities_dir, slug) as rec:
                if str(read_entity_state(home_dir).get("state") or "") == "paused":
                    rows.append({"slug": slug, "action": "skipped", "reason": "stopped"})
                    continue
                if personal_grant_refusal(read_personal_grant(home_dir)) is not None:
                    rows.append({"slug": slug, "action": "skipped", "reason": "no_grant"})
                    continue
                if bool(loop_status(home_dir).get("running")):
                    rows.append({"slug": slug, "action": "skipped", "reason": "running"})
                    continue
                from .entity_access import entity_archived, users_path_of

                if entity_archived(slug, users_path=users_path_of(registry)):
                    rows.append({"slug": slug, "action": "skipped", "reason": "archived"})
                    continue
                rec["restart_resume"] = {"generation": int(rec.get("generation") or 0), "at": _now_iso()}
                _note_resume_to(rec, "personal")
                write_record(entities_dir, slug, rec)
            out = reconcile(registry, slug, "gateway_restart", chat_host=chat_host, visit_host=visit_host)
            rows.append({"slug": slug, "action": "resumed" if out.get("own_time") else "deferred", **out})
        except Exception as e:  # noqa: BLE001 - one broken home never blocks the others
            logger.warning("restart resume failed for %s: %s", slug, e)
            rows.append({"slug": slug, "action": "error", "error": f"{type(e).__name__}: {e}"})
            continue
        safe_publish(registry, slug, "gateway_restart", chat_host=chat_host, visit_host=visit_host)
    for r in rows:
        if r.get("action") in ("resumed", "deferred", "error"):
            logger.info("restart resume: %s", r)
    return rows


def start_restart_resume(registry: Any, *, chat_host: Any = None, visit_host: Any = None) -> Optional[threading.Thread]:
    """The start pass in a daemon thread (service-factory wiring; it never delays serving). Off
    with the background entity sweeps (ABSTRACTGATEWAY_ENTITY_SELF_REPAIR=0, the tests' setting)."""
    from .entity_repair import background_sweeps_enabled

    if not background_sweeps_enabled():
        return None
    thread = threading.Thread(
        target=lambda: resume_after_restart(registry, chat_host=chat_host, visit_host=visit_host),
        name="entity-restart-resume", daemon=True,
    )
    thread.start()
    return thread


def start_own_time(registry: Any, manifest: Any, params: Dict[str, Any], return_to: Optional[str] = None) -> Dict[str, Any]:
    """Spawn the own-time loop with the remembered `loop/start` parameters, else the entity's own
    mind and the defaults (one mind per entity)."""
    from .entity_chat import DEFAULT_ENTITY_CHAT_CONTEXT_WINDOW, entity_base_url, resolve_entity_mind
    from .entity_loop import start_loop

    home_dir = Path(registry.entities_dir) / manifest.slug
    mind = resolve_entity_mind(params.get("provider"), params.get("model"), home_dir=home_dir)
    base_url = entity_base_url(mind["provider"], params.get("base_url"), mind_base_url=mind["base_url"])
    return start_loop(
        home_dir,
        provider=mind["provider"],
        model=mind["model"],
        base_url=base_url,
        tick_seconds=float(params.get("tick_seconds") or 20.0),
        ticks_per_day=int(params.get("ticks_per_day") or 8),
        rest_minutes=float(params.get("rest_minutes") if params.get("rest_minutes") is not None else 30.0),
        shelf_size=(int(params["shelf_size"]) if params.get("shelf_size") is not None else None),
        context_window=int(params.get("context_window") or DEFAULT_ENTITY_CHAT_CONTEXT_WINDOW),
        return_to=return_to,
    )


def remember_own_time_params(registry: Any, slug: str, params: Dict[str, Any]) -> None:
    entities_dir = Path(registry.entities_dir)
    with fenced_here(entities_dir, slug) as rec:
        rec["own_time_params"] = {k: v for k, v in params.items() if v is not None}
        write_record(entities_dir, slug, rec)


def retry(registry: Any, name: str, *, chat_host: Any = None, visit_host: Any = None) -> Dict[str, Any]:
    t0 = time.monotonic()
    manifest = registry.manifest_for(name)
    entities_dir = Path(registry.entities_dir)
    with fenced_here(entities_dir, manifest.slug) as rec:
        rec["start_error"] = None
        write_record(entities_dir, manifest.slug, rec)
    started = reconcile(registry, name, "retry", chat_host=chat_host, visit_host=visit_host, operator=True)
    body = publish(registry, name, "retry", chat_host=chat_host, visit_host=visit_host)
    body.update({
        "action": "retry", "already": False, "took_ms": int((time.monotonic() - t0) * 1000),
        "complete": True, "items": [], "cancelled": {"visits": [], "chats": [], "runs": [], "loop_pid": None},
        "loop_signal": None, "started": {"own_time": bool(started.get("own_time")), "pid": started.get("pid")},
    })
    return body


# --------------------------------------------------------------------------- visit close / maintenance
def after_visit_closed(registry: Any, slug: str, prior: Optional[Dict[str, Any]], *, cause: str = "visit_closed",
                       chat_host: Any = None, visit_host: Any = None) -> None:
    """A visit's close returns to the previous phase where the toggles allow it (contract §4.4).

    The hosts have already restored the pre-visit state by ownership (81a92233's rule). Here: an
    interrupted operator sleep whose maintenance finished while the visit waited wakes now (it does
    not leave a finished sleep standing); everything else is reconcile from the toggles."""
    from abstractruntime.identity.life import read_entity_state, write_entity_state

    entities_dir = Path(registry.entities_dir)
    home_dir = entities_dir / slug
    try:
        with fenced_here(entities_dir, slug) as rec:
            maint = rec.get("maintenance") if isinstance(rec.get("maintenance"), dict) else None
            st = read_entity_state(home_dir)
            if (
                maint
                and maint.get("status") == "done_pending_wake"
                and maint.get("generation") is not None
                and int(maint["generation"]) == int(rec.get("generation") or 0)
                and str(st.get("state") or "") == "asleep"
                and str(st.get("mode") or "") != "visiting"
            ):
                write_entity_state(home_dir, "awake", reason="maintenance done; awake", fence=rec.fence)
                rec["maintenance"] = None
                write_record(entities_dir, slug, rec)
            # A sleep the visit interrupted was written back by the close with its original since
            # (`continue_sleep`).
        reconcile(registry, slug, cause, chat_host=chat_host, visit_host=visit_host)
    except Exception:  # noqa: BLE001 - the close itself already succeeded
        logger.exception("after-visit reconcile failed for %s", slug)
    safe_publish(registry, slug, cause, chat_host=chat_host, visit_host=visit_host)


def maintenance_started(registry: Any, slug: str, generation: int) -> int:
    """Record a running maintenance pass admitted at `generation` (the sleep's own admission, never
    a generation read after it); returns it. GenerationMoved when a Stop landed since (nothing
    recorded: the pass does not run)."""
    entities_dir = Path(registry.entities_dir)
    with fenced(entities_dir, slug, int(generation)) as rec:
        gen = int(generation)
        rec["maintenance"] = {"status": "running", "generation": gen, "started_at": _now_iso()}
        write_record(entities_dir, slug, rec)
        return gen


def maintenance_write_if_owner(registry: Any, slug: str, generation: int, write: Callable[[Any], Any]) -> bool:
    """The maintenance pass's own state writes, checked and written in ONE locked step (review #1):
    only while no Stop moved the generation and no visit took the posture. `write(fence)` is handed
    the pass's fence for its runtime state write."""
    entities_dir = Path(registry.entities_dir)
    try:
        with fenced(entities_dir, slug, int(generation)) as rec:
            if not maintenance_may_write(registry, slug, generation):
                return False
            write(rec.fence)
            return True
    except GenerationMoved:
        return False


def maintenance_may_write(registry: Any, slug: str, generation: int) -> bool:
    """The pass's `finally` may restore its own sleep only when no Stop landed and no visit took
    the posture (contract §3.3, §4.3)."""
    from abstractruntime.identity.life import read_entity_state

    entities_dir = Path(registry.entities_dir)
    rec = read_record(entities_dir, slug)
    if rec.get("generation") is None or int(rec.get("generation") or 0) != int(generation):
        return False  # moved, or unreadable (fail closed)
    st = read_entity_state(entities_dir / slug)
    return str(st.get("state") or "") == "asleep" and str(st.get("mode") or "") != "visiting"


def maintenance_done(registry: Any, name: str, generation: int, *, failed: Optional[str] = None,
                     superseded: bool = False, cancelled_after: Optional[str] = None,
                     chat_host: Any = None, visit_host: Any = None) -> str:
    """The operator sleep's pass returned: wake + reconcile, unless a Stop moved the generation
    (nothing written) or a visit holds the posture (the wake runs at the visit's close).
    Returns the outcome word: woke | stopped | after_visit | not_ours.

    `cancelled_after` (memory's sleep_pass: the night ended early, after that stage): the pass did
    not run to its end. It yielded to a visit (`yielded`) or stopped on its hard check, e.g. an
    unreadable lifecycle generation (`cancelled`). Its receipt is `last_pass`, never
    `last_maintenance`: only a pass that ran to its end is the home's last maintenance, the
    boundary the idle sleep counts from (24 h / 500 memories)."""
    from abstractruntime.identity.life import read_entity_state, write_entity_state

    manifest = registry.manifest_for(name)
    slug = manifest.slug
    entities_dir = Path(registry.entities_dir)
    home_dir = entities_dir / slug
    with fenced_here_or_none(entities_dir, slug) as rec:
        if rec is None:
            return "stopped"  # an unreadable generation: nothing recorded, nothing woken (fail closed)
        mine = isinstance(rec.get("maintenance"), dict) and rec["maintenance"].get("generation") == int(generation)
        # The receipt of THIS pass (review #8): a failure or a superseded pass is always recorded.
        stopped_by_switch = int(rec.get("generation") or 0) != int(generation)
        ended_early = bool(cancelled_after) and not stopped_by_switch and not superseded
        if ended_early:
            st_now = read_entity_state(home_dir)
            yielded = str(st_now.get("state") or "") == "asleep" and str(st_now.get("mode") or "") == "visiting"
            rec["last_pass"] = {"generation": int(generation), "at": _now_iso(),
                                "status": "yielded" if yielded else "cancelled",
                                "cancelled_after": str(cancelled_after), "error": failed}
        else:
            rec["last_maintenance"] = {"generation": int(generation), "at": _now_iso(),
                                       "status": ("cancelled" if stopped_by_switch else
                                                  "superseded" if superseded else ("failed" if failed else "done")),
                                       "error": failed}
        if stopped_by_switch:
            # A clean terminal record: the pass ended under the kill switch, by the operator.
            rec["last_maintenance"].update({"reason": "kill_switch", "by": "operator"})
        if int(rec.get("generation") or 0) != int(generation) or superseded:
            if mine:
                rec["maintenance"] = None  # a stale completion never touches a newer pass's record
            write_record(entities_dir, slug, rec)
            return "stopped" if int(rec.get("generation") or 0) != int(generation) else "superseded"
        st = read_entity_state(home_dir)
        if str(st.get("state") or "") == "asleep" and str(st.get("mode") or "") == "visiting":
            rec["maintenance"] = {"status": "done_pending_wake", "generation": generation, "failed": failed}
            write_record(entities_dir, slug, rec)
            return "after_visit"
        if mine:
            rec["maintenance"] = None
        write_record(entities_dir, slug, rec)
        if str(st.get("state") or "") != "asleep" or str(st.get("written_by") or "operator") not in ("operator", IDLE_SWEEP_WRITER):
            return "not_ours"  # a summon or a wake already ended this sleep
        reason = "maintenance done; awake" + (f" (the pass failed: {failed})" if failed else "")
        write_entity_state(home_dir, "awake", reason=reason, fence=rec.fence)
    # The wake is a moment of his life like any other (the sleep that ends has its own marker).
    _marker(registry, manifest, "wake", {"state": "awake", "prior_state": "asleep", "reason": "maintenance done",
                                         **({"failed": failed} if failed else {})})
    reconcile(registry, name, "maintenance_done", chat_host=chat_host, visit_host=visit_host)
    return "woke"


def note_run_terminal(run: Any) -> None:
    """Runner hook: a summon (work) run of an entity ended — reconcile that entity."""
    try:
        stamp = ((getattr(run, "vars", None) or {}).get("_runtime") or {}).get("entity") or {}
        entity_id = str(stamp.get("entity_id") or "")
        if not entity_id.startswith("entity:") or str(stamp.get("channel") or "") == "":
            return
        slug = entity_id[len("entity:"):].split("@", 1)[0]
        if not slug:
            return
        svc = _service()
        registry = getattr(svc, "entity_registry", None)
        if registry is None:
            return
        threading.Thread(
            target=lambda: (reconcile(registry, slug, "summon_ended"), safe_publish(registry, slug, "summon_ended")),
            name=f"entity-reconcile-{slug}", daemon=True,
        ).start()
    except Exception:  # noqa: BLE001 - a hook never breaks the runner
        logger.exception("entity reconcile hook failed")
