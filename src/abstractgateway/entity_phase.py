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
- `reconcile` starts own time when `personal` is on and nothing else holds the phase; the gateway
  owns that restart (after a visit or chat closes, a summon ends, a toggle, a resume, the end of an
  operator sleep's maintenance). There is no automatic respawn after a crash: the read shows
  `blocked {loop_crashed}` with a `retry` action.

The sidecar `<entities_dir>/.life_phase/<slug>.json` is gateway bookkeeping, outside the home (the
`.host_stream` / `.live_summons` class). It is written atomically under a per-home flock, and the
flock is held only for read-check-write, never across a model call, a spawn or a teardown.
"""

from __future__ import annotations

import contextlib
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
# Sleeps the old loop gate wrote when it found nothing to do (root 1178): until the runtime batch
# they are not sleep at all — the entity is awake and reachable (contract §1.2).
GATE_SLEEP_WRITERS = ("grant-gate", "day-gate", "need-check")
MAINTENANCE_ENDS = "when maintenance is done"
# A spawn claim older than this is a crashed claimant; a new reconcile may spawn.
SPAWN_CLAIM_TTL_S = 30.0
# loop_status.stopped_by values that mean "the loop ended because it was asked to".
REQUESTED_STOPS = ("stop_command", "stop_file", "operator", "personal_disarmed", "personal_off", "kill_switch", "settled_desk")

_DEFAULT_RECORD: Dict[str, Any] = {
    "rev": 0,
    "published_rev": 0,
    "generation": 0,
    "work": True,
    "snapshot": None,
    "since": None,
    "last_activity_phase": None,
    "own_time_params": None,
    "start_error": None,
    "spawning": None,
    "stop_outstanding": [],
    "maintenance": None,
    "sleep_since_override": None,
}


class GenerationMoved(Exception):
    """A writer's captured generation is stale: a Stop landed after it began."""


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
    return rec


def write_record(entities_dir: Path, slug: str, rec: Dict[str, Any]) -> None:
    path = sidecar_path(entities_dir, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp-{secrets.token_hex(4)}")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def generation_of(entities_dir: Path, slug: str) -> int:
    return int(read_record(entities_dir, slug).get("generation") or 0)


@contextlib.contextmanager
def fenced(entities_dir: Path, slug: str, generation: int) -> Iterator[Dict[str, Any]]:
    """Hold the phase lock and raise GenerationMoved when a Stop landed since `generation` was
    captured. Every lifecycle writer wraps its state write in this (contract §3.3)."""
    with phase_lock(entities_dir, slug):
        rec = read_record(entities_dir, slug)
        if int(rec.get("generation") or 0) != int(generation):
            raise GenerationMoved(f"generation moved ({generation} -> {rec.get('generation')})")
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
    toggles = {"work": bool(rec.get("work", True)), "personal": bool(facts["personal"])}
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
    elif work_live(facts):
        phase = "work"
        if seat is not None and not is_human_seat(seat):
            kind = "automation" if seat.get("automation") else "summon"
            work = {"kind": kind, "run_id": seat.get("run_id"), "since": _iso(seat.get("held_since"))}
        else:
            work = {"kind": "work_order", "run_id": None, "since": None}
        work["finishing"] = not toggles["work"]
    elif _sleep_kind(state, loop_alive):
        phase = "sleep"
        kind = _sleep_kind(state, loop_alive)
        if mode == "dreaming" or maintenance_running:
            ends: Optional[str] = MAINTENANCE_ENDS
        elif state.get("wake_at"):
            ends = _iso(state.get("wake_at"))
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

    blocked = _blocked(facts, rec, phase, toggles)
    snap: Dict[str, Any] = {
        "entity_id": manifest.entity_id,
        "name": manifest.slug,
        "phase": phase,
        "stopped": stopped,
        "toggles": toggles,
        "personal_expires_at": facts["personal_expires_at"],
        "sentence": _sentence(name, phase, visit, work, sleep, loop, blocked),
        "next": _next(phase, toggles, sleep, work),
        "blocked": blocked,
        "can": _can(facts, rec, phase, stopped),
        "visit": visit,
        "work": work,
        "sleep": sleep,
        # The existing life_state fields, unchanged in meaning (posture gains "idle").
        "posture": _posture(phase, state, loop, chat, durable),
        "chat_open": bool(chat.get("open")),
        "chat_id": chat.get("chat_id"),
        "state": state.get("state"),
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
    if not toggles["work"] and facts["work_order"]:
        return {
            "code": "work_order_waits_for_runtime",
            "sentence": "Work is off and a work order stands; his own time waits until Work is back on.",
            "action": None,
        }
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
        kind = (work or {}).get("kind")
        base = {"work_order": "Working on his work order", "automation": "Working on an automation"}.get(
            str(kind), "Working on a request from an agent"
        )
        if (work or {}).get("finishing"):
            return base + "; Work is off, so he stops after this run."
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


def _next(phase: str, toggles: Dict[str, bool], sleep: Any, work: Any) -> Optional[Dict[str, Any]]:
    after = "personal" if toggles["personal"] else "idle"
    if phase == "visit":
        return {"phase": None, "sentence": "When the visit closes, he returns to what he was doing."}
    if phase == "work":
        if after == "personal":
            return {"phase": "personal", "sentence": "When this work ends, his own time resumes."}
        return {"phase": "idle", "sentence": "When this work ends, he stays awake and reachable."}
    if phase == "sleep" and (sleep or {}).get("ends"):
        if after == "personal":
            return {"phase": "personal", "sentence": "When maintenance is done he wakes and his own time resumes."}
        return {"phase": "idle", "sentence": "When maintenance is done he wakes, awake and reachable."}
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
            "toggle_work": no("Stopped. Resume him first, then choose what he may do."),
            "toggle_personal": no("Stopped. Resume him first, then choose what he may do."),
        }
    if facts["archived"]:
        return {"visit": no(archived), "sleep_now": no(archived), "toggle_work": no(archived), "toggle_personal": no(archived)}
    if phase == "visit":
        sleep_now = no("He is in a visit; end it to let him sleep.")
    elif phase == "sleep" or (phase == "idle" and rec.get("last_activity_phase") == "sleep"):
        sleep_now = no("He is already resting; a sleep cannot follow a sleep.")
    else:
        sleep_now = yes
    return {"visit": dict(yes), "sleep_now": sleep_now, "toggle_work": dict(yes), "toggle_personal": dict(yes)}


def _first_since(phase: str, snap: Dict[str, Any], facts: Dict[str, Any]) -> Optional[str]:
    """`since` on a home's first derive (no recorded transition yet): the source fact."""
    sub = snap.get(phase) if phase in ("visit", "work", "sleep") else None
    if isinstance(sub, dict) and sub.get("since"):
        return sub["since"]
    return _iso(facts["state"].get("changed_at")) or _iso(facts["loop"].get("updated_at"))


# --------------------------------------------------------------------------- publish
_SEMANTIC_EXCLUDE = ("warnings",)


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
    with phase_lock(entities_dir, slug):
        rec = read_record(entities_dir, slug)
        facts = gather(registry, name, chat_host=chat_host, visit_host=visit_host)
        snap = derive(facts, rec)
        prev = rec.get("snapshot") if isinstance(rec.get("snapshot"), dict) else None
        prev_phase = (prev or {}).get("phase")
        if prev is None:
            since = _first_since(snap["phase"], snap, facts)
        elif prev_phase != snap["phase"]:
            since = _now_iso()
        else:
            since = rec.get("since")
        semantic = {k: v for k, v in snap.items() if k not in _SEMANTIC_EXCLUDE}
        semantic["since"] = since
        semantic["generation"] = int(rec.get("generation") or 0)
        if prev != semantic:
            rec["rev"] = int(rec.get("rev") or 0) + 1
            rec["snapshot"] = semantic
            rec["since"] = since
            if snap["phase"] in ACTIVITY_PHASES:
                rec["last_activity_phase"] = snap["phase"]
            rec["last_cause"] = str(cause or "observed")
            rec["last_from"] = prev_phase
            write_record(entities_dir, slug, rec)
            # A loop that ended because it was asked to (graceful stop, personal off) hands the
            # phase back to the gateway: reconcile repairs a gate sleep and restarts own time when
            # personal is still on. A crash never does (no auto-respawn in v1).
            if prev and prev.get("own_time_running") and not snap["own_time_running"] and snap["phase"] != "stopped":
                if str(facts["loop"].get("stopped_by") or "") in REQUESTED_STOPS and "process is gone" not in str(
                    facts["loop"].get("note") or ""
                ):
                    follow_up = "own_time_ended"
        if int(rec.get("rev") or 0) > int(rec.get("published_rev") or 0):
            _announce(registry, manifest, rec)
        _arm_expiry_timer(registry, name, facts.get("personal_expires_at"))
        body = dict(rec["snapshot"])
        body["rev"] = int(rec["rev"])
        body["warnings"] = list(facts["warnings"])
    if follow_up:
        try:
            reconcile(registry, name, follow_up, chat_host=chat_host, visit_host=visit_host)
            return publish(registry, name, follow_up, chat_host=chat_host, visit_host=visit_host)
        except Exception:  # noqa: BLE001 - the read stays served
            logger.exception("reconcile after own time ended failed for %s", slug)
    return body


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


def _marker(registry: Any, manifest: Any, kind: str, details: Dict[str, Any]) -> None:
    from .entity_replay import record_host_marker

    try:
        home = registry.get_home(manifest.slug)
        record_host_marker(
            entities_dir=Path(registry.entities_dir), slug=manifest.slug, entity_id=manifest.entity_id,
            kind=kind, journal_seq=int(home.memory.current_seq()), details=details,
        )
    except Exception:  # noqa: BLE001 - markers are observability
        logger.exception("%s marker for %s failed", kind, manifest.slug)


def _item(item: str, ident: Any, outcome: str, code: Optional[str] = None, sentence: Optional[str] = None) -> Dict[str, Any]:
    return {"item": item, "id": (str(ident) if ident not in (None, "") else None), "outcome": outcome, "code": code, "sentence": sentence}


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
    """The kill switch (contract §3.1): fence, signal, tear down, report per item."""
    from abstractruntime.identity.life import read_entity_state, request_loop_stop, write_entity_state

    t0 = time.monotonic()
    if chat_host is None and visit_host is None:
        chat_host, visit_host = default_hosts()
    manifest = registry.manifest_for(name)
    slug = manifest.slug
    entities_dir = Path(registry.entities_dir)
    home_dir = entities_dir / slug
    actor = actor or _actor()
    via = via.replace("{name}", slug)

    # 1. FENCE: generation first, then the paused write (every door refuses from here on).
    with phase_lock(entities_dir, slug):
        rec = read_record(entities_dir, slug)
        already = str(read_entity_state(home_dir).get("state") or "") == "paused"
        rec["generation"] = int(rec.get("generation") or 0) + 1
        maint = rec.get("maintenance") if isinstance(rec.get("maintenance"), dict) else None
        if maint and maint.get("status") == "running":
            maint["status"] = "cancel_requested"
        write_record(entities_dir, slug, rec)
        if not already:
            stamp = f"[by {actor} via {via}]"
            write_entity_state(home_dir, "paused", reason=f"stopped{(': ' + reason) if reason else ''} {stamp}")
    if not already:
        _marker(registry, manifest, "pause", {"state": "paused", "reason": reason or "stopped", "by": actor, "channel": "kill_switch"})

    items: List[Dict[str, Any]] = []
    cancelled = {"visits": [], "chats": [], "runs": [], "loop_pid": None}

    # 2. SIGNAL at once: the loop's durable stop command, live runs, queued admissions.
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
        svc = svc if svc is not None else _service()
        outcomes = _cancel_tree(getattr(getattr(svc, "host", None), "runtime", None),
                                getattr(getattr(svc, "host", None), "run_store", None), seat_run)
        for o in outcomes:
            items.append(_item("run", o["run_id"], o["outcome"], o.get("code"), o.get("error")))
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
        try:
            in_flight = bool(visit_host.is_in_flight(visit_run))
        except Exception:  # noqa: BLE001
            in_flight = False
        if in_flight:
            try:
                er = registry.get_entity_runtime(slug)
                for o in _cancel_tree(er.runtime, er.run_store, visit_run):
                    items.append(_item("run", o["run_id"], o["outcome"], o.get("code"), o.get("error")))
                    if o["outcome"] == "cancelled":
                        cancelled["runs"].append(o["run_id"])
                        visit_cancelled = True
            except Exception as e:  # noqa: BLE001
                items.append(_item("run", visit_run, "failed", "run_cancel_failed", f"His visit turn could not be cancelled: {e}"))
    items.append(_queue_stop(entities_dir, slug))
    maint = read_record(entities_dir, slug).get("maintenance")
    if isinstance(maint, dict) and maint.get("status") == "cancel_requested":
        items.append(_item("maintenance", None, "cancel_requested", "maintenance_still_running",
                           "His maintenance pass is finishing; it will not wake him."))
    else:
        items.append(_item("maintenance", None, "not_running"))

    # 3. TEAR DOWN, each step on its own.
    items.append(_kill_loop(home_dir, cancelled))
    items.append(_close_durable(registry, visit_host, name, visit_run, visit_cancelled, cancelled))
    items.append(_close_chat(chat_host, name, cancelled))

    outstanding = [i for i in items if i["outcome"] == "failed"]
    with phase_lock(entities_dir, slug):
        rec = read_record(entities_dir, slug)
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
    })
    return body


