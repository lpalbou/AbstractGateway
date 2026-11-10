"""The entity TILE: what one small card about an entity shows, and nothing it does not (backlog 1012).

Operator, 2026-11-07: "it is absurd for a tiny card about the entity to request so much data that
you will never show", and, of the Board's tiles: "there is zero information". A tile answers, at a
glance, for a person:

- who: the name, the look (the identity record's design and colour), its age, its mind (model);
- now: what it is doing (the ONE composite phase `/cognition` serves), whether it is busy this
  instant, since when, and the one trouble worth a warning (its own time stopped after failures);
- what it is into lately: its newest standing interests, in its own words;
- what is on its mind: its newest open problem or question, in its own words;
- its latest lesson, and its last MEANINGFUL moment (a closed set of kinds: a visit, a sleep, its
  own time... never the operator reading its diary or the birth recording of its identity);
- counts a person can read (open questions and problems, lessons, interests, sleeps).

Words are the client's (the shared `EntityCard` in AbstractUIC's ui-kit): this module serves closed
kinds and the entity's own texts, never snake_case event names to display, never ids, never
provider jargon.

COST. Two halves of very different price:
- the memory half (interests, open problems/questions, lessons) is a PROJECTION of the engine card
  compositor (abstractmemory `entity_card` — one compositor, never a second one that can drift),
  whose result is cached per (home directory, journal high-water seq, parameters): a repeated read
  of an unchanged life is free, a grown life recomputes once;
- the live half (state, phase, loop, visit, moments, look, mind, age) is read fresh on every request
  from small files and the door's own visit/chat hosts — it changes without the journal moving (a
  sleep, a visit ending), so it is never cached by seq.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Hashable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "ENTITY_TILE_ENDPOINT",
    "ENTITY_TILE_VERSION",
    "INTO_LATELY_BOUND",
    "TILE_MOMENT_KINDS",
    "BoundedCache",
    "ENGINE_CARD_CACHE",
    "LIFE_MOMENTS_CACHE",
    "compose_tile",
    "entity_tile_contract",
    "home_cache_key",
    "store_identity",
    "last_meaningful_moment",
    "memory_facts",
    "now_since",
]

ENTITY_TILE_VERSION = 1
ENTITY_TILE_ENDPOINT = "/api/gateway/entities/{name}/tile"

# Newest standing interests a tile carries ("into lately").
INTO_LATELY_BOUND = 3

# The moment kinds a tile may serve as its LAST MOMENT: acts a person recognises as part of the
# entity's life. Everything else on the merged moment list is bookkeeping or observation (the
# operator reading the diary, the birth recording of an identity, a prompt overlay hash, queue
# census, maintenance windows...) and is skipped, never shown. `own_time_*` are the engraved twins
# of `personal_*` (append-only historical streams carry both spellings); the tile serves the ruled
# spelling only.
TILE_MOMENT_KINDS = (
    "summon",  # a visit began
    "session_closed",  # a visit ended
    "sleep",
    "wake",
    "pause",  # stopped by the kill switch
    "personal_started",
    "personal_stop_requested",
    "personal_frozen",  # its own time was cut off (the process died without ceremony)
    "phase_changed",  # carries `to`
    "substrate_changed",  # its mind (model) changed
    "night_voice",  # a dream was narrated
    "work_order_changed",  # carries `change`: set | cleared
    "task_inbox_changed",  # a task was handed over or moved
    "identity_changed",  # its look or voice changed — never the birth recording
)
_ENGRAVED_TWINS = {
    "own_time_started": "personal_started",
    "own_time_stop_requested": "personal_stop_requested",
    "own_time_frozen": "personal_frozen",
}
# An identity_changed row whose cause is the birth recording of a default identity is not a change
# anyone made (framework ADR 0052 §7: `recorded_default`).
_IDENTITY_NOT_A_CHANGE_CAUSES = frozenset({"recorded_default"})
_RULED_PHASES = ("visit", "work", "personal", "sleep")
# The gateway's `phase_changed` announcements (contract-phases v2 §5) whose cause is a door act
# already recorded as its own moment: the tile shows the act, never its echo.
_DOOR_ACT_CAUSES = frozenset({
    "visit_opened", "visit_closed", "chat_opened", "chat_closed", "summon_started", "toggle", "stop",
    "resume", "retry", "operator_sleep", "operator_wake", "kill_switch", "idle_sleep",
})
# What the tile's "now" may say (contract-phases v2 §1.1): the four phases plus the two statuses.
_SERVED_PHASES = _RULED_PHASES + ("idle", "stopped")


def entity_tile_contract() -> Dict[str, Any]:
    """How a client learns the tile read exists (on the roster and in the discovery contract): an
    older gateway serves neither, and the client falls back to the full card."""
    return {"available": True, "version": ENTITY_TILE_VERSION, "endpoint": ENTITY_TILE_ENDPOINT}


# --------------------------------------------------------------------------- caches


class BoundedCache:
    """A small thread-safe LRU. `get_or_compute` runs `compute` at most once per key at a time
    (concurrent pollers of one cold key wait for the first instead of stacking compositor reads
    on the shared thread pool); a compute that raises caches nothing."""

    _STRIPES = 32

    def __init__(self, maxsize: int) -> None:
        if int(maxsize) < 1:
            raise ValueError("maxsize must be >= 1")
        self.maxsize = int(maxsize)
        self._data: "OrderedDict[Hashable, Any]" = OrderedDict()
        self._lock = threading.Lock()
        # Striped compute locks: a key always maps to the same lock, nothing to create or clean up
        # (a per-key lock popped after its compute could be recreated while a waiter still held the
        # old one, and two computes of one key would then run side by side).
        self._stripes = tuple(threading.Lock() for _ in range(self._STRIPES))
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self.hits = 0
            self.misses = 0

    def get(self, key: Hashable) -> Optional[Any]:
        with self._lock:
            if key not in self._data:
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return self._data[key]

    def put(self, key: Hashable, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def get_or_compute(self, key: Hashable, compute: Callable[[], Tuple[Any, bool]]) -> Any:
        """`compute()` returns (value, cacheable): a value computed while the life moved under it is
        served but not kept (its key would claim a state it does not exactly describe)."""
        hit = self.get(key)
        if hit is not None:
            return hit
        with self._stripes[hash(key) % len(self._stripes)]:
            hit = self.get(key)
            if hit is not None:
                return hit
            with self._lock:
                self.misses += 1
            value, cacheable = compute()
            if cacheable:
                self.put(key, value)
            return value


# The engine card per (home directory, journal seq, parameters). 17 entities × a few parameter
# variants fit with room; an ephemeral-sized card is ~90 KB, so the bound caps the cache near 6 MB.
ENGINE_CARD_CACHE = BoundedCache(maxsize=64)
# The merged host-marker + state-history moments per (home, both files' size and mtime): the marker
# stream of a long life is megabytes of JSON lines (ephemeral: 6 MB, 2,700 markers).
LIFE_MOMENTS_CACHE = BoundedCache(maxsize=64)


def home_cache_key(home_dir: Any) -> str:
    """The cache key of a home: its resolved directory. Unique per principal AND per entity (a
    name-keyed cache would serve tenant A's life to tenant B's same-named entity whenever their
    journal seqs collide — the communities F3 rule); resolving folds symlinks and name-case
    aliases onto one key."""
    try:
        return str(Path(home_dir).resolve())
    except OSError:
        return str(home_dir)


def store_identity(path: Any) -> Tuple[int, int]:
    """Which memory database file this is (device, inode): a home restored from a backup or deleted
    and re-created under the same name is another file, so its seq never meets the old life's
    cached card. (-1, -1) when it cannot be read (then the directory + seq key still holds)."""
    try:
        st = Path(path).stat()
        return (int(st.st_dev), int(st.st_ino))
    except OSError:
        return (-1, -1)


# --------------------------------------------------------------------------- the memory half


def _iso(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _text_of(row: Mapping[str, Any]) -> str:
    """The entity's own words for a brief: the statement (the full text), else the title."""
    return str(row.get("statement") or "").strip() or str(row.get("title") or "").strip()


