"""The entity door's VISIT QUEUE store — decision:summon-queue-v1 (2026-07-25).

Queued visits instead of 409 refusals, opt-in (`queue: true` on the summon
body): the door stores the FULL summon payload and EXECUTES it at admission
(the client never resubmits — the two-waiters-race class cannot exist), a
parked entry (`park: true`) doubles as the "leave it with her" mailbox drop,
and the runtime invariants bind (the per-home lease is untouched; admission
grants the right to ATTEMPT a summon; the door never holds anything for a
waiter; admission is idempotent per queue_id — the command_id discipline).

THIS MODULE is the durable store + the sweeper clock ONLY. Admission logic
(which re-runs the whole summon path) lives in routes/entities.py and is
INJECTED here (`set_queue_sweep_executor`) — the store must not import the
routes (cycle), and the sweep must not know door mechanics.

The queue file lives OUTSIDE the home (door bookkeeping that must not travel
on directory copy, exactly like `.live_summons` and `.host_stream`):
    entities/.queues/<slug>.json = {"entries": [ ... ordered ... ]}

Concurrency: every mutation happens under the per-slug guard RLock exported
by entity_seat (the SAME lock the summon door holds across check -> start ->
record), so enqueue / admission / poll / leave / sweep serialize with
summons in-process. Cross-process exclusion stays the home lease's job
(runtime's ruling) — the queue is bookkeeping, never a second exclusion
primitive.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Poll-mode entries reap after this much poll silence (client death must
# never strand a slot — the visit-reaper lesson). park:true entries are
# EXEMPT: they are the mailbox and persist until admitted (contract §19).
QUEUE_POLL_REAP_S = 120
# Loud enqueue cap per home (contract §19: refused loudly, never silently).
QUEUE_MAX_PER_HOME = 20
# The sweeper's clock (close-fires is the fast path; this is the backstop
# for crashed visits, parked entries, and pollers that died).
QUEUE_SWEEP_PERIOD_S = 20.0

_TERMINAL_STATES = {"admitted", "stepped_away", "reaped", "failed"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def queue_path(entities_dir: Path, slug: str) -> Path:
    return Path(entities_dir) / ".queues" / f"{slug}.json"


def read_queue(entities_dir: Path, slug: str) -> List[Dict[str, Any]]:
    path = queue_path(entities_dir, slug)
    try:
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data.get("entries") if isinstance(data, dict) else None
        return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
    except Exception:
        # Unreadable bookkeeping degrades to an empty queue with a loud log —
        # the seat guard (not this file) protects the one life. The corrupt
        # bytes are MOVED ASIDE first (audit P2-2): the next write would
        # otherwise silently destroy every stored prompt (parked mailbox
        # words included) under the fresh empty list.
        logger.warning("queue file unreadable for %s (moved aside; degrading to empty)", slug, exc_info=True)
        try:
            path.replace(path.with_suffix(f".corrupt-{secrets.token_hex(4)}"))
        except Exception:
            pass
        return []


# Terminal entries kept for poll visibility, then pruned (audit P2-3): the
# queue file is a payload STORE, not an archive — without pruning it grows
# forever and keeps prompt words at rest indefinitely.
_TERMINAL_KEEP = 50


def write_queue(entities_dir: Path, slug: str, entries: List[Dict[str, Any]]) -> None:
    terminal = [e for e in entries if str(e.get("state") or "") in _TERMINAL_STATES]
    if len(terminal) > _TERMINAL_KEEP:
        drop = set(
            id(e) for e in sorted(terminal, key=lambda e: str(e.get("enqueued_at") or ""))[: len(terminal) - _TERMINAL_KEEP]
        )
        entries = [e for e in entries if id(e) not in drop]
    path = queue_path(entities_dir, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp-{secrets.token_hex(4)}")
    tmp.write_text(json.dumps({"entries": entries}), encoding="utf-8")
    tmp.replace(path)


def queued_entries(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [e for e in entries if str(e.get("state") or "") == "queued"]


def queue_depth(entities_dir: Path, slug: str) -> int:
    return len(queued_entries(read_queue(entities_dir, slug)))


def position_of(entries: List[Dict[str, Any]], queue_id: str) -> Optional[int]:
    """1-based position among QUEUED entries; None once terminal."""
    pos = 0
    for e in entries:
        if str(e.get("state") or "") != "queued":
            continue
        pos += 1
        if str(e.get("queue_id") or "") == queue_id:
            return pos
    return None


def new_entry(
    *,
    payload: Dict[str, Any],
    caller: str,
    caller_kind: str,
    park: bool,
) -> Dict[str, Any]:
    now = _now_iso()
    return {
        "queue_id": f"q-{secrets.token_hex(8)}",
        # THE FULL SUMMON PAYLOAD rests here (contract §4): the door executes
        # it at admission; words-never-lost is structural, and a park entry
        # with no polling client is exactly the mailbox drop.
        "payload": dict(payload),
        "caller": caller,
        "caller_kind": caller_kind,
        "park": bool(park),
        "state": "queued",
        "enqueued_at": now,
        "last_poll_at": now,
        "attempts": 0,
        "last_attempt_at": None,
        "waiting_behind": None,
        "admitted_run_id": None,
        "admitted_session_id": None,
        "failed_reason": None,
    }


def find_entry(entries: List[Dict[str, Any]], queue_id: str) -> Optional[Dict[str, Any]]:
    for e in entries:
        if str(e.get("queue_id") or "") == queue_id:
            return e
    return None


def head_entry(entries: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    q = queued_entries(entries)
    return q[0] if q else None


def reap_poll_silent(entries: List[Dict[str, Any]], *, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Mark poll-silent, non-park queued entries reaped. Returns the reaped
    entries (caller writes the store + the markers). park entries persist —
    they are the mailbox (contract §19)."""
    now_dt = now or _now()
    reaped: List[Dict[str, Any]] = []
    for e in queued_entries(entries):
        if bool(e.get("park")):
            continue
        try:
            last = datetime.fromisoformat(str(e.get("last_poll_at") or e.get("enqueued_at") or ""))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
        except Exception:
            continue  # unreadable timestamp: leave it for the cap/operator
        if now_dt - last > timedelta(seconds=QUEUE_POLL_REAP_S):
            e["state"] = "reaped"
            e["failed_reason"] = f"poll-silent for more than {QUEUE_POLL_REAP_S}s (client gone)"
            reaped.append(e)
    return reaped