def _service() -> Any:
    from .service import get_gateway_service

    return get_gateway_service()


def _cancel_tree(runtime: Any, run_store: Any, run_id: str) -> List[Dict[str, Any]]:
    if runtime is None or run_store is None:
        return [{"run_id": run_id, "outcome": "failed", "code": "run_cancel_failed", "error": "no runtime to cancel it on"}]
    from .entity_seat import cancel_run_tree_outcomes

    return cancel_run_tree_outcomes(runtime, run_store, run_id, reason="stopped by the kill switch")


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


def _kill_loop(home_dir: Path, cancelled: Dict[str, Any]) -> Dict[str, Any]:
    from .entity_loop import freeze_loop, loop_status

    try:
        before = loop_status(home_dir)
        pid = before.get("pid")
        if not before.get("running"):
            return _item("loop", None, "not_running")
        res = freeze_loop(home_dir, reason="kill switch", requested_by="admin")
        cancelled["loop_pid"] = int(pid) if pid else None
        if res.get("killed") is False or (res.get("was_running") and not res.get("killed", True)):
            return _item("loop", pid, "failed", "loop_still_alive", "His own-time process did not stop; press Stop again.")
        return _item("loop", pid, "killed_sigkill" if res.get("escalated_to_sigkill") else "killed_sigterm")
    except Exception as e:  # noqa: BLE001
        return _item("loop", None, "failed", "loop_still_alive", f"His own-time process could not be stopped: {e}")


