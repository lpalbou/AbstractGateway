"""The gateway's share of the understanding drain (runtime 0869 / gateway 1013, v1).

An entity's memory carries a queue of rebuild tasks (`MemorySystem.tasks`: digests to rewrite in its
own words, cards to author, dreams to narrate). The RUNTIME drains it — `abstractruntime.identity.
understanding.drain(...)`, one durable run per window on the home runtime, every model call a ledger
row. The gateway's part is WHEN and WITH WHAT: after a visit turn parks (window `visit`, bounded by the
profile's `cap_per_turn`), and in idle time for a home whose own-time loop does not run (window `idle`,
until a selection comes back empty), with capability clients the door builds
(`EntityRegistry.capability_llm_factory`).

This module holds the per-home worker and the resolution of the runtime's `drain` BY NAME: a runtime
without it is an ImportError the caller records as a failed drain (the turn probe then says so), never
an `if hasattr` hedge that ships the feature silently missing.

Serialization: one drain job runs per home at a time (the home lease would refuse a second writer
anyway); a job scheduled while one runs REPLACES any job still waiting for that home — a drain is
idempotent over the queue, so the newest intent is the only one worth keeping. The executor is this
module's own small pool, not the shared `watchers` pool: a drain holds a thread for the length of a
window and must never crowd mail polling or provider probes, nor be crowded by them.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "DRAIN_HOLDER",
    "IDLE_DRAIN_HARD_CAP",
    "SECONDS_PER_TASK",
    "HomeDrainWorker",
    "resolve_drain",
    "drain_failure_report",
    "skipped_drain_report",
]

#: The lease holder the runtime's drain writes under; the visit host waits briefly on THIS holder only.
DRAIN_HOLDER = "memory-tasks"

#: What one rebuild task is assumed to cost when nothing was measured yet (memory's activation gate
#: default; memory owner's correction 2: a window budget below hard_cap x 60 leaves every task out as
#: over_budget). The visit window's budget is cap_per_turn x this; the cap is the real bound — a window
#: ends at a task boundary, never mid-task, so the budget only shapes which tasks are selected.
SECONDS_PER_TASK = 60.0

#: Tasks one idle drain call may run before the host re-checks whether the home is still idle; the
#: idle loop calls again while a call ended on the cap or the budget, and stops on an empty selection.
IDLE_DRAIN_HARD_CAP = 20


def resolve_drain() -> Callable[..., Dict[str, Any]]:
    """The runtime's `drain`, by name. Tests replace this function; production never guards it."""
    from abstractruntime.identity.understanding import drain

    return drain


def drain_failure_report(*, trigger: str, window: str, parent: Mapping[str, Any], error: BaseException) -> Dict[str, Any]:
    """The report recorded when a drain could not run or raised: `error` names the exception class
    and message so the turn probe can say the rebuild did not happen (no silent empty list)."""
    return {
        "trigger": trigger,
        "window": window,
        "parent": dict(parent),
        "selected": 0,
        "rebuilt": [],
        "ended_by": "error",
        "error": f"{type(error).__name__}: {error}",
    }


def skipped_drain_report(*, trigger: str, window: str, parent: Mapping[str, Any], reason: str) -> Dict[str, Any]:
    """The report recorded when no drain run is started on purpose (cap_per_turn is 0): a fresh
    empty `rebuilt`, so a probe never shows an earlier turn's list as this turn's."""
    return {
        "trigger": trigger,
        "window": window,
        "parent": dict(parent),
        "selected": 0,
        "rebuilt": [],
        "ended_by": "cap",
        "skipped": reason,
    }


class HomeDrainWorker:
    """Runs drain jobs, one at a time per home, on a small pool of its own."""

    def __init__(self, *, max_workers: int = 2) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max(1, int(max_workers)), thread_name_prefix="entity-drain")
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._running: set = set()
        self._pending: Dict[str, Callable[[], None]] = {}
        self._closing = False

    @property
    def closing(self) -> bool:
        """True once `shutdown()` was called: every `should_continue` the host builds reads it, so a
        running window ends at its next task boundary."""
        return self._closing

    def schedule(self, home: str, job: Callable[[], None]) -> str:
        """Run `job` for `home` now, or after the job currently running for it. Returns "started",
        "queued" (replacing any job still waiting for that home) or "closing" (nothing scheduled)."""
        key = str(home)
        with self._lock:
            if self._closing:
                return "closing"
            if key in self._running:
                self._pending[key] = job
                return "queued"
            self._running.add(key)
        self._executor.submit(self._run, key, job)
        return "started"

    def _run(self, home: str, job: Callable[[], None]) -> None:
        while True:
            try:
                job()
            except Exception:  # noqa: BLE001 - a job's own failure never kills the worker
                logger.exception("entity drain job failed for home %r", home)
            with self._lock:
                nxt = self._pending.pop(home, None)
                if nxt is None or self._closing:
                    self._pending.pop(home, None)
                    self._running.discard(home)
                    self._idle.notify_all()
                    return
                job = nxt

    def is_busy(self, home: Optional[str] = None) -> bool:
        with self._lock:
            if home is None:
                return bool(self._running)
            return str(home) in self._running

    def wait_idle(self, *, home: Optional[str] = None, timeout: Optional[float] = None) -> bool:
        """Block until no job runs (for `home`, or at all). True when idle, False on timeout."""
        with self._idle:
            return self._idle.wait_for(lambda: not (self._running if home is None else (str(home) in self._running)), timeout=timeout)

    def shutdown(self, *, wait_s: float = 5.0) -> None:
        """Stop taking jobs, drop waiting ones, let running windows reach their next task boundary
        (bounded wait), then release the threads. Nothing is interrupted mid-task."""
        with self._lock:
            self._closing = True
            self._pending.clear()
        self.wait_idle(timeout=max(0.0, float(wait_s)))
        self._executor.shutdown(wait=False, cancel_futures=True)