def slugs_with_queues(entities_dir: Path) -> List[str]:
    root = Path(entities_dir) / ".queues"
    try:
        if not root.is_dir():
            return []
        return sorted(p.stem for p in root.glob("*.json"))
    except Exception:
        return []


# ------------------------------------------------------------- the sweeper
# One daemon thread per process, lazily started (no queues = no thread; dies
# with the process, never machine persistence). The executor is INJECTED by
# routes/entities.py: sweep(slug) reaps poll-silent entries and attempts the
# head admission with full door context (markers included). The thread is
# the contract's REAPER BACKSTOP (invariant 11): close-paths fire admission
# synchronously; this clock covers crashed visits, parked entries, and dead
# pollers.

_sweep_executor: Optional[Callable[..., None]] = None
_sweep_thread: Optional[threading.Thread] = None
_sweep_lock = threading.Lock()
# dir -> owning context (or None). The CONTEXT is whatever the registrar
# hands over — routes pass (svc, registry) so a sweep runs against the
# OWNING service (audit P1-4: the sweeper thread has no request context;
# re-resolving get_gateway_service() there always answered the BASE service,
# so per-principal parked entries could never admit by clock).
_sweep_dirs: Dict[str, Any] = {}


def set_queue_sweep_executor(fn: Callable[..., None]) -> None:
    """fn(slug, context) — context is the value registered with the dir."""
    global _sweep_executor
    _sweep_executor = fn


def ensure_queue_sweeper(entities_dir: Path, *, context: Any = None) -> None:
    """Register a data root (+ its owning service context) for sweeping and
    start the sweeper thread once. Called from every door touch that
    creates/reads queue state, and from the service factory at boot (so
    parked entries survive a bounce with no client traffic). A later
    registration with a non-None context upgrades a None one (boot order)."""
    global _sweep_thread
    with _sweep_lock:
        key = str(Path(entities_dir))
        if context is not None or key not in _sweep_dirs:
            _sweep_dirs[key] = context if context is not None else _sweep_dirs.get(key)
        if _sweep_thread is not None and _sweep_thread.is_alive():
            return
        _sweep_thread = threading.Thread(target=_sweep_forever, name="entity-queue-sweeper", daemon=True)
        _sweep_thread.start()


def _sweep_forever() -> None:
    while True:
        time.sleep(QUEUE_SWEEP_PERIOD_S)
        fn = _sweep_executor
        if fn is None:
            continue
        with _sweep_lock:
            dirs = dict(_sweep_dirs)
        for d, context in dirs.items():
            for slug in slugs_with_queues(Path(d)):
                try:
                    fn(slug, context)
                except Exception:
                    # One broken home's sweep must never stop the clock for
                    # the rest (the emit-event isolation rule).
                    logger.warning("queue sweep failed for %s (clock continues)", slug, exc_info=True)