def _close_durable(registry: Any, visit_host: Any, name: str, run_id: str, was_cancelled: bool, cancelled: Dict[str, Any]) -> Dict[str, Any]:
    if not run_id or visit_host is None:
        return _item("visit", None, "not_open")
    try:
        if was_cancelled:
            visit_host.finalize_cancelled(name, run_id)
        else:
            with visit_host.lease_without_waiting():
                visit_host.close(name, run_id, closed_by="pause", reason="stopped by the kill switch")
        cancelled["visits"].append(run_id)
        return _item("visit", run_id, "closed")
    except Exception as e:  # noqa: BLE001
        return _item("visit", run_id, "failed", "visit_close_failed", f"His visit could not be closed: {getattr(e, 'detail', e)}")


def _close_chat(chat_host: Any, name: str, cancelled: Dict[str, Any]) -> Dict[str, Any]:
    if chat_host is None:
        return _item("chat", None, "not_open")
    try:
        status = chat_host.status(name)
        if not status.get("open"):
            return _item("chat", None, "not_open")
        chat_host.close_open_visit(name, reflect=False)
        cancelled["chats"].append(str(status.get("chat_id")))
        return _item("chat", status.get("chat_id"), "closed")
    except Exception as e:  # noqa: BLE001
        return _item("chat", None, "failed", "chat_close_failed", f"His chat could not be closed: {e}")