def _newest(rows: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    best: Optional[Mapping[str, Any]] = None
    for row in rows:
        if not isinstance(row, Mapping) or not _text_of(row):
            continue
        if best is None or str(row.get("observed_at") or "") >= str(best.get("observed_at") or ""):
            best = row
    return best


def memory_facts(engine_card: Mapping[str, Any]) -> Dict[str, Any]:
    """The tile's memory half, projected from the engine card's own sections (never re-derived):
    the newest standing interests, the newest open problem or question, the latest lesson, and the
    counts. Order comes from `observed_at`, the engine's own chronology."""
    discoveries = engine_card.get("discoveries") if isinstance(engine_card.get("discoveries"), Mapping) else {}
    interests = [r for r in (discoveries.get("interests") or []) if isinstance(r, Mapping) and _text_of(r)]
    interests.sort(key=lambda r: str(r.get("observed_at") or ""), reverse=True)
    into_lately = [
        {"text": _text_of(r), "at": _iso(r.get("observed_at")), "explored": bool(r.get("explored"))}
        for r in interests[:INTO_LATELY_BOUND]
    ]

    def _open(section: str) -> List[Mapping[str, Any]]:
        sec = engine_card.get(section)
        rows = sec.get("open") if isinstance(sec, Mapping) else None
        return [r for r in (rows or []) if isinstance(r, Mapping)]

    open_questions = _open("questions")
    open_problems = _open("problems")
    newest_problem = _newest(open_problems)
    newest_question = _newest(open_questions)
    on_its_mind: Optional[Dict[str, Any]] = None
    candidates = [(k, r) for k, r in (("problem", newest_problem), ("question", newest_question)) if r is not None]
    if candidates:
        # The newest of the two; a problem wins a tie (something is wrong, which outranks wondering).
        kind, row = max(candidates, key=lambda kr: (str(kr[1].get("observed_at") or ""), kr[0] == "problem"))
        on_its_mind = {"kind": kind, "text": _text_of(row), "at": _iso(row.get("observed_at"))}

    lessons_sec = engine_card.get("lessons") if isinstance(engine_card.get("lessons"), Mapping) else {}
    lessons = [r for r in (lessons_sec.get("lessons") or []) if isinstance(r, Mapping)]
    newest_lesson = _newest(lessons)
    lessons_total = lessons_sec.get("total")
    return {
        "into_lately": into_lately,
        "on_its_mind": on_its_mind,
        "latest_lesson": (
            {"text": _text_of(newest_lesson), "at": _iso(newest_lesson.get("observed_at"))} if newest_lesson else None
        ),
        "counts": {
            "open_questions": len(open_questions),
            "open_problems": len(open_problems),
            "lessons": int(lessons_total) if isinstance(lessons_total, int) else len(lessons),
            "interests": len(interests),
        },
    }


# --------------------------------------------------------------------------- the live half


def _moment_kind(m: Mapping[str, Any]) -> Optional[str]:
    kind = str(m.get("kind") or "")
    kind = _ENGRAVED_TWINS.get(kind, kind)
    if kind not in TILE_MOMENT_KINDS:
        return None
    details = m.get("details") if isinstance(m.get("details"), Mapping) else {}
    if kind == "identity_changed" and str(details.get("cause") or "") in _IDENTITY_NOT_A_CHANGE_CAUSES:
        return None
    return kind


def last_meaningful_moment(moments: Sequence[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """The newest moment a person recognises (TILE_MOMENT_KINDS), with the one structured detail
    its words need: `to` for a phase change (a ruled phase word), `change` for a work order."""
    for m in sorted(
        (m for m in moments if isinstance(m, Mapping)), key=lambda m: str(m.get("at") or ""), reverse=True
    ):
        kind = _moment_kind(m)
        if kind is None or not _iso(m.get("at")):
            continue
        out: Dict[str, Any] = {"kind": kind, "at": _iso(m.get("at"))}
        details = m.get("details") if isinstance(m.get("details"), Mapping) else {}
        if kind == "phase_changed":
            to = str(details.get("to") or "")
            if to not in _RULED_PHASES:
                continue  # a phase change to a word the tile cannot name is not shown as one
            if details.get("from") is not None and str(details.get("from")) == to:
                continue  # a new snapshot that is not a phase change (a toggle, a sub-object)
            if str(details.get("cause") or "") in _DOOR_ACT_CAUSES:
                continue  # the act itself is the moment (sleep, wake, pause, summon...), not its echo
            out["to"] = to
        if kind == "work_order_changed" and str(details.get("change") or "") in ("set", "cleared"):
            out["change"] = str(details.get("change"))
        return out
    return None


# Which moments start which phase (the `since` of the tile's "now").
_PHASE_STARTS = {
    "visit": ("summon",),
    "personal": ("personal_started",),
    "work": (),
    "sleep": ("sleep", "session_closed", "personal_frozen", "personal_stop_requested"),
}


def now_since(
    *,
    phase: Optional[str],
    state: Mapping[str, Any],
    loop: Mapping[str, Any],
    chat: Mapping[str, Any],
    moments: Sequence[Mapping[str, Any]],
) -> Optional[str]:
    """Since when the entity is in its current phase, from structured facts only (state times,
    the chat seat's opening, the newest moment that starts the phase). None when nothing says."""
    changed_at = _iso(state.get("changed_at"))
    if phase is None or str(state.get("state") or "") == "paused":
        return changed_at  # stopped: the kill switch's own time
    if phase == "sleep" and str(state.get("state") or "") == "asleep":
        return changed_at
    if phase == "visit" and chat.get("open") and _iso(chat.get("opened_at")):
        return _iso(chat.get("opened_at"))
    starts = _PHASE_STARTS.get(phase, ())
    newest: Optional[str] = None
    for m in moments:
        if not isinstance(m, Mapping):
            continue
        kind = _ENGRAVED_TWINS.get(str(m.get("kind") or ""), str(m.get("kind") or ""))
        details = m.get("details") if isinstance(m.get("details"), Mapping) else {}
        starts_it = kind in starts or (kind == "phase_changed" and str(details.get("to") or "") == phase)
        at = _iso(m.get("at"))
        if starts_it and at and (newest is None or at > newest):
            newest = at
    if phase == "sleep":
        # The resting default (awake, nothing running): it rests since the last thing that ended —
        # the state's own change or the loop stopping, whichever came last.
        loop_at = _iso(loop.get("updated_at")) if not loop.get("running") else None
        return max((t for t in (newest, changed_at, loop_at) if t), default=None)
    return newest or changed_at


def _trouble(loop: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The one trouble a tile warns about: its own time stopped itself after repeated failures
    (three failed ticks in a row, usually the model timing out). Cleared by the next start."""
    if not loop.get("running") and str(loop.get("stopped_by") or "") == "failures":
        return {"kind": "own_time_failed", "at": _iso(loop.get("updated_at"))}
    return None


def _age_days(born: Any) -> Optional[int]:
    try:
        created = datetime.fromisoformat(str(born).replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - created).days)
    except (TypeError, ValueError):
        return None


def compose_tile(
    *,
    name: str,
    slug: str,
    born: Any,
    look: Optional[Mapping[str, Any]],
    mind: Optional[Mapping[str, Any]],
    now: Mapping[str, Any],
    moments: Sequence[Mapping[str, Any]],
    memory: Mapping[str, Any],
    sleeps: Optional[int],
    as_of_seq: Optional[int],
    warnings: Sequence[str] = (),
) -> Dict[str, Any]:
    """The tile payload (version 1). `now` is the shared live fold (`_live_now` in the routes)."""
    phase = now.get("phase")
    state = now.get("st") if isinstance(now.get("st"), Mapping) else {}
    loop = now.get("loop") if isinstance(now.get("loop"), Mapping) else {}
    chat = now.get("chat") if isinstance(now.get("chat"), Mapping) else {}
    tile_phase = phase if phase in _SERVED_PHASES else "stopped"
    out: Dict[str, Any] = {
        "tile_version": ENTITY_TILE_VERSION,
        "name": name,
        "slug": slug,
        "look": dict(look) if look else None,
        "born": _iso(born),
        "age_days": _age_days(born),
        "mind": dict(mind) if mind else None,
        "now": {
            "phase": tile_phase,
            # The operator state under the phase: "awake" under phase sleep is the resting default
            # (nothing running, IDLE-IS-SLEEP), which a person reads as resting, not asleep.
            "state": str(state.get("state") or "") or None,
            "detail": now.get("sleep_detail") if tile_phase == "sleep" else None,
            "busy": bool(now.get("working")),
            "since": now_since(phase=phase, state=state, loop=loop, chat=chat, moments=moments),
            "trouble": _trouble(loop),
        },
        "into_lately": list(memory.get("into_lately") or []),
        "on_its_mind": memory.get("on_its_mind"),
        "latest_lesson": memory.get("latest_lesson"),
        "last_moment": last_meaningful_moment(moments),
        "counts": {**dict(memory.get("counts") or {}), "sleeps": int(sleeps) if isinstance(sleeps, int) else None},
        "as_of_seq": int(as_of_seq) if as_of_seq is not None else None,
        "read_at": datetime.now(timezone.utc).isoformat(),
    }
    if warnings:
        out["warnings"] = list(warnings)
    return out
