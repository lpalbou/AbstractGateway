"""Event-loop watchdog: a hung gateway exits so its service manager restarts it.

Why (2026-10-04 03:15): a text-to-speech stream blocked the event loop for
10+ minutes at 95% CPU. The process stayed alive, so nothing restarted it —
launchd's `KeepAlive` and systemd's `Restart=on-failure` restart a process
that EXITS, never one that hangs, and the local supervisor only counted its
failed health probes. A gateway whose loop cannot run answers nothing; the
honest thing is to say why and leave.

How:

- a tick coroutine on the event loop stamps `time.monotonic()` every
  `tick_s` (at most 1 s);
- a daemon thread checks the stamp; when it is older than `limit_s` (the
  `serve --watchdog-seconds` flag, default 30), it writes one ``[FATAL]``
  line, the stack of the event-loop thread (the code that blocks it) and a
  faulthandler dump of every thread to stderr — the gateway log — and calls
  ``os._exit(WATCHDOG_EXIT_CODE)``;
- a GIL-independent backstop: each tick re-arms
  ``faulthandler.dump_traceback_later(limit_s + BACKSTOP_GRACE_S, exit=True)``.
  If native code blocks the loop while HOLDING the GIL, the Python thread
  above cannot run at all; faulthandler's C thread still dumps every stack
  and exits (code 1).

Both exits are non-zero, which is what `KeepAlive: {SuccessfulExit: false}`
(launchd), `Restart=on-failure` (systemd) and the root installer/supervisor
loops act on. ``time.monotonic()`` does not advance while the machine sleeps
(macOS and Linux), so a laptop waking up is not a stall.

The watchdog runs only under `abstractgateway serve` (the CLI configures it
before the server starts); an app imported elsewhere (tests, `--reload`'s
child) has it off and `/api/health` says so.
"""

from __future__ import annotations

import asyncio
import faulthandler
import os
import sys
import threading
import time
import traceback
from typing import Any, Callable, Dict, Optional

DEFAULT_WATCHDOG_SECONDS = 30.0
# EX_TEMPFAIL (sysexits.h): "temporary failure, the invoker should retry".
WATCHDOG_EXIT_CODE = 75
BACKSTOP_GRACE_S = 15.0

_configured_limit_s: Optional[float] = None
_active: Optional["LoopWatchdog"] = None
_lock = threading.Lock()


def configure(limit_s: Optional[float]) -> None:
    """Called by `serve` before uvicorn starts: the limit for THIS process
    (None or <= 0 = off)."""
    global _configured_limit_s
    _configured_limit_s = float(limit_s) if limit_s is not None and float(limit_s) > 0 else None


def configured_limit_s() -> Optional[float]:
    return _configured_limit_s


def _write(stream: Any, text: str) -> None:
    try:
        stream.write(text)
        stream.flush()
    except Exception:
        pass


def _exit_after_stall(watchdog: "LoopWatchdog", age_s: float) -> None:
    """Default stall action: forensics to the log, then a distinct exit."""
    stream = watchdog.stream
    _write(
        stream,
        f"[FATAL] gateway watchdog: the event loop has not run for {age_s:.1f}s (limit {watchdog.limit_s:g}s, "
        f"`serve --watchdog-seconds`); the gateway answers nothing while it is blocked. Dumping stacks and exiting "
        f"with code {WATCHDOG_EXIT_CODE} so the service manager restarts it.\n",
    )
    _write(stream, watchdog.format_loop_stack())
    try:
        _write(stream, "[FATAL] gateway watchdog: all threads (faulthandler):\n")
        faulthandler.dump_traceback(file=stream, all_threads=True)
    except Exception:
        pass
    _write(stream, f"[FATAL] gateway watchdog: exiting with code {WATCHDOG_EXIT_CODE}\n")
    os._exit(WATCHDOG_EXIT_CODE)


class LoopWatchdog:
    def __init__(
        self,
        limit_s: float,
        *,
        on_stall: Optional[Callable[["LoopWatchdog", float], None]] = None,
        backstop: bool = True,
        stream: Any = None,
    ) -> None:
        self.limit_s = float(limit_s)
        self.tick_s = max(0.05, min(1.0, self.limit_s / 10.0))
        self._on_stall = on_stall or _exit_after_stall
        self._backstop = bool(backstop)
        self.stream = stream if stream is not None else sys.stderr
        self._last_tick = time.monotonic()
        self._loop_thread_id: Optional[int] = None
        self._stop = threading.Event()
        self._task: Optional[asyncio.Task] = None
        self._thread: Optional[threading.Thread] = None
        self.fired = False

    # -- event-loop side ------------------------------------------------
    async def _tick(self) -> None:
        while True:
            self._last_tick = time.monotonic()
            if self._backstop:
                try:
                    faulthandler.dump_traceback_later(self.limit_s + BACKSTOP_GRACE_S, exit=True, file=self.stream)
                except Exception:
                    pass
            await asyncio.sleep(self.tick_s)

    def start(self) -> None:
        """Start from INSIDE the running event loop (lifespan startup)."""
        self._loop_thread_id = threading.get_ident()
        self._last_tick = time.monotonic()
        self._task = asyncio.get_running_loop().create_task(self._tick(), name="gateway-loop-watchdog-tick")
        self._thread = threading.Thread(target=self._watch, name="gateway-loop-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
        if self._backstop:
            try:
                faulthandler.cancel_dump_traceback_later()
            except Exception:
                pass

    # -- watcher side ---------------------------------------------------
    def last_tick_age_s(self) -> float:
        return max(0.0, time.monotonic() - self._last_tick)

    def _watch(self) -> None:
        while not self._stop.wait(self.tick_s):
            age = self.last_tick_age_s()
            if age > self.limit_s:
                self.fired = True
                self._on_stall(self, age)
                return

    def format_loop_stack(self) -> str:
        tid = self._loop_thread_id
        frame = sys._current_frames().get(tid) if tid is not None else None
        if frame is None:
            return "[FATAL] gateway watchdog: event-loop thread stack unavailable\n"
        return "[FATAL] gateway watchdog: the event-loop thread is blocked here:\n" + "".join(traceback.format_stack(frame))

    def snapshot(self) -> Dict[str, Any]:
        return {"enabled": True, "limit_s": self.limit_s, "last_tick_age_s": round(self.last_tick_age_s(), 3)}


def start_configured() -> Optional[LoopWatchdog]:
    """Lifespan startup: start the watchdog `serve` configured (no-op when off)."""
    global _active
    limit = _configured_limit_s
    if limit is None:
        return None
    with _lock:
        if _active is not None:
            _active.stop()
        _active = LoopWatchdog(limit)
        _active.start()
        return _active


def stop_active() -> None:
    """Lifespan shutdown: a draining gateway is not a hung one (uvicorn's
    graceful-shutdown timeout bounds the drain)."""
    global _active
    with _lock:
        if _active is not None:
            _active.stop()
        _active = None


def health_snapshot() -> Dict[str, Any]:
    wd = _active
    if wd is None:
        return {"enabled": False, "limit_s": _configured_limit_s, "last_tick_age_s": None}
    return wd.snapshot()