def resume(registry: Any, name: str, *, actor: Optional[str] = None, chat_host: Any = None, visit_host: Any = None) -> Dict[str, Any]:
    """Clear stopped, write awake (never a sleep), reconcile from the toggles (contract §3.2)."""
    from abstractruntime.identity.life import read_entity_state, write_entity_state

    t0 = time.monotonic()
    if chat_host is None and visit_host is None:
        chat_host, visit_host = default_hosts()
    manifest = registry.manifest_for(name)
    slug = manifest.slug
    entities_dir = Path(registry.entities_dir)
    home_dir = entities_dir / slug
    actor = actor or _actor()
    with phase_lock(entities_dir, slug):
        already = str(read_entity_state(home_dir).get("state") or "") != "paused"
        if not already:
            write_entity_state(home_dir, "awake", reason=f"resumed [by {actor} via POST /entities/{slug}/lifecycle]")
            rec = read_record(entities_dir, slug)
            rec["maintenance"] = None
            rec["stop_outstanding"] = []
            write_record(entities_dir, slug, rec)
    started = {"own_time": False, "pid": None}
    if not already:
        _marker(registry, manifest, "wake", {"state": "awake", "prior_state": "paused", "reason": "resumed", "by": actor})
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
    registry: Any, name: str, *, work: Optional[bool], personal: Optional[bool],
    actor: Optional[str] = None, chat_host: Any = None, visit_host: Any = None,
) -> Dict[str, Any]:
    """Write the permissions (contract §2). Raises ValueError('stopped') while stopped."""
    from abstractruntime.identity.life import read_entity_state, write_personal_grant

    from .entity_loop import loop_status, stop_loop

    if chat_host is None and visit_host is None:
        chat_host, visit_host = default_hosts()
    manifest = registry.manifest_for(name)
    slug = manifest.slug
    entities_dir = Path(registry.entities_dir)
    home_dir = entities_dir / slug
    actor = actor or _actor()
    changed: Dict[str, Any] = {"work": None, "personal": None}
    with phase_lock(entities_dir, slug):
        if str(read_entity_state(home_dir).get("state") or "") == "paused":
            raise ToggleRefused("stopped_use_resume")
        rec = read_record(entities_dir, slug)
        facts = gather(registry, name, chat_host=chat_host, visit_host=visit_host)
        if facts["archived"]:
            raise ToggleRefused("entity_archived")
        if work is not None and bool(rec.get("work", True)) != bool(work):
            changed["work"] = {"from": bool(rec.get("work", True)), "to": bool(work)}
            rec["work"] = bool(work)
            write_record(entities_dir, slug, rec)
        if personal is not None and bool(facts["personal"]) != bool(personal):
            changed["personal"] = {"from": bool(facts["personal"]), "to": bool(personal)}
            write_personal_grant(home_dir, mode=("until_revoked" if personal else "disabled"), granted_by=actor)
    if changed["personal"] is not None:
        _marker(registry, manifest, "personal_granted" if personal else "personal_grant_revoked",
                {"channel": "toggle", "by": actor, "mode": "until_revoked" if personal else "disabled"})
        if not personal and bool(loop_status(home_dir).get("running")):
            # Own time ends at the loop's next boundary (the current step finishes). He is never
            # put to sleep by the toggle: reconcile repairs the gate's leftover sleep after exit.
            stop_loop(home_dir, reason=f"personal turned off by {actor}", requested_by="operator")
    if changed["work"] is not None and not work:
        loop = loop_status(home_dir)
        if bool(loop.get("running")) and str(loop.get("day_kind") or "") == "work":
            stop_loop(home_dir, reason=f"work turned off by {actor}", requested_by="operator")
    if changed["work"] is not None or changed["personal"] is not None:
        reconcile(registry, name, "toggle", chat_host=chat_host, visit_host=visit_host)
    body = publish(registry, name, "toggle", chat_host=chat_host, visit_host=visit_host)
    body["changed"] = changed
    return body


class ToggleRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def work_allowed(registry: Any, slug: str) -> bool:
    return bool(read_record(Path(registry.entities_dir), slug).get("work", True))


# --------------------------------------------------------------------------- reconcile
def reconcile(registry: Any, name: str, cause: str, *, chat_host: Any = None, visit_host: Any = None) -> Dict[str, Any]:
    """The gateway owns own time (contract §4). Idempotent; returns {own_time, pid, reason}."""
    from abstractruntime.identity.life import write_entity_state

    if chat_host is None and visit_host is None:
        chat_host, visit_host = default_hosts()
    manifest = registry.manifest_for(name)
    slug = manifest.slug
    entities_dir = Path(registry.entities_dir)
    home_dir = entities_dir / slug
    with phase_lock(entities_dir, slug):
        rec = read_record(entities_dir, slug)
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
            write_entity_state(home_dir, "awake", reason="his own time ended; awake and reachable")
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
        if not facts["personal"]:
            return {"own_time": False, "pid": None, "reason": "personal_off"}
        if not bool(rec.get("work", True)) and facts["work_order"]:
            return {"own_time": False, "pid": None, "reason": "work_order_waits_for_runtime"}
        claim = rec.get("spawning") if isinstance(rec.get("spawning"), dict) else None
        if claim and time.time() - float(claim.get("at") or 0) < SPAWN_CLAIM_TTL_S:
            return {"own_time": False, "pid": None, "reason": "spawning"}
        rec["spawning"] = {"at": time.time(), "generation": generation, "cause": cause}
        write_record(entities_dir, slug, rec)
        params = dict(rec.get("own_time_params") or {})

    pid = None
    error: Optional[str] = None
    try:
        started = start_own_time(registry, manifest, params)
        pid = started.get("pid")
    except Exception as e:  # noqa: BLE001 - reported as blocked, never raised at the trigger
        error = str(getattr(e, "detail", "") or e) or e.__class__.__name__

    with phase_lock(entities_dir, slug):
        rec = read_record(entities_dir, slug)
        rec["spawning"] = None
        rec["start_error"] = error
        moved = int(rec.get("generation") or 0) != generation
        write_record(entities_dir, slug, rec)
    if moved and pid:
        # A Stop landed while the loop was being spawned: the new loop never outlives the fence.
        from .entity_loop import freeze_loop

        try:
            freeze_loop(home_dir, reason="kill switch landed during the start", requested_by="admin")
        except Exception:  # noqa: BLE001
            logger.exception("could not stop a loop spawned across a Stop for %s", slug)
        return {"own_time": False, "pid": None, "reason": "stopped"}
    if error:
        return {"own_time": False, "pid": None, "reason": "start_failed", "error": error}
    _marker(registry, manifest, "personal_started", {"channel": "reconcile", "cause": cause, "pid": pid})
    return {"own_time": True, "pid": pid, "reason": cause}


