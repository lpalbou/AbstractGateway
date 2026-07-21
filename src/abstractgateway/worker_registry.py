"""Process-wide registry of long-lived gateway worker threads.

Resilience wave 2026-07-21 (adversary P2-5): the gateway spawns a handful of
long-lived daemon threads (runner, entity reapers, self-repair sweeper,
telegram/email/agora bridges). Their loops are individually exception-guarded,
but nothing surfaced their LIVENESS — a dead reaper silently stopped closing
idle sessions with zero signal. This registry is the one place such threads
announce themselves; /api/health renders name -> alive so a dead worker is
visible on the probe operators already watch.

Deliberately dumb: strong references (a handful of Thread objects — dead-thread
visibility is the point, so entries never vanish on GC), last-writer-wins on
re-register (a restarted worker replaces its predecessor's entry), zero
dependencies. Registration is OPT-IN at spawn sites; short-lived helper threads
(per-request TTS feeders, typing indicators) stay out by design.
"""

from __future__ import annotations

import threading
from typing import Dict

_lock = threading.Lock()
_workers: Dict[str, threading.Thread] = {}


def register_worker(name: str, thread: threading.Thread) -> None:
    """Announce a long-lived worker thread under a stable name.

    Re-registering a name replaces the previous entry (restart semantics).
    Never raises: liveness bookkeeping must not break the worker it watches.
    """
    try:
        key = str(name or "").strip()
        if not key or thread is None:
            return
        with _lock:
            _workers[key] = thread
    except Exception:
        pass


def unregister_worker(name: str) -> None:
    """Remove a worker entry on DELIBERATE stop (stop()/shutdown paths).

    A deliberately-stopped worker must not read as a death on /api/health —
    the registry only ever alarms on threads that should still be running.
    """
    try:
        with _lock:
            _workers.pop(str(name or "").strip(), None)
    except Exception:
        pass


def workers_snapshot() -> Dict[str, Dict[str, object]]:
    """name -> {alive} for every registered worker. Pure read, never raises."""
    out: Dict[str, Dict[str, object]] = {}
    try:
        with _lock:
            items = list(_workers.items())
        for name, thread in items:
            try:
                out[name] = {"alive": bool(thread.is_alive())}
            except Exception:
                out[name] = {"alive": False}
    except Exception:
        pass
    return out