def start_own_time(registry: Any, manifest: Any, params: Dict[str, Any]) -> Dict[str, Any]:
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
    )


def remember_own_time_params(registry: Any, slug: str, params: Dict[str, Any]) -> None:
    entities_dir = Path(registry.entities_dir)
    with phase_lock(entities_dir, slug):
        rec = read_record(entities_dir, slug)
        rec["own_time_params"] = {k: v for k, v in params.items() if v is not None}
        write_record(entities_dir, slug, rec)


def retry(registry: Any, name: str, *, chat_host: Any = None, visit_host: Any = None) -> Dict[str, Any]:
    t0 = time.monotonic()
    manifest = registry.manifest_for(name)
    entities_dir = Path(registry.entities_dir)
    with phase_lock(entities_dir, manifest.slug):
        rec = read_record(entities_dir, manifest.slug)
        rec["start_error"] = None
        write_record(entities_dir, manifest.slug, rec)
    started = reconcile(registry, name, "retry", chat_host=chat_host, visit_host=visit_host)
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
        with phase_lock(entities_dir, slug):
            rec = read_record(entities_dir, slug)
            maint = rec.get("maintenance") if isinstance(rec.get("maintenance"), dict) else None
            st = read_entity_state(home_dir)
            if (
                maint
                and maint.get("status") == "done_pending_wake"
                and int(maint.get("generation") or -1) == int(rec.get("generation") or 0)
                and str(st.get("state") or "") == "asleep"
                and str(st.get("mode") or "") != "visiting"
            ):
                write_entity_state(home_dir, "awake", reason="maintenance done; awake")
                rec["maintenance"] = None
                write_record(entities_dir, slug, rec)
            elif prior and str(prior.get("state") or "") == "asleep" and prior.get("changed_at"):
                # The same sleep continues: it keeps its original since.
                rec["sleep_since_override"] = {"changed_at": st.get("changed_at"), "since": prior.get("changed_at")}
                write_record(entities_dir, slug, rec)
        reconcile(registry, slug, cause, chat_host=chat_host, visit_host=visit_host)
    except Exception:  # noqa: BLE001 - the close itself already succeeded
        logger.exception("after-visit reconcile failed for %s", slug)
    safe_publish(registry, slug, cause, chat_host=chat_host, visit_host=visit_host)


def maintenance_started(registry: Any, slug: str) -> int:
    """Record a running operator maintenance pass; returns the generation it runs under."""
    entities_dir = Path(registry.entities_dir)
    with phase_lock(entities_dir, slug):
        rec = read_record(entities_dir, slug)
        gen = int(rec.get("generation") or 0)
        rec["maintenance"] = {"status": "running", "generation": gen, "started_at": _now_iso()}
        write_record(entities_dir, slug, rec)
        return gen


def maintenance_may_write(registry: Any, slug: str, generation: int) -> bool:
    """The pass's `finally` may restore its own sleep only when no Stop landed and no visit took
    the posture (contract §3.3, §4.3)."""
    from abstractruntime.identity.life import read_entity_state

    entities_dir = Path(registry.entities_dir)
    rec = read_record(entities_dir, slug)
    if int(rec.get("generation") or 0) != int(generation):
        return False
    st = read_entity_state(entities_dir / slug)
    return str(st.get("state") or "") == "asleep" and str(st.get("mode") or "") != "visiting"


def maintenance_done(registry: Any, name: str, generation: int, *, failed: Optional[str] = None,
                     chat_host: Any = None, visit_host: Any = None) -> str:
    """The operator sleep's pass returned: wake + reconcile, unless a Stop moved the generation
    (nothing written) or a visit holds the posture (the wake runs at the visit's close).
    Returns the outcome word: woke | stopped | after_visit | not_ours."""
    from abstractruntime.identity.life import read_entity_state, write_entity_state

    manifest = registry.manifest_for(name)
    slug = manifest.slug
    entities_dir = Path(registry.entities_dir)
    home_dir = entities_dir / slug
    with phase_lock(entities_dir, slug):
        rec = read_record(entities_dir, slug)
        if int(rec.get("generation") or 0) != int(generation):
            rec["maintenance"] = None
            write_record(entities_dir, slug, rec)
            return "stopped"
        st = read_entity_state(home_dir)
        if str(st.get("state") or "") == "asleep" and str(st.get("mode") or "") == "visiting":
            rec["maintenance"] = {"status": "done_pending_wake", "generation": generation, "failed": failed}
            write_record(entities_dir, slug, rec)
            return "after_visit"
        rec["maintenance"] = None
        write_record(entities_dir, slug, rec)
        if str(st.get("state") or "") != "asleep" or str(st.get("written_by") or "operator") != "operator":
            return "not_ours"  # a summon or a wake already ended this sleep
        reason = "maintenance done; awake" + (f" (the pass failed: {failed})" if failed else "")
        write_entity_state(home_dir, "awake", reason=reason)
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
